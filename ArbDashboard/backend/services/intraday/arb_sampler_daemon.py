#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ArbIntradaySamplerDaemon —— ARM 原生分时采集守护进程（独立 systemd 服务）

背景（东哥 2026-10-14~15 拍板）：
- 2026-10-09 重构把 main.py 里的本地分时采样实例化删了（注释"分时由 ARM 采集"），
  但 ARM 侧那个采集器从没立起来 → fund_intraday_quotes 自 2026-10-10 起冻结。
- 现成 IntradaySamplerService 本身完全可用：它调 get_realtime_quote → 腾讯 gtimg
  （ARM 实测可达，price 正常返回），写入目标基金 162411/164701/161116。
- 本守护进程 = 把现成 IntradaySamplerService 作为**独立进程**常驻运行，
  物理上仍在 ARM（arb-dashboard 同一台机器），符合"分时归 ARM 采集"的意图，
  且与 arb-dashboard 解耦：重启后端不影响采集，亦能被健康巡检独立监控。

设计原则：
- 纯复用现有组件，不重写取价/估值逻辑（第一性原理，非打补丁）。
- 仅负责"构造依赖 + 启动采样循环 + 常驻"，不碰 main.py、不撤销 10-09 删除决定。
"""
import asyncio
import logging
import os
import sys

# ── [AI-2026-10-15] 守护进程运行环境（与 arb-dashboard.service 保持一致）────────────
# - ARB_DASHBOARD_MODE=1：开启富途 OpenD 自动连接守护（否则美股ETF篮子价 XOP/GLD 取不到 → rt_val 算不出 → 0 行）
# - FUTU_HOST=10.0.0.83：AMD 实例在 Oracle 内网的私有 IP（公网 129.146.18.129:11111 被防火墙挡），
#   即 FutuOpenD 真实监听地址。setdefault 允许 systemd 单元用 Environment= 覆盖。
os.environ.setdefault("ARB_DASHBOARD_MODE", "1")
os.environ.setdefault("FUTU_HOST", "10.0.0.83")

# 路径：保证能 import arbcore / services
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.dirname(BACKEND)
for p in (BACKEND, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

from arbcore.database.db_manager import DatabaseManager, _resolve_db_path
from services.config_service import ConfigService
from services.market_data_service import MarketDataService
# [预热] 先完整加载 arbcore.analysis 包，打破 arbcore 内潜伏的循环依赖：
#   dynamic_valuation → analysis.common → analysis/__init__ → realtime_analysis → dynamic_valuation
# main.py 进程靠导入顺序侥幸规避；独立进程是首个触发 dynamic_valuation 加载方，
# 若不在 sampler 之前先加载 analysis 包，构造 sampler 时会撞上循环 import。
import arbcore.analysis.common  # noqa: F401
from services.intraday.sampler_service import IntradaySamplerService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("arb_sampler_daemon")


async def main():
    db_path = _resolve_db_path()
    os.environ.setdefault("ARB_MASTER_DB", db_path)
    log.info("DB path: %s", db_path)

    db = DatabaseManager(db_path=db_path, include_trading=False)
    config_service = ConfigService(db)
    mds = MarketDataService(db)

    # 关键：realtime_manager 必须先 start 才会连接腾讯/新浪（取价链路前提）
    mds.realtime_manager.start()
    log.info(
        "realtime_manager 已启动，活跃源: %s",
        list(mds.realtime_manager.active_fetchers.keys()),
    )

    sampler = IntradaySamplerService(db, mds, config_service)
    await sampler.start()
    log.info("IntradaySamplerService 已启动, running=%s", sampler.running)

    # 常驻：采样循环由 sampler 内部 asyncio task 驱动，这里只保持事件循环存活
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        log.info("收到取消信号，准备退出")
    finally:
        try:
            await sampler.stop()
        except Exception as e:
            log.warning("sampler.stop 异常: %s", e)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt，退出")
