#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
  原油 LOF 实时估值(hrv) 分母采样器 —— CL(WTI) 合约月多时点冻结价。

背景：
  hrv 实时估值分母取各标的确定自身净值的时点：
    CRUD  -> CME WTI 14:30 EDT
    BRNT  -> ICE Brent 11:30 EDT  (本期按东哥口径统一用 CL，带 Brent 残差)
    US三  -> NYSE 16:00 EDT
    1671 / 1699 -> TSE 收盘 15:30 JST = 02:30 EDT
        [2026-09-26 东哥拍板新增] 501018 日本腿（东京 Simplex WTI + 野村原油多头指数，
        合计 11.04%）的定盘时刻。此前被兜底归到 1600，分母锚点错位 13.5h，
        实测系统性偏差 -1.74%（对估值 -0.18%，每天存在、不随日抵消）。
        160723 / 161129 篮子无日本腿，多采此点不影响其估值（消费端不用）。
  本期(2026-09-15 起)改为"有效近月 ±1" 同时采样对比：
    - 有效近月 = 当月 + 2（USO 在每月初 5-8 个工作日滚仓，中下旬实际持有次次月；
      如 9 月 → 近月=11 月 CL2611）
    - WTI 两合约 = [近月, +1月]（如 9 月 → 2611/2612；10 月 → 2612/2701）
    - 下月自动滚动（如 10 月 → 2612/2701）
  [2026-11-10 换月 Bug 修复] 额外固定采「近月链起点 = 当月+1」（如 11 月 → 2612）：
    此前该月只靠 etf_contract_exposure 的 Brent 书月份碰巧补上，换月工具把已交割
    Brent 月停用后就会断供，而 016 观察表 / USO 近月腿正需要它。显式多采一个月，
    按月自动滚动；不并入 get_active_cl_contracts() 以免改变生产估值默认合约口径。
  每个 NY 时点逐合约抓取：WTI 两合约 + Brent 书同月合约 + 近月链起点。
  [2026-09-17 东哥拍板] 2610（主连）底层已滚过、估值对对冲无意义，停止采样；Brent 同月(2701)为跨品种对冲反算保留采样。

部署：
  - 由 systemd timer 在 America/New_York 时区 02:30/11:30/14:30/16:00 触发（冬夏令时自动切换）。
  - 脚本按当前 NY 时间自动判定命中哪个时点（容错 +-10min），非交易时段跳过。
  - 周末 + 2026 美股假日不采（0230 复用同一守卫，已知边界见 POINTS 上方注释）。
  - 落库 futures_freeze_prices(trade_date, point, symbol, price, src, fetched_at)，
    symbol 如 'CL2610'/'CL2611'/'CL2612'。
  - 落库后按 RETAIN_DAYS（默认 30 天，见配置区）滚动保留，并清掉历史遗留的主力连续
    symbol='CL' 及过期合约（不在当前活跃列表中的）。
  - 采样成功经 Hermes 电报(Telegram)通道推送一条给东哥（每天三次，每次带三合约价）。
    [2026-09-17] 东哥已停微信 bot，通道统一走 Telegram（@dongge_inspect_bot，无主动消息频率限制）。

手动测试：
  python cl_freeze_sampler.py --point 1430          # 强制写今天 14:30 那一行（三合约）
  python cl_freeze_sampler.py --point 0230          # 强制写今天 02:30 那一行（日本腿用）
  python cl_freeze_sampler.py --point 1430 --dry-run # 只打印不写库
  python cl_freeze_sampler.py --test-notify          # 只发一条测试 Telegram（不采样不写库）
  python cl_freeze_sampler.py --db /path/arb_master.db
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------- 配置 ----------
DEFAULT_DB = "/home/ubuntu/arbtest/database/arb_master.db"
LOG_DIR = "/home/ubuntu/arbtest/logs"
LOG_FILE = os.path.join(LOG_DIR, "cl_freeze.log")
SINA_BASE = "https://hq.sinajs.cn/list="
SINA_REF = "https://finance.sina.com.cn"

# NY 时点（小时, 分钟）
# [2026-09-26 东哥拍板] 新增 0230 = 东京 TSE 收盘（15:30 JST = 06:30 UTC = 02:30 EDT），
# 供 501018 日本腿（1671/1699）做 hrv 分母；其余三点仍是原口径，不动。
# ⚠️ 两个已知边界（有意不修，理由见 docs/013_4a §6.25.9）：
#   ① EST 冬令时：东京 15:30 JST 恒为 06:30 UTC，而本表按 ET 定点 ⇒ 冬令时实际落在
#      01:30 EST（即采到东京收盘后 1h 的价）。残留量级 < 0.3%，且消费端只影响日本腿，
#      故接受；若要严格对齐应把该条 OnCalendar 改走 UTC（会引入 point 标签语义不一致）。
#   ② 交易日守卫：0230 复用 is_trading_day（美股假日表），周一恰逢美股假日当天会被跳过
#      ⇒ 该日无 0230，消费端逐腿退 1600（等价改动前行为，无副作用；影响约 4 天/年）。
POINTS = {"0230": (2, 30), "1130": (11, 30), "1430": (14, 30), "1600": (16, 0)}
POINT_LABEL = {"0230": "02:30", "1130": "11:30", "1430": "14:30", "1600": "16:00"}

# 冻结价历史保留天数（含今天）。
# [2026-09-15 东哥] 7 天 → 够做短期对比验证，但会让 §016 观察表的行在 7 天后
#   失去可重算性（本机经 sync-freeze 全量替换后随之消失）。
# [2026-10-01 东哥拍板] 7 天 → 30 天：给 hrv 误差观察（016）留足回填官方 NAV
#   与重算的窗口。单日 4 时点 × 3 合约 ≈ 12 行 ⇒ 30 天约 360 行，体积可忽略。
RETAIN_DAYS = 30

# 有效近月 +1 两合约（YYMM），动态计算。
# 规则：有效近月 = 当月 + 2（USO 在每月初 5-8 个工作日滚仓，中下旬实际持有次次月）。
# 例: 9 月 → 近月=11 月 → [2611, 2612]；10 月 → [2612, 2701]
def get_active_cl_contracts(as_of_date=None):
    """返回当前活跃的两个 CL 合约 YYMM 列表 [近月, +1月]。"""
    if as_of_date is None:
        as_of_date = ny_now().date()
    y, m = as_of_date.year, as_of_date.month
    front_m = m + 2  # 有效近月 = 当月 + 2（USO 月中已滚至次次月）
    front_y = y
    if front_m > 12:
        front_m -= 12
        front_y += 1

    def _yymm(year, month):
        return f"{year % 100:02d}{month:02d}"

    contracts = []
    for i in range(2):
        cm = front_m + i
        cy = front_y
        if cm > 12:
            cm -= 12
            cy += 1
        contracts.append(_yymm(cy, cm))
    return contracts


def get_brent_hedge_months(db_path: str) -> list[str]:
    """从 etf_contract_exposure 取 Brent 书、in_book=1 的合约月（如 2701），追加进采样列表，
    供同月 CL 跨品种对冲反算使用。表不存在 / 查询失败返回 []（不影响 WTI 主采样）。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT contract_month FROM etf_contract_exposure "
                "WHERE variety='Brent' AND in_book=1 "
                "AND contract_month IS NOT NULL AND contract_month != ''"
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


# 运行时合约列表（main() 启动时算一次，当天不变）
CONTRACTS: list[str] = []
SRC = "sina"


def sina_code(contract: str) -> str:
    """新浪行情代码，如 'hf_CL2611'。"""
    return "hf_CL" + contract


def symbol_of(contract: str) -> str:
    """落库 symbol，如 'CL2611'。"""
    return "CL" + contract


# [2026-09-17] 采样成功 → 电报(Telegram)通知（复用 ARM 上 Hermes 的 hermes send）
# 东哥已停微信 bot，通道统一走 Telegram（@dongge_inspect_bot，无主动消息频率限制）。
HERMES_BIN = "/home/ubuntu/.local/bin/hermes"
# 账号标识一律走环境变量（本文件进公开仓库，禁止硬编码）。
# ARM 运行时由 systemd unit 的 Environment=HERMES_NOTIFY_TARGET 提供，形如 telegram:<chat_id>
NOTIFY_TARGET = os.environ.get("HERMES_NOTIFY_TARGET", "")

# 2026 美股休市日（ equity holidays，作为采样守卫；期货略有差异但不影响快照语义）
US_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}

# ---------- NY 时间（优先 zoneinfo，失败回退 DST 手工规则） ----------
try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:
    _NY = None


def _ny_offset_utc(dt_utc: datetime) -> int:
    """America/New_York 相对 UTC 的偏移秒数（DST: UTC-4, 否则 UTC-5）。"""
    year = dt_utc.year
    # DST 起点：3 月第 2 个周日 02:00 本地 -> 07:00 UTC
    mar = datetime(year, 3, 1, tzinfo=timezone.utc)
    d = mar
    sundays = 0
    while d.month == 3:
        if d.weekday() == 6:
            sundays += 1
            if sundays == 2:
                dst_start = d.replace(hour=7)
                break
        d += timedelta(days=1)
    # DST 终点：11 月第 1 个周日 02:00 本地 -> 06:00 UTC
    nov = datetime(year, 11, 1, tzinfo=timezone.utc)
    d = nov
    first_sunday = None
    while d.month == 11:
        if d.weekday() == 6:
            first_sunday = d.replace(hour=6)
            break
        d += timedelta(days=1)
    if dst_start <= dt_utc < first_sunday:
        return -4 * 3600  # EDT
    return -5 * 3600  # EST


def ny_now() -> datetime:
    if _NY is not None:
        return datetime.now(_NY)
    dt_utc = datetime.now(timezone.utc)
    off = _ny_offset_utc(dt_utc)
    return dt_utc.astimezone(timezone(timedelta(seconds=off)))


def detect_point(now: datetime, tol_min: int = 10) -> str | None:
    hhmm = now.hour * 60 + now.minute
    for p, (h, m) in POINTS.items():
        if abs(hhmm - (h * 60 + m)) <= tol_min:
            return p
    return None


def is_trading_day(d: datetime) -> bool:
    if d.weekday() >= 5:  # 周六/周日
        return False
    if d.strftime("%Y-%m-%d") in US_HOLIDAYS_2026:
        return False
    return True


# ---------- 行情抓取 ----------
def fetch_cl(contract: str) -> tuple[float, str]:
    """抓取单个远月合约（如 2611）新浪 hf_CL2611 价格。返回 (price, sina_time)。"""
    code = sina_code(contract)
    req = urllib.request.Request(
        SINA_BASE + code, headers={"Referer": SINA_REF, "User-Agent": "Mozilla/5.0"}
    )
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", "ignore")
    m = re.search(r'var hq_str_' + code + r'="(.*?)"', raw)
    if not m:
        raise ValueError(f"新浪返回未匹配到 {code} 行情")
    fields = m.group(1).split(",")
    price = None
    for f in fields:
        f = f.strip()
        try:
            v = float(f)
            if 1.0 < v < 1000.0:  # WTI 合理区间
                price = v
                break
        except ValueError:
            continue
    if price is None:
        raise ValueError(f"新浪 {code} 未解析出有效价格")
    t = fields[-1].strip() if fields else ""
    return price, t


# ---------- 落库 ----------
CREATE_FREEZE_SQL = """
    CREATE TABLE IF NOT EXISTS futures_freeze_prices (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date  TEXT NOT NULL,
        point       TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        price       REAL NOT NULL,
        src         TEXT NOT NULL DEFAULT 'sina',
        fetched_at  TEXT NOT NULL,
        UNIQUE(trade_date, point, symbol)
    )
"""


def write_rows(db_path: str, trade_date: str, point: str,
               rows: list[tuple[str, float, str, str]], active_symbols: list[str]) -> int:
    """写入一批合约采样（rows: [(symbol, price, src, fetched_at), ...]），并清理旧数据。

    清理策略：保留最近 RETAIN_DAYS 天（今天及过去 RETAIN_DAYS-1 天）且 symbol 在活跃
    列表中的行；删除更早的历史、以及历史遗留的主力连续 symbol='CL' 和过期月合约，
    避免旧数据污染。
    （东哥 2026-09-15 拍板：保留 7 天历史用于对比验证，而非每晚只留当天；
      东哥 2026-10-01 拍板：7 天 → 30 天，见 RETAIN_DAYS 上方注释）
    返回写入行数。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(CREATE_FREEZE_SQL)
        for symbol, price, src, fetched_at in rows:
            conn.execute(
                """
                INSERT INTO futures_freeze_prices
                    (trade_date, point, symbol, price, src, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, point, symbol) DO UPDATE SET
                    price=excluded.price, src=excluded.src, fetched_at=excluded.fetched_at
                """,
                (trade_date, point, symbol, price, src, fetched_at),
            )
        # 清理：保留最近 RETAIN_DAYS 天(今天及过去 RETAIN_DAYS-1 天) 且 symbol 在活跃列表中的行；
        # 删除更早历史 + 旧主力连续('CL') + 过期月合约。
        cutoff = None
        try:
            d = datetime.strptime(trade_date, "%Y-%m-%d").date()
            cutoff = (d - timedelta(days=RETAIN_DAYS - 1)).strftime("%Y-%m-%d")
        except Exception:
            cutoff = None
        cutoff_sql = "trade_date < ? OR " if cutoff else ""
        cutoff_params = [cutoff] if cutoff else []
        placeholders = ",".join("?" for _ in active_symbols)
        del_cur = conn.execute(
            f"DELETE FROM futures_freeze_prices WHERE {cutoff_sql}symbol NOT IN ({placeholders})",
            tuple(cutoff_params + active_symbols))
        if del_cur.rowcount:
            log = logging.getLogger("cl_freeze")
            log.info("清理旧冻结价 %d 条（保留最近%d天活跃合约），cutoff=%s",
                     del_cur.rowcount, RETAIN_DAYS, cutoff or "n/a")
        conn.commit()
        return len(rows)
    finally:
        conn.close()


# ---------- 电报(Telegram)通知 ----------
def notify_telegram(trade_date: str, point: str, prices: dict[str, float]) -> None:
    """采样落库成功后给东哥电报(@dongge_inspect_bot)推一条（prices: 合约->价格）。失败绝不影响采样主流程。

    复用 ARM 上 Hermes 的 `hermes send`（无 LLM / 无 agent loop），目标为 Telegram。
    以 ubuntu 用户运行（与 cl-freeze.service 一致），显式带上 HOME 以读到 ~/.hermes。
    """
    import subprocess
    log = logging.getLogger("cl_freeze")
    if point == "TEST":
        msg = ("【CL冻结价采样】通道测试\n"
               f"发送时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}\n"
               "若你收到此条，说明采样成功自动通知已就绪。")
    else:
        bj = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
        parts = " ".join(f"{c}={prices[c]:.3f}" for c in sorted(prices))
        msg = f"{bj} 采样 {trade_date} point={point} {parts} → 落库成功 ✅"
    if not NOTIFY_TARGET:
        log.warning("未配置 HERMES_NOTIFY_TARGET，跳过 Telegram 通知（不影响落库）")
        return
    env = dict(os.environ)
    env.setdefault("HOME", "/home/ubuntu")
    # Telegram 通道无主动消息频率限制；失败即等 60s 重试，最多 3 次；通知失败绝不影响采样主流程。
    attempts = 3
    for i in range(attempts):
        try:
            r = subprocess.run(
                [HERMES_BIN, "send", "--to", NOTIFY_TARGET, msg],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if r.returncode == 0:
                log.info("Telegram 通知已发送 point=%s", point)
                return
            log.warning("Telegram 通知失败(第%d/%d次) rc=%s err=%s",
                        i + 1, attempts, r.returncode, (r.stderr or "")[:200])
        except Exception as e:
            log.warning("Telegram 通知异常(第%d/%d次): %s", i + 1, attempts, e)
        if i < attempts - 1:
            time.sleep(60)
    log.error("Telegram 通知最终失败(已重试%d次，不影响采样)", attempts)


# ---------- 主流程 ----------
def main() -> int:
    ap = argparse.ArgumentParser(description="CL 合约月多时点冻结采样器（02:30/11:30/14:30/16:00 ET，有效近月±1 三合约）")
    ap.add_argument("--point", choices=list(POINTS.keys()),
                    help="手动指定时点(测试用)，不指定则按当前 NY 时间自动判定")
    ap.add_argument("--db", default=DEFAULT_DB, help="目标 sqlite 路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    ap.add_argument("--no-notify", action="store_true", help="落库后不发送 Telegram 通知")
    ap.add_argument("--test-notify", action="store_true",
                    help="仅发送一条测试 Telegram（不采样不写库）")
    args = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger("cl_freeze")

    if args.test_notify:
        log.info("[test-notify] 仅发送测试 Telegram 通知")
        notify_telegram(ny_now().strftime("%Y-%m-%d"), "TEST", {})
        return 0

    now = ny_now()
    # 动态计算当日活跃合约：WTI 有效近月±1 + Brent 书合约月（跨品种对冲反算）
    # + 近月链起点（当月+1，详见文件头 2026-11-10 说明）
    global CONTRACTS
    CONTRACTS = get_active_cl_contracts(now.date())
    for m in get_brent_hedge_months(args.db):
        if m not in CONTRACTS:
            CONTRACTS.append(m)
    # [2026-11-10] 近月链起点（当月+1）：016 观察表与 USO/1671/03175 近月腿需要
    # CL2612 这类合约；此前只靠 etf_contract_exposure 的 Brent 书月份「碰巧」补上，
    # 换月工具把已交割 Brent 月停用后该月会断供。显式多采一个月，按月自动滚动。
    # ⚠️ 不并入 get_active_cl_contracts() 的返回值：那会改变 selected_contract
    #    （生产估值默认合约）的口径。本改动只扩大采样覆盖，不动估值口径。
    nm, ny = now.month + 1, now.year
    if nm > 12:
        nm -= 12
        ny += 1
    near_chain = f"{ny % 100:02d}{nm:02d}"
    if near_chain not in CONTRACTS:
        CONTRACTS.append(near_chain)
    trade_date = now.strftime("%Y-%m-%d")
    point = args.point or detect_point(now)
    if point is None:
        log.info("当前 NY 时间 %s 不命中任一采样点，跳过", now.strftime("%H:%M"))
        return 0
    if not args.point and not is_trading_day(now):
        log.info("%s 非美股交易日，跳过采样", trade_date)
        return 0

    # 逐个抓取当日活跃合约链
    prices: dict[str, float] = {}
    for contract in CONTRACTS:
        try:
            price, _sina_time = fetch_cl(contract)
            prices[contract] = price
            log.info("采样 trade_date=%s point=%s symbol=%s price=%.3f",
                     trade_date, point, symbol_of(contract), price)
        except Exception as e:
            log.error("抓取 %s 失败: %s", sina_code(contract), e)

    if not prices:
        log.error("全部合约抓取失败，跳过写库")
        return 1

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.dry_run:
        log.info("[dry-run] 不写库 db=%s active_contracts=%s prices=%s", args.db, CONTRACTS, prices)
        return 0

    active_syms = [symbol_of(c) for c in CONTRACTS]
    rows = [(symbol_of(c), prices[c], SRC, fetched_at) for c in sorted(prices)]
    try:
        n = write_rows(args.db, trade_date, point, rows, active_syms)
    except Exception as e:
        log.error("写库失败 db=%s: %s", args.db, e)
        return 1
    log.info("落库成功（%d 个合约）", n)

    if not args.no_notify:
        notify_telegram(trade_date, point, prices)
    return 0


if __name__ == "__main__":
    sys.exit(main())
