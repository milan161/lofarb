import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from arbcore.utils.market_calendar import is_a_share_session  # [AI-2026-08-16] 交易时段门禁


logger = logging.getLogger(__name__)


# 【AI-2026-07-20 原】分类优先级管理：改为从 app_settings 读取暂停列表
# HIGH_FREQ_CATEGORIES 是系统支持的全部分类（每分类独立 3s 快照循环）
# [AI-2026-09-23] 分类级暂停功能已完整删除（见 docs/013_7），此处仅保留 ALL_CATEGORIES 供快照循环遍历
# [AI-2026-10-11 东哥拍板] 收敛为 LOFarb **实有的 4 类**（原硬编码 7 类含已废弃的
#   QDII亚洲 / 国内LOF / 现金管理 ⇒ 启动日志天天刷「跳过空分类(无成员基金，不起循环)」噪音，
#   东哥要求不再显示）。新增分类时请同步这里。
ALL_CATEGORIES = ["黄金原油", "QDII欧美", "QDII日本", "商品"]


class DashboardSnapshotService:
    """Background dashboard cache.

    API handlers should read this service instead of calculating dashboard data
    inline. If a refresh fails, the last successful snapshot is kept.

    [AI-2026-09-23] 分类级暂停功能已删除：所有"有成员基金"的分类都启动独立快照循环（3s 刷新）；
    空分类（无成员基金）不起循环，避免空载计算与首屏填充噪音。
    """

    def __init__(
        self,
        fund_service,
        market_data_service=None,
        high_interval: float = 3.0,
        normal_interval: float = 30.0,
        idle_interval: float = 60.0,
    ):
        self.fund_service = fund_service
        self.market_data_service = market_data_service
        self.high_interval = high_interval
        self.normal_interval = normal_interval
        # [AI-2026-08-16] 非交易时段(盘后/盘前/周末/节假日)轮询休眠间隔：跳过重算、保留缓存。
        self.idle_interval = idle_interval
        self._lock = threading.RLock()
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._last_errors: Dict[str, str] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []

    def _category_has_funds(self, category: str) -> bool:
        """分类是否在 unified_fund_list 中有成员基金。空分类(如已删空的暂停分类)无需起快照循环。"""
        try:
            dbm = getattr(self.fund_service, 'db', None)
            path = getattr(dbm, 'db_path', None) if dbm else None
            if not path:
                return True  # 拿不到 DB 时保守保留循环，避免误杀有基金分类
            conn = sqlite3.connect(path, timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM unified_fund_list WHERE category = ?", (category,)
                ).fetchone()
                return bool(row and row[0] > 0)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("[SNAPSHOT] 分类基金数探测失败 %s: %s", category, exc)
            return True

    async def start(self):
        if self._running:
            return
        self._running = True

        # [AI-2026-09-23] 只给"有成员基金"的分类起快照循环：空分类(如已删空的 QDII亚洲/国内LOF/现金管理)
        # 起循环只会每 3s 空载计算 + 刷"首屏填充 基金数=0"噪音，直接跳过。
        active_categories = [c for c in ALL_CATEGORIES if self._category_has_funds(c)]
        skipped = [c for c in ALL_CATEGORIES if c not in active_categories]
        if skipped:
            logger.info(f"[SNAPSHOT] 跳过空分类(无成员基金，不起循环): {skipped}")
        logger.info(f"[SNAPSHOT] 有基金分类启动快照循环({len(active_categories)}/{len(ALL_CATEGORIES)}): {active_categories}")

        # [AI-2026-08-25] 立即后台启动首次刷新（非阻塞 lifespan）：
        # 不阻断 uvicorn listen，前端 wait-backend 秒级通过；同时保证
        # 即使已过 15:00（is_a_share_session=False），首屏也能在 ~几秒内填满。
        async def _initial_refresh():
            for cat in active_categories:
                try:
                    await self.refresh_once(cat, None, cat)
                except Exception:
                    logger.exception("[SNAPSHOT] 初始刷新失败: %s", cat)
        asyncio.create_task(_initial_refresh())

        # watchlist 始终运行
        self._tasks = [
            asyncio.create_task(self._loop("watchlist", self.high_interval, True, None)),
        ]
        # 每个未暂停的分类启动独立循环
        for cat in active_categories:
            self._tasks.append(
                asyncio.create_task(self._loop(cat, self.high_interval, False, cat))
            )
        logger.info(f"Dashboard snapshot service started ({len(active_categories)} active categories)")

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("Dashboard snapshot service stopped")

    async def _loop(self, key: str, interval: float, use_db_watchlist: bool, category: Optional[str]):
        while self._running:
            # [AI-2026-08-16] 交易时段门禁：仅 A 股交易时段(9:30-15:00 交易日,含午休)做实时刷新；
            # 盘后/盘前/周末/节假日跳过重算、保留缓存、长休眠，避免空载实时估值轮询空烧 CPU。
            # [AI-2026-09-11] 但 NAV 库若已落库比快照更新的净值(step4/定时净值更新)，立即重建一次，
            # 否则看板净值日期会停在陈旧值（本次 Bug 根因：盘后 step4 更新了库，快照却不再重算）。
            if not is_a_share_session():
                try:
                    if self._db_nav_newer_than_snapshot(key):
                        await self.refresh_once(key, None, category, use_db_watchlist=use_db_watchlist)
                except Exception as exc:
                    logger.warning("[SNAPSHOT] 盘后快照重建失败 %s: %s", key, exc)
                await asyncio.sleep(self.idle_interval)
                continue
            # 分类级暂停功能已于 2026-09-23 删除：所有"有成员基金"的分类都正常跑快照循环。
            started = time.monotonic()
            try:
                await self.refresh_once(key, None, category, use_db_watchlist=use_db_watchlist)
            except Exception as exc:
                logger.warning("Dashboard snapshot loop failed for %s: %s", key, exc)
            await asyncio.sleep(max(0.2, interval - (time.monotonic() - started)))

    def _source_status(self) -> Dict[str, Any]:
        if not self.market_data_service:
            return {}
        realtime = getattr(self.market_data_service, "realtime_manager", None)
        return {
            "active_sources": self.market_data_service.get_active_source_names(),
            "ib_connected": bool(getattr(getattr(self.market_data_service, "ib_reader", None), "connected", False)),
            "futu_disabled": bool(getattr(getattr(self.market_data_service, "futu_reader", None), "disabled", True)),
            "realtime_symbols": len(getattr(realtime, "symbols", []) or []),
        }

    def _read_watchlist_from_db(self) -> List[str]:
        try:
            return self.fund_service.get_my_watchlist()
        except Exception as exc:
            logger.warning("Failed to read dashboard watchlist: %s", exc)
            return []

    # [AI-2026-09-11] NAV 库新鲜度探针：盘后/周末也能发现 step4 或定时净值更新已落库的新净值，
    # 触发快照重建，避免看板停在陈旧净值日期（本次 Bug 根因）。
    def _latest_db_nav_date(self) -> Optional[str]:
        """返回 unified_fund_history 中最新有效净值日期（ISO 字符串）；失败返回 None。"""
        try:
            dbm = getattr(self.fund_service, "db", None)
            path = getattr(dbm, "db_path", None) if dbm else None
            if not path:
                return None
            conn = sqlite3.connect(path, timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT MAX(date) FROM unified_fund_history WHERE nav IS NOT NULL AND nav > 0"
                ).fetchone()
                return row[0] if row and row[0] else None
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("[SNAPSHOT] NAV 日期探针失败: %s", exc)
            return None

    def _db_nav_newer_than_snapshot(self, key: str) -> bool:
        """DB 最新净值日期是否比当前快照反映的更新（盘后/周末重建判定）。"""
        snap = self._snapshots.get(key)
        snap_nav_date = snap.get("max_nav_date") if snap else None
        db_nav_date = self._latest_db_nav_date()
        if not db_nav_date:
            return False
        if not snap_nav_date:
            return True
        return str(db_nav_date) > str(snap_nav_date)

    async def refresh_once(
        self,
        key: str,
        watchlist: Optional[List[str]],
        category: Optional[str],
        use_db_watchlist: bool = False,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        with self._lock:
            first_fill = key not in self._snapshots

        def _compute():
            effective_watchlist = self._read_watchlist_from_db() if use_db_watchlist else watchlist
            return self.fund_service.get_unified_dashboard_data(
                watchlist=effective_watchlist,
                category=category,
            )

        try:
            data = await asyncio.to_thread(_compute)
            compute_ms = int((time.monotonic() - started) * 1000)
            # [AI-2026-09-11] 记录本快照反映的最新净值日期，用于盘后/周末检测 NAV 库是否比快照更新。
            nav_dates = [str(r.get("nav_date")) for r in (data or []) if r.get("nav_date")]
            max_nav_date = max(nav_dates) if nav_dates else None
            snapshot = {
                "data": data,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "stale": False,
                "source_status": self._source_status(),
                "compute_ms": compute_ms,
                "error": None,
                "key": key,
                "max_nav_date": max_nav_date,
            }
            with self._lock:
                self._snapshots[key] = snapshot
                self._last_errors.pop(key, None)
            if first_fill:
                logger.info("[SNAPSHOT] 首屏填充 key=%s 耗时=%dms 基金数=%d",
                            key, compute_ms, len(data) if isinstance(data, list) else 0)
            return snapshot
        except Exception as exc:
            compute_ms = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._last_errors[key] = str(exc)
                previous = self._snapshots.get(key)
                if previous:
                    stale = dict(previous)
                    stale.update({"stale": True, "error": str(exc), "compute_ms": compute_ms})
                    self._snapshots[key] = stale
                    return stale
            logger.exception("Dashboard snapshot refresh failed for %s", key)
            raise

    def _snapshot_key(self, watchlist: Optional[List[str]], category: Optional[str]) -> str:
        if watchlist:
            return "watchlist"
        if category:
            return category
        # [AI-2026-07-20] 不再生成"all"全量快照，改由合并各分类快照
        return "_combined"

    def get_snapshot(self, watchlist: Optional[List[str]] = None, category: Optional[str] = None) -> Dict[str, Any]:
        key = self._snapshot_key(watchlist, category)
        with self._lock:
            if category:
                # 请求特定分类
                snapshot = self._snapshots.get(category)
                if snapshot:
                    return dict(snapshot)
                # [AI-2026-10-06] 兜底：分类无后台快照循环（如后端启动时该分类尚无成员基金、
                # 或首屏 refresh_once 抛异常且 previous=None 未落下任何快照）时，按需同步构建一次，
                # 避免「TAB 存在却永久 NO DATA / dashboard snapshot not ready」。
                # 触发条件 = self._snapshots 中无该分类键（首填失败或循环未起），下次请求即自愈，
                # 不再依赖重启后端。构建失败仅记 WARNING 并返回空，绝不抛到 API 层。
                if self._category_has_funds(category):
                    try:
                        data = self.fund_service.get_unified_dashboard_data(category=category)
                        if isinstance(data, list) and data:
                            snapshot = {
                                "data": data,
                                "updated_at": datetime.now().isoformat(timespec="seconds"),
                                "stale": False,
                                "source_status": self._source_status(),
                                "compute_ms": 0,
                                "error": None,
                                "key": category,
                                "max_nav_date": None,
                            }
                            self._snapshots[category] = snapshot
                            logger.info("[SNAPSHOT] 按需构建分类快照 key=%s 基金数=%d", category, len(data))
                            return dict(snapshot)
                        # 空结果（异常被 get_unified_dashboard_data 顶层吞成 []，或该分类确无数据）：
                        # 不落快照，下次请求重试（函数自身 5s 缓存保证重试开销极小），避免永久缓存空。
                        logger.warning("[SNAPSHOT] 按需构建分类快照 %s 返回空（可能该分类估值抛异常，查后端日志 'get_unified_dashboard_data 失败'）", category)
                    except Exception as exc:
                        logger.warning("[SNAPSHOT] 按需构建分类快照失败 %s: %s", category, exc)
                return {
                    "data": [],
                    "updated_at": None,
                    "stale": True,
                    "source_status": self._source_status(),
                    "compute_ms": 0,
                    "error": "dashboard snapshot not ready",
                    "key": key,
                }
            elif watchlist:
                # [AI-2026-08-16] 优先取专用 watchlist 快照；若不存在(周末/重启后门禁未生成)，
                # fallback 到从各分类缓存快照中按 fund_code 过滤，避免"我的自选"在非交易日显示空白。
                #
                # [AI-2026-11-14 修 BUG·两层] 东哥实测「我的自选」时好时坏，查下来是两个问题叠加：
                #   BUG-1 原逻辑只要 watchlist 快照存在就**无条件 return**，哪怕过滤后是空
                #         （?watchlist=164824 → 0 条，但全量 52 条里有它）。
                #   BUG-2 只修"为空才兜底"仍不够：专用快照的基金池只覆盖部分自选
                #         （实测 ?watchlist=164824,159502,159985,513000 → 只回 2 只，缺 159502/513000）。
                #   ⇒ 改为「按缺失补齐」：先从专用快照取，缺的那几只再从各分类快照里捞。
                allowed = set(watchlist)
                found = []
                snapshot = self._snapshots.get("watchlist")
                if snapshot:
                    found = [item for item in snapshot.get("data", []) if item.get("fund_code") in allowed]
                    missing = allowed - {it.get("fund_code") for it in found}
                    if not missing:
                        # 专用快照已覆盖全部请求 → 直接用它（保留其 updated_at 口径）
                        result = dict(snapshot)
                        result["data"] = found
                        result["key"] = "watchlist_request"
                        return result
                else:
                    missing = set(allowed)
                # fallback / 补齐：从分类快照聚合，只补 missing 的部分（避免与 found 重复）
                combined = list(found)
                latest_updated = None
                for cat in ALL_CATEGORIES:
                    snap = self._snapshots.get(cat)
                    if snap and snap.get("data"):
                        for item in snap["data"]:
                            code = item.get("fund_code")
                            if code in missing:
                                combined.append(item)
                                missing.discard(code)
                        if snap.get("updated_at") and (not latest_updated or snap["updated_at"] > latest_updated):
                            latest_updated = snap["updated_at"]
                if missing:
                    logger.warning(
                        "[SNAPSHOT] 自选中有 %d 只不在任何分类快照内: %s（可能估值失败或已停更）",
                        len(missing), sorted(missing),
                    )
                if combined:
                    return {
                        "data": combined,
                        "updated_at": latest_updated,
                        "stale": False,
                        "source_status": self._source_status(),
                        "compute_ms": None,
                        "error": None,
                        "key": "watchlist_fallback",
                    }
            else:
                # 无分类/无 watchlist → 合并所有非暂停分类的快照数据
                combined_data = []
                latest_updated = None
                for cat in ALL_CATEGORIES:
                    snap = self._snapshots.get(cat)
                    if snap and snap.get("data"):
                        combined_data.extend(snap["data"])
                        if snap.get("updated_at") and (not latest_updated or snap["updated_at"] > latest_updated):
                            latest_updated = snap["updated_at"]
                if combined_data:
                    return {
                        "data": combined_data,
                        "updated_at": latest_updated,
                        "stale": False,
                        "source_status": self._source_status(),
                        "compute_ms": None,
                        "error": None,
                        "key": "_combined",
                    }
        return {
            "data": [],
            "updated_at": None,
            "stale": True,
            "source_status": self._source_status(),
            "compute_ms": 0,
            "error": "dashboard snapshot not ready",
            "key": key,
        }

    def get_runtime_health(self) -> Dict[str, Any]:
        now = datetime.now()
        with self._lock:
            snapshots = {}
            for key, snap in self._snapshots.items():
                updated_at = snap.get("updated_at")
                age_seconds = None
                if updated_at:
                    try:
                        age_seconds = (now - datetime.fromisoformat(updated_at)).total_seconds()
                    except Exception:
                        age_seconds = None
                snapshots[key] = {
                    "updated_at": updated_at,
                    "age_seconds": age_seconds,
                    "stale": snap.get("stale", False),
                    "compute_ms": snap.get("compute_ms", 0),
                    "rows": len(snap.get("data") or []),
                    "max_nav_date": snap.get("max_nav_date"),
                    "error": snap.get("error"),
                }
            return {
                "running": self._running,
                "snapshots": snapshots,
                "last_errors": dict(self._last_errors),
                "source_status": self._source_status(),
            }
