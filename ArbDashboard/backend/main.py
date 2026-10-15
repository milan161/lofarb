import os
import sys
import importlib.util
import asyncio

# [AI-2026-08-03 / 2026-08-05] 强制唯一启动解释器：后端必须用项目虚拟环境 ArbDashboard/.venv 启动。
# 机器上存在多个 Python（系统 Python311 / WorkBuddy 内置 3.13），其中只有 .venv 同时装有
# ibapi + apscheduler + xtquant + akshare 等全部依赖。用错解释器会导致 IB 行情 / 定时调度器
# 静默失效（典型症状：No module named 'apscheduler'、IB 盘口全空）。
# 早前做法是“缺依赖就 fatal 退出”，但误双击 main.py 仍可能用系统 Python 起第二个后端、或闪退。
# 改为“非 .venv 解释器（系统 Python / WorkBuddy python / 误双击）在此无缝 re-exec 到 .venv”——
# 第一性原理：用户怎么启动都只该有一个 .venv 后端，从源头杜绝用错 python / 双实例抢 IB ClientId。
# 守卫只针对 Windows 本机开发环境（多 python 容易点错）；云端 Linux 看板用 systemd 固定解释器且
# ARB_DASHBOARD_MODE=1 跳过本守卫（看板不连 IB、不跑本地调度，依赖也不同）。
def _require_project_venv():
    if sys.platform != 'win32' or os.environ.get('ARB_DASHBOARD_MODE', '0') == '1':
        return
    if '.venv' in sys.executable.lower():
        return  # 已经在正确的项目解释器
    # 非 .venv 解释器 → 自动重定向到 .venv（不再 fatal 退出，避免闪退/双后端）
    venv_py = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '.venv', 'Scripts', 'python.exe'))
    if os.path.exists(venv_py):
        sys.stderr.write("[venv] 检测到非项目 .venv 解释器，自动重定向到 .venv 启动...\n")
        try:
            os.execv(venv_py, [venv_py, os.path.abspath(__file__)] + sys.argv[1:])
        except Exception as e:
            sys.stderr.write(f"[venv] 重定向失败: {e}\n")
    sys.stderr.write(
        "\n\033[1;91m[致命] 后端必须用项目虚拟环境启动，但未找到 ArbDashboard/.venv/Scripts/python.exe\033[0m\n"
        "[致命] 请先创建虚拟环境，或改用 ArbDashboard/backend/runback.bat 启动\n\n")
    sys.exit(2)

_require_project_venv()

import json
import subprocess
import threading

# [AI-2026-08-05] 单实例守卫已禁用：用户要求“不搞 kill”，且进程扫描可能误判导致后端起不来。
# 双实例防护改由 runback.bat 的 8000 端口检查 + _require_project_venv 的 re-exec 共同保证。
def _enforce_single_instance():
    return

_enforce_single_instance()


import time
import pandas as pd
import logging
from logging.handlers import RotatingFileHandler
import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Form, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from datetime import datetime

# Setup logging
backend_dir = os.path.dirname(os.path.abspath(__file__))
workspace_root = os.path.abspath(os.path.join(backend_dir, ".."))
# [AI-2026-10-08 修正] 仓库根 = src 的父：deploy/、database/ 都在这一层（deploy 已被移出 src/）。
#   ARM 同构：/home/ubuntu/arbtest/{src,deploy,database}。禁止再用 <repo>/src/deploy（该路径不存在）。
repo_root = os.path.abspath(os.path.join(workspace_root, "..", ".."))
logs_dir = os.path.abspath(os.path.join(repo_root, "logs"))

START_TIME = time.time()  # 进程启动时刻，供 /api/status 计算运行时长

if not os.path.exists(logs_dir):
    os.makedirs(logs_dir, exist_ok=True)

log_filename = datetime.now().strftime("%Y-%m-%d_%H%M%S.log")
log_filepath = os.path.join(logs_dir, log_filename)

log_format = '%(asctime)s - %(levelname)s - %(message)s - %(name)s'

class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[94m',
        'INFO': '\033[92m',       # Green
        'WARNING': '\033[93m',    # Yellow
        'ERROR': '\033[91m',      # Red
        'CRITICAL': '\033[1;91m'  # Bold Red
    }
    RESET = '\033[0m'
    
    def format(self, record):
        original_levelname = record.levelname
        color = self.COLORS.get(original_levelname, self.RESET)
        record.levelname = f"{color}{original_levelname}{self.RESET}"
        formatted = super().format(record)
        record.levelname = original_levelname
        return formatted

# Setup File Handler (no colors)
file_handler = RotatingFileHandler(
    log_filepath,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding='utf-8'
)
file_handler.setFormatter(logging.Formatter(log_format))

# [AI-2026-09-03] 先固化原始 stderr 给 console_handler。
# 下面会把 sys.stdout/sys.stderr 重定向到 Tee（同时写文件），若 handler 用默认 sys.stderr，
# 重定向后每条 logger 记录会被 Tee 再抄一份进文件 → 日志重复。显式绑定原始流可避免。
_orig_stdout = sys.stdout
_orig_stderr = sys.stderr

# Setup Console Handler (with colors)
console_handler = logging.StreamHandler(_orig_stderr)
console_handler.setFormatter(ColorFormatter(log_format))

# Configure Root Logger
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger("ArbTest")

# ============================================================
# [AI-2026-09-03] stdout/stderr 落盘（消除 print 诊断盲区）
# 背景：arbcore/fetchers/ib_reader.py 等模块大量用 print 输出关键信息
#   （IB 连接端口/ClientId/连接成功失败、Error 错误码、订阅发起、行情农场状态…），
#   这些内容只进控制台、不进日志文件 → 每次 IB 断连日志里前后一片空白，只能靠猜。
# 做法：把 sys.stdout / sys.stderr 换成 Tee，原样透传控制台 + 同步写入
#   logs/stdout_<同时间戳>.log（与主日志同时间戳，便于对照）。
# 设计要点：
#   1) 行缓冲 + 锁：print(a, b) 会分多次 write，多线程下不累积整行会交错撕裂。
#   2) utf-8 + errors='replace'：绝不让编码异常再触发一轮崩溃/重连。
#   3) 代理 __getattr__ 到原始流：uvicorn/第三方库调 fileno()/isatty() 时行为不变。
#   4) 任何异常都不得影响主流程，重定向失败只告警、不阻断启动。
# ============================================================
class _TeeStream:
    """原样透传控制台，同时把内容抄写进日志文件。"""

    def __init__(self, primary, secondary, lock, name):
        self._primary = primary      # 原始 sys.stdout / sys.stderr
        self._secondary = secondary  # 日志文件对象
        self._lock = lock
        self._name = name
        self._buf = []

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)
        try:
            with self._lock:
                self._buf.append(data)
                if '\n' in data:
                    line = ''.join(self._buf)
                    self._buf = []
                    self._secondary.write(line)
                    self._secondary.flush()
        except Exception:
            pass
        try:
            self._primary.write(data)
        except (UnicodeEncodeError, OSError):
            pass
        return len(data)

    def flush(self):
        try:
            with self._lock:
                if self._buf:
                    self._secondary.write(''.join(self._buf))
                    self._buf = []
                self._secondary.flush()
        except Exception:
            pass
        try:
            self._primary.flush()
        except Exception:
            pass

    # 第三方库（uvicorn / ibapi / 各 SDK）可能访问流对象的其它属性，统一代理到原始流
    def __getattr__(self, item):
        return getattr(self._primary, item)


try:
    _stdout_log_path = os.path.join(
        logs_dir, datetime.now().strftime("stdout_%Y-%m-%d_%H%M%S.log")
    )
    _stdout_log_fh = open(_stdout_log_path, 'a', encoding='utf-8', errors='replace')
    _tee_lock = threading.Lock()
    sys.stdout = _TeeStream(_orig_stdout, _stdout_log_fh, _tee_lock, 'stdout')
    sys.stderr = _TeeStream(_orig_stderr, _stdout_log_fh, _tee_lock, 'stderr')
    logger.info(f"📝 stdout/stderr 已同步落盘: {_stdout_log_path}")
except Exception as _e:
    logger.warning(f"⚠️ stdout/stderr 落盘初始化失败（不影响主流程）: {_e}")

# [Master-Slave] 检查主交易程序 (LOFarb) 是否运行
# 强制设为 False，防止 opencode cli 等占用 5000 端口导致误判为 Slave 只读模式
lof_is_running = False

# [AI-2026-08-02] 云端看板模式开关：ARM 部署瘦身看板时设 ARB_DASHBOARD_MODE=1，禁用下单相关私有插件，纯展示
DASHBOARD_MODE = os.environ.get('ARB_DASHBOARD_MODE', '0') == '1'

# [V4.4] 强力补丁：全局唯一 TQ 抢占与锁定
# [V10.0] 启动时不自动连接通达信，跳过 TQ 全局初始化（用户点击"通达信"按钮时才需要）
# 用户手动重连通达信时，TdxRealtimeFetcher.connect() 会自行完成 TQ 初始化
logger.info("[V10.0] 跳过 TQ 全局初始化（通达信待用户手动连接）")

# Add project root and core/arbcore to path
# [FIX][AI-2026-08-16] arbcore 为核心模块目录（项目根 src/ 下的 arbcore）
backend_dir = os.path.dirname(os.path.abspath(__file__))
# arbcore 在 ArbDashboard 的上级目录 (项目根 src/arbcore)
# 需要添加项目根 (ArbDashboard 的上级目录) 到 sys.path，这样 Python 才能找到 arbcore 包
arbcore_parent = os.path.normpath(os.path.join(backend_dir, "..", ".."))
arbcore_dir = os.path.join(arbcore_parent, "arbcore")
if os.path.exists(arbcore_dir):
    sys.path.insert(0, arbcore_parent)
    logger.info(f"使用 arbcore 目录: {arbcore_dir} (父目录: {arbcore_parent})")
else:
    # 降级：尝试使用 backend/core
    fallback_dir = os.path.join(backend_dir, "core")
    if os.path.exists(fallback_dir):
        sys.path.insert(0, fallback_dir)
        logger.warning(f"arbcore 目录不存在，使用降级目录: {fallback_dir}")
    else:
        raise RuntimeError(f"既找不到 {arbcore_dir}，也找不到 {fallback_dir}")

# 1. [V3.11 统一数据库路径]
# [AI-2026-08-17] 用与 DatabaseManager 一致的 _resolve_db_path 定位项目根，
#   根治 ARM 部署无 src/ 层导致 '../..' 算错、连到 /home/ubuntu/database 空库的 bug
from arbcore.database.db_manager import _resolve_db_path, check_master_db
root_db_path = _resolve_db_path()
# [AI-2026-08-16] 交易数据独立库：成交/配对/策略规则等敏感交易数据与市场因子分离，隐私保护
tran_db_path = os.path.join(os.path.dirname(root_db_path), "arb_tran.db")
logger.info(f"📂 Using database at {root_db_path}")
# [AI-2026-10-10] 交易库已剥离至 LOFTrader，删除启动日志（tran_db 路径变量保留，ledger_service 仍使用）
# 🔴 [AI-2026-10-15 东哥定调] ⚠️ 本路径**仅 full 模式（交易机）使用**；DASHBOARD_MODE(ARM/云端)
#   一律走内存库，绝不在该路径上创建文件 —— 护栏见下方 tran_db 实例化处（修红线#6）。

# [AI-2026-10-07] 主库自检（防呆）：必须跑在下方 DatabaseManager 实例化**之前** ——
# 一旦路径解析失误，os.makedirs + sqlite3.connect（默认 READWRITE|CREATE）会先静默造出
# 一个 0 字节空库，那时再自检就晚了。挡住两类事故：
#   ① 主库文件缺失（目录搬迁 / ARB_MASTER_DB 未设 / 布局变化）
#   ② 主库文件存在但为空库/错库 —— 后者是 10-06「分类标签全丢」的真实形态，
#      文件被造出来后「存在」了，任何 os.path.exists 检查都会放行。
# 默认 fail-fast 拒绝启动（空库跑起来 = 错的估值与溢价）；应急降级：设 ARB_DB_GUARD=warn 则仅告警。
_db_ok, _db_problems = check_master_db(root_db_path)
if _db_ok:
    logger.info(f"✅ [DB-GUARD] 主库自检通过: {root_db_path}")
else:
    for _p in _db_problems:
        logger.critical(f"🚨 [DB-GUARD] 主库自检失败: {_p}")
    logger.critical(f"🚨 [DB-GUARD] 解析路径: {root_db_path}")
    logger.critical("🚨 [DB-GUARD] 排查方向：ARB_MASTER_DB 环境变量 / _shared 布局 / 是否发生目录搬迁")
    if os.environ.get("ARB_DB_GUARD", "").strip().lower() == "warn":
        logger.critical("⚠️ [DB-GUARD] ARB_DB_GUARD=warn → 仅告警并继续启动（可能连到空库/错库，后果自负）")
    else:
        raise RuntimeError("主库自检失败，拒绝启动：" + "；".join(_db_problems))

# Define project root (ArbDashboard directory)
project_root = workspace_root
logger.info(f"📁 Project root: {project_root}")

try:
    from arbcore.database.db_manager import DatabaseManager
    from services.fund_service import FundService
    from services.config_service import ConfigService
    from services.market_data_service import MarketDataService
    from services.system_status_service import system_status
    # [AI-2026-10-09] 本地分时采样已移除以本地不再自己采（分时由 ARM 采集 → arm_sync_service 同步）
    # 原 import services.intraday.sampler_service 已删（孤儿代码，东哥拍板「彻底删」）
    from services.dashboard_snapshot_service import DashboardSnapshotService
    # [AI-2026-10-08] TradingService 已剥离至 LOFTrader（交易内核不再由 LOFarb 持有）
    from services.config_manager_service import ConfigManagerService
    from services.ledger_service import LedgerService
    from services.etf_rotation_service import ETFRotationService
    from services.holding_service import HoldingService

    # [AI-2026-10-09 东哥拍板] 删掉恒 None 的死变量 `auto_trade_runner`（2026-08-22 起
    #   旧文件型信号引擎已废弃，策略引擎归 LOFTrader :8001 的 rule_engine 独占）。
    #   原写法 `auto_trade_runner = None` + lifespan 里 `if auto_trade_runner: ...stop()`
    #   是永假分支，留着只会让人误以为 LOFarb 还持有自动交易。

    logger.info("Core modules imported successfully")
except Exception as e:
    logger.error(f"Failed to import core modules: {e}")
    raise

# 2. Initialize Database Manager FIRST
# [V3.11][AI-2026-08-16] 统一数据库路径（相对 workspace_root 推导，项目下移 src/ 后仍有效）
db = DatabaseManager(db_path=root_db_path, include_trading=False)
# [AI-2026-08-16] 交易独立库（含 user_trades/broker_redemption_fees/arbitrage_pairs/fund_fees 交易表）
# 🔴 [AI-2026-10-15 东哥定调 · 修红线#6「arb_tran.db 绝不得出现在 ARM」]
#   交易库只在**交易机**（full 模式）存在。ARM/云端看板是纯展示副本，根本不存在交易数据，
#   故这里不再实例化磁盘交易库 —— 掐断「启动即 os.makedirs + sqlite3.connect ⇒ 静默造出一个
#   空 arb_tran.db」这条自建链（ARM 上 10-11 那份 221KB 遗留正是此产物，且删了必被重建）。
#   ARM 侧改用**内存库**句柄：不落盘、无数据，读行为与"空交易库"完全一致
#   （实测 get_fee_rate→0 / get_prev_close→0 / _get_usd_rate→经 ATTACH 主库正常出价）。
#   写路径本就进不来：/api/ledger/* 与 /api/config/* 已被 _is_dashboard_denied 全线 403。
if DASHBOARD_MODE:
    tran_db = DatabaseManager(db_path=':memory:', include_trading=True)
    logger.info("🛡️ [DASHBOARD_MODE] 交易库已禁用：改用内存库，不落盘、不创建 arb_tran.db")
else:
    tran_db = DatabaseManager(db_path=tran_db_path, include_trading=True)

def _print_data_source_banners():
    """启动后统一打印各数据源连接状态（清晰的双层提醒标志）并写入里程碑日志"""
    rt = market_data_service.realtime_manager
    active = rt.active_fetchers if rt else {}

    def _is_fetcher_connected(fetcher) -> bool:
        """检查实时行情 fetcher 是否真正已连接（不为 None、未 disabled）"""
        if fetcher is None:
            return False
        if getattr(fetcher, 'disabled', False):
            return False
        if getattr(fetcher, 'is_connected', True) is False:
            return False
        return True

    # [AI-2026-10-10] guojin/galaxy（QMT 交易客户端）已剥离至 LOFTrader，不再显示其连接状态 banner
    sources = [
        ("tdx",    "通达信",  _is_fetcher_connected(active.get("tdx")),
         "请点击左侧'通达信'按钮启动"),
        ("ib",     "IB 盈透证券",
         market_data_service.ib_reader is not None and getattr(market_data_service.ib_reader, 'connected', False),
         "请点击左侧'IB'按钮启动"),
        ("futu",   "富途 OpenD",
         market_data_service.futu_reader is not None and not getattr(market_data_service.futu_reader, 'disabled', True),
         "请点击左侧'富途'按钮启动"),
    ]

    for key, label, available, hint in sources:
        if available:
            logger.info(f"{label} 连接正常")
            system_status.add_milestone("SUCCESS", f"{label} 连接正常")
        else:
            # [AI-2026-10-09 东哥拍板] 未连接属**异常态**（连接正常才是 INFO）
            #   ⇒ 升 WARNING + ⚠️ 前缀，避免刷屏时被当成常态静默滑过
            logger.warning(f"⚠️ {label} 待连接")
            system_status.add_milestone("WARNING", f"{label} {hint}")

# 2. Initialize Services with DB instance
config_service = ConfigService(db)
# [AI-2026-10-08] 交易内核剥离（第 5 步）：TradeManager / TradingService / LazyTrader /
#   RuleEngine / SmartOpenMonitor 全部迁至独立仓库 LOFTrader（后端 :8001 / 前端 :5174）。
#   LOFarb 自此为纯「数据 + 估值 + 看板」节点，恒不挂载任何交易组件 ——
#   同时根治「两进程各持一个 RuleEngine 读同一规则表」的双下单风险。
trading_service = None
# [AI-2026-10-10] 交易内核已剥离至 LOFTrader，删除确认性启动日志
_active_watchlist = []
_nav_last_updated = {"time": None, "date": None}
_nav_scheduled_today_date = ""
_morning_refreshed_today = False
_morning_refresh_time = None
market_data_service = MarketDataService(db)
fund_service = FundService(db, market_data_service=market_data_service, config_service=config_service)
holding_service = HoldingService(db, market_data_service=market_data_service)
# [AI-2026-10-09] 本地分时采样已移除（分时由 ARM 采集 → arm_sync_service 同步），不再实例化 sampler_service
dashboard_snapshot_service = DashboardSnapshotService(
    fund_service,
    market_data_service=market_data_service,
)
config_manager_service = ConfigManagerService(project_root)
# [AI-2026-08-16] 账本服务连交易库 tran，对账时 ATTACH master 读因子
ledger_service = LedgerService(tran_db, master_db_path=root_db_path)
etf_rotation_service = ETFRotationService(db, market_data_service=market_data_service)

def _is_script_running(script_name: str) -> bool:
    """Best-effort process guard for background scripts."""
    try:
        import subprocess
        if sys.platform == "win32":
            output = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Select-Object -ExpandProperty CommandLine"],
                text=True,
                # [AI-2026-10-11] 根治 EReader 幻影：text=True 按 GBK 解码 PowerShell 大输出时
                # 偶发 UnicodeDecodeError，在 subprocess 的 _readerthread 线程抛出（线程名恰好含
                # `_readerthread`），被 ib_reader 的线程异常钩子误判为 IB 协议杂音并误触发断连。
                # errors='replace' 使解码永不失败，本调用只用于进程名匹配，替换字符不影响匹配。
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            output = subprocess.check_output(["ps", "axo", "command"], text=True, timeout=5)
        current_pid = str(os.getpid())
        for line in output.splitlines():
            if script_name in line and current_pid not in line:
                return True
    except Exception as e:
        logger.debug(f"Process guard failed for {script_name}: {e}")
    return False

def _popen_script_once(cmd, cwd, script_name: str) -> bool:
    if _is_script_running(script_name):
        logger.info(f"{script_name} is already running, skip duplicate launch")
        system_status.add_milestone("INFO", f"{script_name} 已在运行，跳过重复启动")
        return False
    subprocess.Popen(cmd, cwd=cwd)
    return True

# [AI-2026-10-10] Private export 插件已剥离至 LOFTrader，移除挂载逻辑（.gitignore 已屏蔽 **/private/，不入库）
# [AI-2026-10-08] LazyTrader 随交易内核剥离至 LOFTrader（交易见 :8001）。
lazy_trader_instance = None

# [AI-2026-10-09 东哥拍板] Lazy Simulator（周末 mock 数据）随交易域剥离至 LOFTrader。
#   该能力本质是「造信号驱动下单演练」，属交易侧；LOFTrader :8001 已实现并被前端调用。
#   LOFarb 前端零调用 ⇒ 此处连同 private/lazy_simulator.py 一并移除（少一个常驻线程）。
lazy_simulator_instance = None

# [AI-2026-10-08] RuleEngine（DB 驱动策略引擎）随交易内核剥离至 LOFTrader。
#   唯一策略引擎现由 LOFTrader :8001 持有 ⇒ 根治双进程读同一规则表的双下单风险。
#   ⚠️ 下面 rule_engine / _smart_* / signal_detector 保留占位：main.py 里还有若干**模块级**
#      端点函数引用它们（删名字会 NameError → 500）。这些端点本属交易域，LOFTrader 已有同名实现，
#      LOFarb 侧不再挂载，功能只从 :5174 进入。彻底删除需连端点一起搬，本轮不做（避免大改）。
rule_engine = None

# [AI-2026-10-08] SmartOpenMonitor 随交易内核剥离至 LOFTrader（私有模式页见 :5174）。
_smart_start = _smart_stop = _smart_status = _smart_update_target = None

# [AI-2026-08-22] SignalDetector 已废弃删除（其僵尸端点 /api/signal_detector/* 一并移除）；
# 唯一策略引擎为 DB 驱动的 rule_engine（见上方加载块）
signal_detector = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ArbTest Backend lifespan...")
    # [2026-08-27] 启动即后台同步当日分时（本地不采，ARM/AMD 采后回传；关注基金 162411/164701/161116）
    try:
        from services.intraday.arm_sync_service import ensure_synced_today
        threading.Thread(target=ensure_synced_today, daemon=True).start()
    except Exception as e:
        logger.warning(f"启动 ARM 分时同步线程启动失败(忽略): {e}")
    try:
        # 1. [核心策略] 启动即运行一次 011 数据更新（异步，不需要通达信）
        # 011只读取历史数据并写入数据库，与通达信实时行情不冲突
        async def run_011_first():
            if sys.platform != "win32":
                logger.info("📊 [Cloud] 云端部署环境，静默跳过 011 本地数据更新任务")
                system_status.add_milestone("INFO", "云端部署，跳过本地数据同步")
                # [AI-2026-08-25] ARM 启动时从 YAML 同步基金列表到 DB，保证独立性
                try:
                    _sync_config_to_db()
                    logger.info("✅ [Cloud] YAML→DB 基金列表同步完成")
                except Exception as e:
                    logger.error(f"❌ [Cloud] YAML→DB 同步失败: {e}")
                return

            # [AI-2026-10-11 东哥拍板] 非交易日（周末/法定假期）**不起 011 子进程**。
            # 背景：启动即跑 011 是「后端一重启就拉一次数据」的入口，但它原先**不看交易日**；
            #   而 daily_updater.run() 的默认分支原会在周末跑一套轻量采集 ⇒ 周日重启后端
            #   就会空跑 + 触发「VPS 今日无数据」噪音。
            #   daily_updater.run() 侧已加同样的交易日门控（双保险）；这里提前拦掉，
            #   连子进程与日志文件都不产生。判据与 daily_updater 同口径
            #   （arbcore.utils.market_calendar.is_trading_day，日历不可用时退回周末判断）。
            try:
                from arbcore.utils.market_calendar import is_trading_day as _is_td_011
                _now_011 = datetime.now()
                try:
                    _td_011 = _is_td_011('A_SHARE', _now_011.date())
                except Exception:
                    _td_011 = _now_011.weekday() < 5
                if not _td_011:
                    logger.info(f"📅 [{_now_011.strftime('%Y-%m-%d')}] 非A股交易日，跳过启动时 011 数据更新")
                    system_status.add_milestone("INFO", "非交易日，跳过 011 数据更新")
                    return
            except Exception as _e_011:
                logger.warning(f"011 交易日门控不可用({_e_011})，按原样启动")

            logger.info("📊 启动时自动运行 011 数据更新任务...")
            system_status.add_milestone("INFO", "启动时自动运行 011 数据更新")
            print("daily_updater 即将启动...")
            # [AI-2026-06-28] 修复：daily_updater.py 在 scheduler/ 下，不在 arbcore/scripts/
            scripts_dir = os.path.normpath(os.path.join(backend_dir, "scheduler"))
            script_path = os.path.join(scripts_dir, "daily_updater.py")
            
            # [V4.1] 尝试多种 Python 路径 — 统一使用 ArbDashboard/.venv
            python_exe_candidates = [
                os.path.normpath(os.path.join(backend_dir, "..", ".venv", "Scripts", "python.exe")),  # ArbDashboard/.venv (正确)
                r"C:\Users\milan\AppData\Local\Programs\Python\Python311\python.exe",  # 系统 Python311
                "python",  # PATH
            ]
            
            python_exe = None
            for candidate in python_exe_candidates:
                if os.path.exists(candidate):
                    python_exe = candidate
                    logger.info(f"✅ 找到 Python: {python_exe}")
                    break
            
            if python_exe and os.path.exists(script_path):
                try:
                    if _popen_script_once([python_exe, script_path], scripts_dir, "daily_updater.py"):
                        logger.info("011 任务已在后台启动 (daily_updater)")
                        system_status.add_milestone("SUCCESS", "011 数据更新任务已启动")
                except Exception as e:
                    logger.error(f"011 任务启动失败: {e}")
                    system_status.add_milestone("ERROR", f"011 任务启动失败: {e}")
            else:
                logger.info(f"ℹ️ 未检测到 011 脚本，跳过自动更新")
                system_status.add_milestone("INFO", "未检测到 011 脚本，跳过自动更新")
        
        asyncio.create_task(run_011_first())

        # [AI-2026-10-10] 分时数据依赖 ARM milestone 已移除（纯噪声，无功能影响）
        # 3. 启动实时行情引擎（延迟10秒，等 011 任务先跑起来）
        # 011 需要 1-2 分钟，通达信可以稍后启动
        await dashboard_snapshot_service.start()
        system_status.add_milestone("SUCCESS", "Dashboard 快照服务已启动")

        async def start_mds_later():
            await asyncio.sleep(10)
            try:
                market_data_service.realtime_manager.start()
                logger.info("实时行情引擎已在后台启动")
                system_status.add_milestone("SUCCESS", "实时行情引擎已启动")

                # [AI-2026-08-02] 云端看板：无头环境无前端触发订阅，启动即播种全部基金代码，使实时行情流动
                # [AI-2026-09-22] 美股订阅清单统一走 MarketDataService._get_futu_symbols() 单一出口，
                # 不再在此本地重复实现。旧写法两处都踩坑：
                #   ① 读 data_source_config.ib_config.whitelist —— 该行是 2026-06-11 旧架构的遗留副本
                #      （写入路径 config_service.update_ib_symbols 已于 2026-06-12 删除，值冻结在
                #       2026-06-10 的 6 只），权威早已迁至 yaml ib_core_symbols；
                #   ② 篮子 SQL 把列名写成 symbol（库实为 underlying_symbol）→ 恒抛异常被 except 吞掉。
                #   两者叠加 ⇒ 云端长期只订到 6 只，缺 SLV/XBI/VGT/XLY/KWEB，IB 真出问题切不过去。
                if os.environ.get('ARB_DASHBOARD_MODE', '0') == '1':
                    try:
                        import sqlite3
                        _con = sqlite3.connect(root_db_path)
                        _lof = [r[0] for r in _con.execute("SELECT fund_code FROM unified_fund_list")]
                        _con.close()
                        _us = market_data_service._get_futu_symbols()
                        market_data_service.realtime_manager.subscribe(_lof)
                        if market_data_service.futu_reader and not getattr(market_data_service.futu_reader, 'disabled', True):
                            market_data_service.futu_reader.get_prices(_us)
                        logger.info(f"[DASHBOARD_MODE] 已播种实时订阅: A股{len(_lof)}只, 美股ETF{len(_us)}只")
                    except Exception as e:
                        logger.warning(f"[DASHBOARD_MODE] 实时订阅播种失败: {e}")
            except Exception as e:
                logger.error(f"实时行情引擎启动失败: {e}")
                system_status.add_milestone("ERROR", f"实时行情引擎启动失败: {e}")

            # 延迟获取各数据源连接状态，确保所有异步初始化已完成
            await asyncio.sleep(2)
            _print_data_source_banners()
            
            # [V10.0] 启动完成提示：引导用户手动连接需要的券商客户端
            system_status.add_milestone("INFO", "💡 如需实时行情，请点击对应按钮连接券商客户端")
            
            # [AI-2026-08-20 东哥口径] 本地模式启动绝不自动连接 IB（恢复 V10.0 手动语义：
            # 用户点击侧边 IB 按键才连接；云端 ARB_DASHBOARD_MODE=1 由 market_data_service 守护线程负责自动连接）。
            if os.environ.get('ARB_DASHBOARD_MODE', '0') == '1':
                try:
                    if market_data_service.ib_reader:
                        success, msg = market_data_service.ib_reader.reconnect()
                        if success:
                            logger.info(f"✅ IB Gateway 自动连接成功")
                            system_status.add_milestone("SUCCESS", "IB Gateway 自动连接成功")
                        else:
                            logger.info(f"ℹ️ IB Gateway 自动连接跳过: {msg}")
                            system_status.add_milestone("INFO", f"IB Gateway 未检测到: {msg}")
                except Exception as e:
                    logger.warning(f"IB 自动连接异常: {e}")
        
        asyncio.create_task(start_mds_later())

        # [AI-2026-10-09 东哥拍板] SignalDetector 已废弃删除（2026-08-22），
        #   下方 `signal_detector = None` 恒为 None ⇒ 这段 if/else 是**永假的死分支**，
        #   每启必刷两条「未加载」噪音（其中一条还塞进用户可见的 System Milestone）。
        #   策略引擎现由 LOFTrader(:8001) 的 rule_engine 独占（避免双进程双下单）。
        #   ⇒ 整段删除，不再打这两条日志。

        # 5. 定义脚本路径和 Python 查找的公共函数
        def _get_scripts_dir():
            # [AI-2026-06-28] daily_updater.py 在 scheduler/ 下
            return os.path.normpath(os.path.join(backend_dir, "scheduler"))
        def _find_python():
            # [AI-2026-08-17] 跨平台稳健版：优先复用当前进程解释器（main.py 已被 .venv 护栏锁定，
            # sys.executable 必为装齐 apscheduler/ibapi/xtquant 依赖的 venv python），Windows/Linux/macOS 通用，零路径猜测。
            if getattr(sys, 'executable', None) and os.path.exists(sys.executable):
                return sys.executable
            # 兜底（极少触发）：跨平台查找 venv / 系统 python
            import shutil
            for sub in ("Scripts", "bin"):
                cand = os.path.normpath(os.path.join(backend_dir, "..", ".venv", sub, "python.exe" if os.name == 'nt' else "python"))
                if os.path.exists(cand):
                    return cand
            for cmd in ("python3", "python"):
                found = shutil.which(cmd)
                if found:
                    return found
            return None
        def _run_daily_updater(args_list):
            sd = _get_scripts_dir()
            sp = os.path.join(sd, "daily_updater.py")
            pe = _find_python()
            if pe and os.path.exists(sp):
                return _popen_script_once([pe, sp] + args_list, sd, "daily_updater.py")
            return False

        # 6. [V9.0] 9:20 清晨自动刷新 Woody/汇率/VPS
        # [AI-2026-08-02] 触发时刻可配：ARB_MORNING_REFRESH_AT="HH:MM"，默认 09:20。
        # 场景：ARM 展示副本只从东京 VPS 读现成产物，必须等东京自己的采集任务（约 08:30/09:20）
        #      全部落盘之后再去拉，否则会拉到半成品。故 ARM 上设 09:35。
        _mr_at = os.environ.get('ARB_MORNING_REFRESH_AT', '09:20')
        try:
            _MR_H, _MR_M = (int(x) for x in _mr_at.split(':'))
        except Exception:
            logger.warning(f"⚠️ [清晨刷新] ARB_MORNING_REFRESH_AT='{_mr_at}' 格式错误，回退 09:20")
            _MR_H, _MR_M = 9, 20

        global _morning_refreshed_today, _morning_refresh_time
        async def morning_refresh_scheduler():
            global _morning_refreshed_today, _morning_refresh_time
            while True:
                await asyncio.sleep(300)
                now = datetime.now()
                # [AI-2026-10-12 东哥拍板] 仅周日跳过，周六放行——VPS crontab 每天跑，
                # 周五美股数据必须周六清晨收（daily_updater 门控会走 _weekend_vps_pull_only）。
                if now.weekday() == 6:
                    _morning_refreshed_today = False
                    continue
                today = now.strftime("%Y-%m-%d")
                if _morning_refreshed_today and today != _morning_refresh_time:
                    _morning_refreshed_today = False  # 新的一天
                if not _morning_refreshed_today and now.hour >= _MR_H and (now.hour > _MR_H or now.minute >= _MR_M):
                    _morning_refreshed_today = True
                    _morning_refresh_time = today
                    logger.info(f"⏰ [清晨刷新] 自动触发 --refresh-morning (Woody/汇率/VPS) @ {_MR_H:02d}:{_MR_M:02d}")
                    system_status.add_milestone("INFO", f"⏰ {_MR_H:02d}:{_MR_M:02d} 自动清晨数据刷新")
                    if _run_daily_updater(["--refresh-morning"]):
                        logger.info("✅ [清晨刷新] 已启动 --refresh-morning")
                    else:
                        logger.warning("⚠️ [清晨刷新] 启动失败")

        # [AI-2026-08-02] 总开关：设 ARB_NO_DAILY_UPDATER=1 可完全不注册清晨刷新定时器。
        # 用途：某个部署副本不需要自己触发 daily_updater（例如纯展示副本、或由外部 cron 统一调度）。
        # 注意：ARM 已配通东京 VPS 后，9:20 清晨刷新是其静态因子的唯一自动更新入口，
        #      在 ARM 上开启此开关会让静态数据重新变死，请谨慎。
        if os.environ.get('ARB_NO_DAILY_UPDATER', '0') == '1':
            logger.info("⏸️ [清晨刷新] 已被 ARB_NO_DAILY_UPDATER=1 禁用，定时器未注册")
        else:
            asyncio.create_task(morning_refresh_scheduler())
            logger.info(f"⏰ [清晨刷新] 定时器已注册 ({_MR_H:02d}:{_MR_M:02d} 自动刷新 Woody/汇率/VPS)")

        # 7. [V9.0] 净值定时更新：下午 18:00 / 19:30 / 21:00 自动补跑 step4
        global _nav_last_updated, _nav_scheduled_today_date
        _nav_slot_done = set()
        
        async def nav_update_scheduler():
            global _nav_last_updated, _nav_scheduled_today_date
            run_at = ["18:00", "19:30", "21:00"]
            while True:
                await asyncio.sleep(300)
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.weekday() in (5, 6):
                    _nav_slot_done.clear()
                    continue
                if today != _nav_scheduled_today_date:
                    _nav_scheduled_today_date = today
                    _nav_slot_done.clear()
                    _nav_last_updated = {"time": None, "date": None}
                hm = now.strftime("%H:%M")
                for slot in run_at:
                    if slot not in _nav_slot_done and hm >= slot:
                        _nav_slot_done.add(slot)
                        logger.info(f"⏰ [自动净值更新] 触发定时 {slot} 净值更新...")
                        system_status.add_milestone("INFO", f"⏰ 定时净值更新 ({slot})")
                        if _run_daily_updater(["--nav-only"]):
                            _nav_last_updated["time"] = now.strftime("%H:%M")
                            _nav_last_updated["date"] = today
                            logger.info(f"✅ [自动净值更新] 定时 {slot} 已启动 --nav-only")
                        else:
                            logger.warning(f"⚠️ [自动净值更新] 启动失败")
        
        asyncio.create_task(nav_update_scheduler())
        logger.info("⏰ [自动净值更新] 定时器已注册 (18:00 / 19:30 / 21:00)")

        # 7.5 [2026-10-08 移除] 原「收盘后数据流水线定时（15:35 自动 daily-close → export → upload）」。
        #   东哥确认该功能已取消、不再需要（deploy/H5web/daily_close_pipeline.py 已不存在）⇒ 整段删除：
        #   此前它 15:35 因脚本缺失每天静默告警，且引用的还是 deploy/ 迁移前的旧布局路径（src/deploy/…）。
        #   注：_popen_script_once / _find_python 仍被上方净值更新等处复用，定义保留。

    except Exception as e:
        logger.error(f"❌ Failed during backend startup: {e}")
        system_status.add_milestone("ERROR", f"系统启动自检异常: {e}")

    # 4.6 [2026-10-09 东哥拍板 · 冻结机制已整体取消] 原「实时估值冻结调度器」（每日 15:00 后
    # 把 rt_val/rt_premium 快照到 database/rt_freeze.json）整段删除：程序估值与时间无关，
    # 行情展示走数据驱动（有真实盘口→显示，无数据→"等待数据"），禁止 15:00 快照覆盖 live 值。
    # rt_freeze.json / rt_cache.json 停止写入（历史文件保留）；rt_frozen 字段恒不写入。

    # 4.7 [2026-08-20] 盘后收盘价兜底调度器：时间 ≥15:00 且今天 price 为 NULL 时，
    # 调 daily_updater 写今天官方收盘。解决主看板盘后显示"昨天收盘"的问题，无需等 15:35 流水线。
    # [AI-2026-08-20] 修复：① 原插入位置破坏旧 4.6 冻结调度器的 try/except 结构（SyntaxError）；
    # ② 原实现同步调 _DU()._step4_fetch_prices() 在 asyncio 事件循环内阻塞数分钟（腾讯K线
    # 每只 12s 超时 × 数十只），导致 uvicorn 全接口超时。改为 _popen_script_once 异步子进程
    # 启动 daily_updater --daily-close（与 15:35 流水线同款，不阻塞事件循环）。
    # [AI-2026-10-09] _dt import 下移至此（原属 4.6 冻结块，冻结已取消但下方循环仍用 _dt.now()）。
    try:
        from datetime import datetime as _dt
        # [AI-2026-10-03 东哥点名修复] 复用 A股交易日历（chinese_calendar + _HOLIDAYS_2026 兜底）。
        # 仅用于下方盘后兜底循环的「非交易日」门控；取不到时降级 None（退回仅周末过滤），
        # 绝不影响其他功能。与 daily_updater.py 同口径（arbcore.utils.market_calendar）。
        try:
            from arbcore.utils.market_calendar import is_trading_day as _is_cn_trading_day
        except Exception as _e:
            _is_cn_trading_day = None
            logger.warning(f"[AFTER_HOURS] A股交易日历不可用，退回仅周末过滤: {_e}")

        def _count_today_close_prices(_db, _date):
            """[AI-2026-09-02] 同步 DB 查询，只供 asyncio.to_thread 调用。
            绝不能在 asyncio 事件循环（MainThread）里直接连 SQLite —— 一旦等锁，
            整个 uvicorn 全接口超时（loop-watchdog 抓到的 60s 卡死即由此而来）。

            [AI-2026-10-09 方案B·东哥拍板] 返回 (covered, expected) 覆盖率判据：
            - covered = unified_fund_list 中今日已写官方收盘价(price非空)的只数；
            - expected = unified_fund_list 只数 − 其中「真·无日K线」跳过名单只数。
            原判据「今日任意 price 非空行数 > 0 即完成」会被 159985/161226 期货镜像行
            （盘后 16:36 落库）提前满足 → 其余 34 只缺价也永不重试（2026-10-09 事故根因之一）。
            改按名单覆盖率判定：2 条镜像行凑不齐 36 只覆盖 ⇒ 照样触发补写；
            真·无K线基金从 expected 中剔除，避免凑不齐导致盘后无限重拉。"""
            _conn = _db._get_conn()
            try:
                _list_codes = {str(r[0]) for r in _conn.execute(
                    "SELECT fund_code FROM unified_fund_list").fetchall() if r[0]}
                _covered = _conn.execute(
                    "SELECT COUNT(*) FROM unified_fund_history WHERE date=? AND price IS NOT NULL"
                    " AND fund_code IN (SELECT fund_code FROM unified_fund_list)",
                    (_date,)).fetchone()[0]
            finally:
                _conn.close()
            _denied = 0
            try:
                import json as _json
                _dl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        '..', '..', 'arbcore', 'config', 'no_kline_funds.json')
                if os.path.exists(_dl_path):
                    with open(_dl_path, 'r', encoding='utf-8') as _f:
                        _denied = len(set(_json.load(_f).keys()) & _list_codes)
            except Exception:
                _denied = 0  # 名单读不了按 0 算（等价于全量覆盖判定，宁可多重试也不误判完成）
            return _covered, max(len(_list_codes) - _denied, 0)

        async def _after_hours_close_price_loop():
            """[AI-2026-08-20] 盘后收盘价兜底：15:00后每天自动写今天官方收盘。
            使用DB检查保证幂等（重启/崩溃不重复写）；子进程异步执行不阻塞事件循环。

            [AI-2026-10-11 方案A·东哥拍板] 两条节流：
              ① 轮询间隔 30s → **300s**（盘后收盘价不需要 30s 级敏捷度）；
              ② 新增进程内 `_ah_done` 记忆：**一旦拉齐当天就不再查 DB**（跨天自动失效）。
            效果：DB 查询从「一天约 2880 次」降到「拉齐前每 5 分钟 1 次、拉齐后 0 次」。
            """
            _ah_done = set()   # [方案A] 已完成日期（进程内）；进程重启后重查一次，无害
            while True:
                await asyncio.sleep(300)   # [AI-2026-10-11 方案A] 30s → 300s
                _n = _dt.now()
                if _n.weekday() in (5, 6) or _n.hour < 15:
                    continue
                # [AI-2026-10-03 东哥点名修复] A股法定假期（国庆/春节等）全市场无场内价 ⇒ 下方
                # 「今天 price 非空行数 > 0」判据恒为 0 ⇒ 每 30s 冷启动一次 daily_updater
                # --daily-close、永不退出（10-01 实测耗尽全部 1080 个槽位/天、单日日志放大 60 倍）。
                # 与 daily_updater.py 同口径复用 is_trading_day('A_SHARE')。
                if _is_cn_trading_day is not None and not _is_cn_trading_day('A_SHARE', _n.date()):
                    continue
                # 幂等检查：DB中今天是否已有收盘价？
                _today = _n.strftime('%Y-%m-%d')
                if _today in _ah_done:      # [方案A] 当天已拉齐 → 不再查 DB、不再起子进程
                    continue
                try:
                    # [AI-2026-09-02] DB 查询放线程，避免阻塞事件循环
                    # [AI-2026-10-09 方案B] 返回 (covered, expected) 覆盖率二元组
                    _covered, _expected = await asyncio.to_thread(_count_today_close_prices, db, _today)
                except Exception as _e:
                    logger.error(f"[AFTER_HOURS] DB检查失败: {_e}")
                    continue
                if _expected > 0 and _covered >= _expected:
                    _ah_done.add(_today)    # [方案A] 已拉齐 → 记入，当天不再查
                    continue  # 覆盖率已齐（余下均为真·无日K线），跳过
                # 未齐（含镜像行冒充场景）→ 异步子进程触发写入（不阻塞 uvicorn 事件循环）
                try:
                    # [AI-2026-09-24 修 bug] 原实现硬编码 Windows 路径 ".venv/Scripts/python.exe"，
                    # 在 ARM(Linux) 上每 30s 抛 FileNotFoundError → daily_updater --daily-close 从未跑起来
                    # → _step_oil_recalc 唯一触发点失效。改用同作用域既有的跨平台 _find_python()。
                    _pe = _find_python()
                    _sd = os.path.normpath(os.path.join(backend_dir, "scheduler"))
                    _du_py = os.path.join(_sd, "daily_updater.py")
                    if not (_pe and os.path.exists(_du_py)):
                        logger.warning(f"[AFTER_HOURS] 未找到 Python 或脚本，跳过本轮: pe={_pe} script={_du_py}")
                        continue
                    if _popen_script_once([_pe, _du_py, "--daily-close"], _sd, "daily_updater.py"):
                        logger.info(f"[AFTER_HOURS] 盘后收盘价兜底已异步启动 daily_updater --daily-close ({_today} 覆盖 {_covered}/{_expected})")
                    else:
                        logger.warning(f"[AFTER_HOURS] 盘后收盘价兜底启动被拒（可能已在运行）({_today})")
                except Exception as _e:
                    logger.error(f"[AFTER_HOURS] 启动盘后收盘价任务失败: {_e}")

        asyncio.create_task(_after_hours_close_price_loop())
        logger.info("[AFTER_HOURS] 盘后收盘价兜底调度器已启动（每日 15:00 后异步子进程写入，asyncio 零依赖）")
        system_status.add_milestone("SUCCESS", "盘后收盘价兜底调度器已启动")

    except Exception as e:
        logger.warning(f"⚠️ [AFTER_HOURS] 盘后收盘价兜底调度器启动失败: {e}")

    # [AI-2026-08-25] 事件循环卡死自曝 watchdog（纯诊断，零业务逻辑）
    # 背景：IB/银河QMT 手动重连后 ~25s 事件循环被同步调用堵死（HTTP 全超时），进程随后无声崩溃。
    # 卡死 >15s 时自动把全线程栈写入 logs/watchdog_stall_*.txt，供定位阻塞行号。严禁删除——抓到根因前这是唯一现场取证手段。
    try:
        from services.loop_watchdog import loop_watchdog
        loop_watchdog.start(asyncio.get_running_loop(), logger, logs_dir)
    except Exception as e:
        logger.warning(f"⚠️ [loop-watchdog] 启动失败（不影响业务）: {e}")

    yield

    # [AI-2026-08-03] 各调度器已改为 asyncio 任务（随进程退出自动回收），无需显式 shutdown
    # [AI-2026-10-09] 原冻结调度器已随冻结机制整体取消（见上方 4.6 注释）
    logger.info("🛠️ Shutting down ArbTest Backend...")
    await dashboard_snapshot_service.stop()
    # [AI-2026-10-09] 本地分时采样已移除，无 sampler_service 可 stop（分时由 ARM 采集 → arm_sync_service 同步）
    # [AI-2026-10-09] auto_trade_runner / signal_detector 两个恒 None 死变量已删（见上方 import 块注释）
    market_data_service.realtime_manager.stop()
    # [AI-2026-07-13] 先 stop_polling() 再 disconnect()，防止 polling 线程抢重连
    if market_data_service and market_data_service.ib_reader:
        market_data_service.ib_reader.stop_polling()
        market_data_service.ib_reader.disconnect_from_ib()

# [AI-2026-10-07] JSON 序列化兜底：把 NaN / +Inf / -Inf 递归转成 null 再交给序列化。
#   根因：Starlette 的 JSONResponse.render 用 json.dumps(..., allow_nan=False)，
#   返回体里只要有**任一非有限浮点**就抛
#     ValueError: Out of range float values are not JSON compliant
#   → 端点 500（且不打印 Python 栈，前端只见 "Internal Server Error"，极难定位）。
#   真实来源：SQLite 的 NULL 列经 pandas 读出变 NaN（典型：/api/fund/<code>/intraday
#   的 open_premium / close_premium / lof_bid1 / etf_bid1 等盘口列，未成交时段为空）。
#   ⚠️ 这是**类型层面的根治**（一处生效、覆盖全部端点），而非逐个端点打补丁 ——
#      未来任何返回 DataFrame/含 NaN 数据的端点都不会再因此 500。
def _json_sanitize(_obj):
    """把非有限浮点(NaN/±Inf)递归替换为 None，其余原样返回。"""
    if isinstance(_obj, float):
        # NaN 不等于自身；±Inf 用等值判断（不额外依赖 math 模块）
        if _obj != _obj or _obj == float("inf") or _obj == float("-inf"):
            return None
        return _obj
    if isinstance(_obj, dict):
        return {_k: _json_sanitize(_v) for _k, _v in _obj.items()}
    if isinstance(_obj, (list, tuple)):
        return [_json_sanitize(_v) for _v in _obj]
    return _obj


class _SafeJSONResponse(JSONResponse):
    """默认响应类：先清洗非有限浮点，再走标准 JSON 序列化。"""

    def render(self, content) -> bytes:
        return super().render(_json_sanitize(content))


# [AI-2026-10-07] 云端看板节点关闭接口文档面（/docs /redoc /openapi.json）。
#   本机（full 模式）保留，便于开发调试；ARM（DASHBOARD_MODE=1）一律 None ⇒ 404。
_docs_kwargs = ({"docs_url": None, "redoc_url": None, "openapi_url": None}
                if DASHBOARD_MODE else {})
app = FastAPI(title="ArbTest API", version="1.0.0", lifespan=lifespan,
              default_response_class=_SafeJSONResponse, **_docs_kwargs)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================================================
# [AI-2026-10-07] 纵深防御：看板节点（DASHBOARD_MODE=1）在**后端**再拒一层私有/写端点。
#   背景：nginx 是第一道（公网侧）；这里是第二道 —— 防「nginx 被误改 / 直连 8000 / 未来
#   有人放开安全组」时私有端点裸奔。本机（full 模式）不生效，不影响东哥本地功能。
#   ⚠️ 与 nginx 口径**必须一致**：/api/tokyo/admin、/api/tokyo/status、/api/tokyo/diff
#      是「东京维护面板页面 + 其数据源」，**故意不入拦截名单**；只拦 POST /api/tokyo/repush。
#      详见 server-docs/093_6 §11.1（东哥第 1 组纠正：tokyo 严禁整前缀封）。
# ==============================================================
_DASHBOARD_DENY_PREFIXES = (
    "/api/db/", "/api/private/", "/api/ledger/", "/api/rule_engine/", "/api/config/",
    "/api/system/", "/api/sync/", "/api/bond/", "/api/etf-rotation/",
    "/api/trading/", "/api/penetration/", "/api/oil/",
)
_DASHBOARD_DENY_EXACT = {
    "/api/tokyo/repush",
    "/api/fund/sync-freeze", "/api/fund/sync-usa-etf",
    "/api/fund/oil-refetch-prices", "/api/fund/oil-price-freshness",
}
# [AI-2026-10-08 东哥拍板·修复安全加固误伤] 移出 "/holding-recalc-detail"：
# 该端点是**只读诊断**（返回各腿两日 ETF 收盘价/两日汇率原值/权重/前填标注），
# 10-07 那批加固按"公开页 0 引用"把它与写端点一起封了，导致本机前端（full 模式）
# 经 `_arm_api_via_ssh` 代理拿 ARM 时被 403 顶回（ARM 是 DASHBOARD_MODE）⇒
# 持仓静态估值的展开明细**整片空白**、且「🔸沿用上一交易日收盘价」的前填警告一并消失。
# 放开是安全的：① 它只读、无写；② 公网面由 nginx `arbmini.conf` 的 403 独立把守
# （SSH 代理走 ARM 回环 8000，不经 nginx）⇒ 公网不可达、本机代理可达，各得其所。
_DASHBOARD_DENY_FUND_SUFFIX = (
    "/basket", "/history", "/holdings", "/holding-periods", "/holding-valuation",
    "/holding-realtime", "/hedge-exposure", "/valuation_meta",
    "/reconcile_static_val", "/sync-oil-static",
)


def _is_dashboard_denied(path: str) -> bool:
    """True = 该路径在 DASHBOARD_MODE 下应被后端拒绝。"""
    if not DASHBOARD_MODE:
        return False
    if path in _DASHBOARD_DENY_EXACT:
        return True
    for _p in _DASHBOARD_DENY_PREFIXES:
        if path.startswith(_p):
            return True
    if path.startswith("/api/fund/") and path.endswith(_DASHBOARD_DENY_FUND_SUFFIX):
        return True
    return False


@app.middleware("http")
async def _dashboard_guard(request: Request, call_next):
    if _is_dashboard_denied(request.url.path):
        logger.warning(f"[DASHBOARD_GUARD] 拒绝 {request.method} {request.url.path}")
        return JSONResponse(status_code=403, content={
            "status": "error", "message": "此端点在云端看板节点（DASHBOARD_MODE）已被禁用"})
    return await call_next(request)

# [AI-2026-07-25] 已删除 /api/health 健康检查端点（方案C：该健康检查功能无调用方，华而不实）

# [V6.0] 存储前端传递的最新自选基金列表（用于采样服务过滤）
# (已在服务初始化前定义)

# [AI-2026-07-13] 导出十天历史数据库（供分享/分析用）
@app.get("/api/db/export_share")
async def export_share_db():
    """生成最新 10 天的 arb_master_share.db 并返回下载"""
    import sqlite3
    from datetime import datetime, timedelta
    import tempfile
    import shutil

    src = root_db_path
    if not os.path.exists(src):
        return JSONResponse(status_code=404, content={"status": "error", "message": "源数据库不存在"})

    cutoff = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    tmp_path = os.path.join(tempfile.gettempdir(), f"arb_master_share_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")

    # 日期过滤映射: {表名: (日期列名: str | None)}，None 表示全部复制
    # [AI-2026-08-16] 移除交易/策略表(auto_trade_rules/broker_redemption_fees/fund_fees/user_trades)，
    # 交易数据已迁 arb_tran.db，share 库只含净值/估值/溢价等市场数据，杜绝隐私泄露。
    date_filter_map = {
        'access_sync_status': 'sync_date',
        'app_settings': None,
        'data_source_config': None,
        'etf_raw_api_data': None,
        'etf_rotation_list': None,
        'exchange_rate': 'date',
        'fund_basket_weights': 'date',
        'fund_daily_factors': 'date',
        'fund_purchase_status': None,        # 全部保留（金额配置）
        'futures_daily': 'date',
        'index_history': 'date',
        'index_realtime_quotes': None,
        'raw_api_data': 'date',
        'unified_fund_history': 'date',
        'unified_fund_list': None,
        'usa_etf_daily_prices': 'date',
    }

    try:
        conn_src = sqlite3.connect(src)
        conn_dst = sqlite3.connect(tmp_path)
        cur = conn_src.cursor()

        tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        total = 0

        for (t,) in tables:
            if t == 'sqlite_sequence':
                continue
            if t not in date_filter_map:
                continue

            # 建表
            sql = cur.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{t}'").fetchone()
            if not sql or not sql[0]:
                continue
            conn_dst.execute(sql[0])

            # 用 PRAGMA table_info 获取列数（避免 cur.description 被前面的查询覆盖）
            col_info = cur.execute(f"PRAGMA table_info({t})").fetchall()
            n_cols = len(col_info)
            placeholders = ','.join(['?'] * n_cols)

            date_col = date_filter_map[t]
            if date_col is None:
                rows = cur.execute(f"SELECT * FROM {t}").fetchall()
                conn_dst.executemany(f"INSERT INTO {t} VALUES ({placeholders})", rows)
                cnt = len(rows)
            else:
                rows = cur.execute(f"SELECT * FROM {t} WHERE {date_col} >= ?", (cutoff,)).fetchall()
                conn_dst.executemany(f"INSERT INTO {t} VALUES ({placeholders})", rows)
                cnt = len(rows)

            total += cnt
            logger.info(f"  {t}: {cnt} rows{' (filtered)' if date_col else ' (all)'}")

        conn_dst.commit()
        conn_src.close()
        conn_dst.close()

        logger.info(f"✅ 分享库已生成: {tmp_path} ({total} rows, cutoff={cutoff})")

        # 返回文件下载
        return FileResponse(
            tmp_path,
            media_type="application/octet-stream",
            filename=f"arb_master_share_{datetime.now().strftime('%Y%m%d')}.db",
            headers={"Content-Disposition": f"attachment; filename=arb_master_share_{datetime.now().strftime('%Y%m%d')}.db"}
        )
    except Exception as e:
        logger.error(f"❌ 生成分享库失败: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/dashboard")
async def get_dashboard(watchlist: str = None, category: str = None):
    """Unified dashboard data for both LOF and JSL
    Reads a background snapshot so UI polling never performs slow valuation
    work inline.
    """
    try:
        import traceback
        requested_watchlist = [code.strip() for code in watchlist.split(',') if code.strip()] if watchlist else None
        snapshot = dashboard_snapshot_service.get_snapshot(
            watchlist=requested_watchlist,
            category=category,
        )
        return {
            "status": "ok",
            "data": snapshot.get("data", []),
            "updated_at": snapshot.get("updated_at"),
            "stale": snapshot.get("stale", False),
            "source_status": snapshot.get("source_status", {}),
            "compute_ms": snapshot.get("compute_ms", 0),
            "error": snapshot.get("error"),
        }
    except Exception as e:
        msg = f"Dashboard API Error: {e}"
        logger.error(msg)
        logger.error(traceback.format_exc())  # 添加详细堆栈跟踪
        system_status.add_milestone("ERROR", msg)
        return JSONResponse(status_code=500, content={"status": "error", "message": msg})

@app.get("/api/status")
async def get_status():
    """运维状态接口（东哥专用，不改东京程序）。
    返回：运行模式、估值快照时间、各行情源健康、上次推送东京时间、运行时长。
    用途：直接 curl <本机或ARM>:8000/api/status 即可知行情从哪来、新不新、对不对。
    """
    try:
        snapshot = dashboard_snapshot_service.get_snapshot()
        sources = snapshot.get("source_status", {}) or {}
        # 方案甲：ARM 部署目录的 .last_push 记录上次推送东京时间（本地主程序无此文件则为 None）
        last_push = None
        for cand in [
            os.path.expanduser("~/arbtest/deploy/.last_push"),
            os.path.join(repo_root, "deploy", ".last_push"),
        ]:
            try:
                if os.path.exists(cand):
                    with open(cand) as fh:
                        last_push = fh.read().strip()
                    break
            except Exception:
                pass
        return {
            "status": "ok",
            "mode": "DASHBOARD_MODE" if DASHBOARD_MODE else "full",
            "dashboard_updated_at": snapshot.get("updated_at"),
            "stale": snapshot.get("stale", False),
            "sources": sources,
            "last_push_to_tokyo": last_push,
            "uptime_seconds": int(time.time() - START_TIME),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/market/overview")
async def get_market():
    try:
        data = fund_service.get_market_overview(market_data_service=market_data_service)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"Market Overview Error: {e}")
        return {"status": "error", "message": str(e)}

# [AI-2026-10-06] 主看板顶部「环球市场概览」条（同类概览条概念，自有实现）。
# 新浪外盘期货 hf_ 一把抓：黄金/白银/WTI + 标普500/纳斯达克100/日经225（东哥拍板：道琼斯不要）。
# 只返回 最新价+涨跌幅（昨结为基准），不带"收盘/行情/行情时间"等冗余字段。
# 60s 内存缓存：前端随 fetchData 轮询也只打新浪一分钟一次；抓取失败回退旧缓存，绝不出假值（缺字段直接跳过）。
# [AI-2026-10-08 东哥需求] 概览条展示对应合约代码：六项全部走新浪外盘期货 hf_ API，
# 名称→合约对照（后端查询代码即 symbol 字段，去掉 hf_ 前缀即 CME/COMEX 合约根代码）：
#   黄金=GC(COMEX金) 白银=SI(COMEX银) WTI原油=CL(NYMEX原油)
#   标普500=ES(CME E-mini) 纳斯达克100=NQ(CME E-mini) 日经225=NK(CME日元计价)
# 注：纳斯达克100 用的是 NQ **期货**，不是 .ndx/.NDX 现货指数（两者不同源、盘中点位有基差）。
_GLOBAL_OVERVIEW_SYMBOLS = [
    ("commodity", "黄金",        "hf_GC"),
    ("commodity", "白银",        "hf_SI"),
    ("commodity", "WTI原油",     "hf_CL"),
    ("futures",   "标普500",     "hf_ES"),
    ("futures",   "纳斯达克100", "hf_NQ"),
    ("futures",   "日经225",     "hf_NK"),
]
_GLOBAL_OVERVIEW_CACHE: dict = {"ts": 0.0, "data": []}

@app.get("/api/market/global-overview")
async def get_global_overview():
    import requests as _requests
    now = time.time()
    if _GLOBAL_OVERVIEW_CACHE["data"] and now - _GLOBAL_OVERVIEW_CACHE["ts"] < 60:
        return {"status": "ok", "data": _GLOBAL_OVERVIEW_CACHE["data"]}
    symbols = ",".join(s for _, _, s in _GLOBAL_OVERVIEW_SYMBOLS)
    try:
        resp = _requests.get(
            f"http://hq.sinajs.cn/list={symbols}",
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=8, verify=False, proxies={"http": None, "https": None},
        )
        resp.encoding = "gbk"
        raw: dict = {}
        for line in resp.text.strip().split("\n"):
            if "=" not in line or '"' not in line:
                continue
            # [AI-2026-10-06 修] 行形如 var hq_str_hf_GC="..."；按"_"切[-1]只会得到"GC"，
            # 必须按 hq_str_ 前缀切，键才是完整的 hf_GC（修复后首版解析全空 bug）
            key = line.split("=")[0].split("hq_str_")[-1].strip()
            raw[key] = line.split('"')[1].split(",")
        items = []
        for group, name, sym in _GLOBAL_OVERVIEW_SYMBOLS:
            parts = raw.get(sym) or []
            # hf_ 字段（新浪外盘期货）：parts[0]=最新价，parts[7]=昨结算 → 涨跌幅基准
            if len(parts) < 8:
                continue
            try:
                price = float(parts[0])
                base = float(parts[7])
            except (ValueError, TypeError):
                continue
            if price <= 0 or base <= 0:
                continue
            items.append({
                "group": group,
                "name": name,
                "symbol": sym,
                # [AI-2026-10-08 东哥需求] 合约根代码（GC/SI/CL/ES/NQ/NK），前端在名称后跟一个灰色小字
                "code": sym.replace("hf_", ""),
                "price": price,
                "change_pct": round((price / base - 1) * 100, 2),
            })
        if items:
            _GLOBAL_OVERVIEW_CACHE.update(ts=now, data=items)
    except Exception as e:
        logger.error(f"global-overview 抓取失败（回退旧缓存）: {e}")
    return {"status": "ok", "data": _GLOBAL_OVERVIEW_CACHE["data"]}

@app.get("/api/system/milestones")
async def get_system_milestones():
    """获取系统运行里程碑日志"""
    return {"status": "ok", "data": system_status.get_milestones()}

# [AI-2026-08-07] 后台维护页：对比「本地」与「H5(ARM)」各数据源最新日期 + 程序健康。
# 远程脚本通过 ssh arm 在 ARM 上查询其本地库；用临时文件 stdin 传入，避免 Windows(cmd) 下 heredoc 失效。
# 缺失/异常字段一律返回 None，绝不兜底成旧值/常量（SUPREME 铁律）。
_REMOTE_FRESHNESS_SCRIPT = r'''
import sqlite3, json, os
cands = [
    "/home/ubuntu/arbtest/database/arb_master.db",
    os.path.expanduser("~/arbtest/database/arb_master.db"),
    "/home/ubuntu/arbtest/ArbDashboard/../database/arb_master.db",
]
db = None
for c in cands:
    if c and os.path.exists(c):
        db = c
        break
out = {}
try:
    if not db:
        out["_error"] = "ARM DB not found"
    else:
        con = sqlite3.connect(db); cur = con.cursor()
        def mx(s):
            try:
                cur.execute(s); r = cur.fetchone(); return r[0] if r else None
            except Exception:
                return None
        out["lof_price"] = mx("SELECT MAX(date) FROM unified_fund_history")
        out["lof_nav"] = mx("SELECT MAX(nav_date) FROM unified_fund_history")
        out["static_val"] = mx("SELECT MAX(date) FROM unified_fund_history WHERE static_val IS NOT NULL")
        out["us_etf"] = mx("SELECT MAX(date) FROM usa_etf_daily_prices")
        out["fx_mid"] = mx("SELECT MAX(date) FROM exchange_rate")
        out["fx_spot"] = mx("SELECT MAX(updated_at) FROM exchange_rate WHERE usd_cny_spot IS NOT NULL")
        out["futures"] = mx("SELECT MAX(date) FROM futures_daily")
        out["fund_factors"] = mx("SELECT MAX(date) FROM fund_daily_factors")
        out["index_hist"] = mx("SELECT MAX(date) FROM index_history")
        cur.execute("SELECT MAX(sync_date), MAX(sync_time) FROM access_sync_status")
        rr = cur.fetchone()
        out["vps_sync_date"] = rr[0] if rr else None
        out["vps_sync_time"] = rr[1] if rr else None
        con.close()
except Exception as e:
    out["_error"] = str(e)
print(json.dumps(out))
'''


def _build_freshness_report(db_path: str) -> dict:
    """读取单个库各数据源最新日期。缺失/异常字段返回 None（绝不兜底）。"""
    import sqlite3
    out = {}
    try:
        con = sqlite3.connect(db_path); cur = con.cursor()
        def mx(s):
            try:
                cur.execute(s); r = cur.fetchone(); return r[0] if r else None
            except Exception:
                return None
        out["lof_price"] = mx("SELECT MAX(date) FROM unified_fund_history")
        out["lof_nav"] = mx("SELECT MAX(nav_date) FROM unified_fund_history")
        out["static_val"] = mx("SELECT MAX(date) FROM unified_fund_history WHERE static_val IS NOT NULL")
        out["us_etf"] = mx("SELECT MAX(date) FROM usa_etf_daily_prices")
        out["fx_mid"] = mx("SELECT MAX(date) FROM exchange_rate")
        out["fx_spot"] = mx("SELECT MAX(updated_at) FROM exchange_rate WHERE usd_cny_spot IS NOT NULL")
        out["futures"] = mx("SELECT MAX(date) FROM futures_daily")
        out["fund_factors"] = mx("SELECT MAX(date) FROM fund_daily_factors")
        out["index_hist"] = mx("SELECT MAX(date) FROM index_history")
        cur.execute("SELECT MAX(sync_date), MAX(sync_time) FROM access_sync_status")
        rr = cur.fetchone()
        out["vps_sync_date"] = rr[0] if rr else None
        out["vps_sync_time"] = rr[1] if rr else None
        con.close()
    except Exception as e:
        out["_error"] = str(e)
    return out


def _fetch_h5_report_via_ssh() -> tuple:
    """通过 ssh arm 查询 ARM(H5) 库。返回 (report_dict_or_None, error_or_None)。"""
    import tempfile
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(_REMOTE_FRESHNESS_SCRIPT)
            tmp = f.name
        cmd = 'ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o BatchMode=yes arm "python3 -"'
        proc = subprocess.run(cmd, shell=True, stdin=open(tmp, "r"),
                              capture_output=True, encoding="utf-8", errors="replace", timeout=25)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return json.loads((proc.stdout or "").strip().splitlines()[-1]), None
        return None, (proc.stderr or proc.stdout or "no output").strip()[:400]
    except subprocess.TimeoutExpired:
        return None, "SSH 超时（arm 不可达 / 网络中断 / 不在可访问 arm 的网络）"
    except Exception as e:
        return None, str(e)[:400]
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _arm_api_via_ssh(method: str, path: str, query: dict = None, json_body: dict = None, timeout: int = 60) -> dict:
    """通过 ssh arm 在 ARM 本地调其后端 HTTP API（localhost:8000），返回解析后的 dict。

    [AI-2026-09-23 B方案] 持仓静态估值改由 ARM 自算；本机相关端点（持仓分析重算/手喂外盘价）
    不再本地计算，改为 SSH 代理到 ARM 对应端点（同源代码，ARM 自己算+写 ARM 库），前端无感。
    ARM 后端经 nginx 反代 /api/，但此处走 SSH 到 ARM 内网 8000，不经过公网写接口。
    失败返回 {"status":"error",...}，绝不兜底/绝不抛异常。
    """
    import json
    import base64
    import subprocess
    from urllib.parse import urlencode
    url = "http://127.0.0.1:8000" + path
    if query:
        url += "?" + urlencode(query)
    if method.upper() == "POST" and json_body is not None:
        body_b64 = base64.b64encode(json.dumps(json_body).encode("utf-8")).decode("ascii")
        remote = (
            'import json, base64, urllib.request\n'
            'body=base64.b64decode("' + body_b64 + '")\n'
            'req=urllib.request.Request("' + url + '", data=body, headers={"Content-Type":"application/json"}, method="POST")\n'
            'try:\n'
            '    r=urllib.request.urlopen(req, timeout=30); print(r.read().decode())\n'
            'except Exception as e:\n'
            '    print(json.dumps({"status":"error","message":str(e)[:300]}))\n'
        )
    else:
        remote = (
            'import json, urllib.request\n'
            'req=urllib.request.Request("' + url + '", method="GET")\n'
            'try:\n'
            '    r=urllib.request.urlopen(req, timeout=30); print(r.read().decode())\n'
            'except Exception as e:\n'
            '    print(json.dumps({"status":"error","message":str(e)[:300]}))\n'
        )
    try:
        cmd = 'ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no -o BatchMode=yes arm "python3 -"'
        # [AI-2026-09-23 根因修复] 中文 Windows 下 text=True 会按系统 locale(GBK) 解码子进程输出；
        # 远端 JSON 含非 ASCII（中文 note / 基金名）时抛 UnicodeDecodeError → subprocess 内部
        # readerthread 崩 → proc.stdout 变为 None → 紧随其后的 proc.stdout.strip() 报
        # "'NoneType' object has no attribute 'strip'"（detail 端点踩此坑，manual 因纯 ASCII 幸免）。
        # 第一性原理：解码口径写死 utf-8，不随系统 locale 漂移；stdout 再兜底 None。
        proc = subprocess.run(
            cmd, shell=True, input=remote, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        if proc.returncode != 0:
            return {"status": "error", "message": f"SSH/ARM 调用失败: {(proc.stderr or '').strip()[:300]}"}
        out = (proc.stdout or "").strip()
        if not out:
            return {"status": "error", "message": "ARM 返回空"}
        try:
            return json.loads(out.splitlines()[-1])
        except Exception:
            return {"status": "error", "message": f"ARM 返回非JSON: {out[:200]}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "SSH 超时（arm 不可达）"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:300]}


def _is_arm() -> bool:
    """判断当前后端进程是否运行在 ARM(H5) 部署机上。

    [AI-2026-09-23 B方案] 同源代码在 ARM 与本机各跑一份：持仓静态估值改由 ARM 自算，
    故本机端点需 SSH 代理到 ARM；但 ARM 机器上的端点若也走 SSH 代理会试图连回自己(无 arm 别名)而失败。
    以数据库路径区分：ARM 库在 /home/ubuntu/（POSIX 路径），本机在 Windows（如 D:/Study/ARB/_shared/）。
    """
    return "/home/ubuntu" in root_db_path or root_db_path.startswith("/home")


@app.get("/api/system/maintenance-report")
async def get_maintenance_report():
    """后台维护：本地 vs H5(ARM) 各数据源最新日期 + 程序健康。供前端「后台维护」页使用。"""
    try:
        local = _build_freshness_report(root_db_path)
        h5, h5_error = await asyncio.to_thread(_fetch_h5_report_via_ssh)
        return {
            "status": "ok",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "today": datetime.now().strftime("%Y-%m-%d"),
            "local_db": root_db_path,
            "uptime_seconds": int(time.time() - START_TIME),
            "local": local,
            "h5": h5,
            "h5_error": h5_error,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/fund/{code}/history")
async def get_fund_history(code: str):
    data = fund_service.get_fund_history(code)
    # [AI-2026-10-09 方案A·东哥拍板] 附带锚点信号：today=服务器当天日期、trading_today=A股日历判定
    # （market_calendar.is_trading_day，与 daily_updater/盘后兜底同口径，可判任意日期、不依赖爬虫落库时点）。
    # 前端据此校验 fundHistory rows[0] 是否为当日行：今天是交易日但缺行 ⇒ T-x 标签降级显真实日期、不冒充。
    from datetime import date as _d
    _today = _d.today()
    try:
        from arbcore.utils.market_calendar import is_trading_day as _is_cn_td
        _trading = bool(_is_cn_td('A_SHARE', _today))
    except Exception as _e:
        logger.warning(f"[fund-history] 交易日判定失败，按交易日处理(锚点校验保守): {_e}")
        _trading = True
    return {"status": "ok", "data": data,
            "today": _today.strftime('%Y-%m-%d'), "trading_today": _trading}

# [AI-2026-08-04] 单基金「核对静态估值」：替代已移除的全局重算。补采该基金近 days 个交易日
# 价格+净值（级联底层ETF日价），再重算 static_val。对应前端历史弹窗「核对静态估值」按钮。
@app.post("/api/fund/{code}/reconcile_static_val")
async def reconcile_static_val(code: str, days: int = 10):
    try:
        from services.fund_reconcile import reconcile_fund_static_val
        result = reconcile_fund_static_val(code, days)
        return {"status": "ok" if result.get("ok") else "error", "data": result}
    except Exception as e:
        import traceback
        logger.error(f"reconcile_static_val {code} failed: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": str(e)}

@app.get("/api/fund/{code}/intraday")
def get_fund_intraday(code: str, date: str = None, days: int = 1):
    """获取基金的分时数据（曲线图用，支持多日）
    - days: 向前回溯天数（1/3/5），默认1天
    """
    # [2026-08-27] 当日首次请求时后台触发从 ARM 同步当日分时（本地不采，ARM/AMD 采后回传）
    try:
        from services.intraday.arm_sync_service import ensure_synced_today
        threading.Thread(target=ensure_synced_today, daemon=True).start()
    except Exception as e:
        logger.warning(f"触发 ARM 分时同步失败(忽略): {e}")
    data = fund_service.get_fund_intraday(code, date, days)
    return {"status": "ok", "data": data}


@app.post("/api/sync/arm_intraday")
async def sync_arm_intraday_api(code: str = Body(default=None)):
    """手动触发从 ARM 同步当日分时到本地（关注基金 162411/164701/161116）。
    前端「同步ARM」按钮调用；SCP 在后台线程池执行，不阻塞事件循环。"""
    try:
        from services.intraday.arm_sync_service import sync_arm_intraday_to_local
        fund_codes = [code] if code else None
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: sync_arm_intraday_to_local(fund_codes))
        return {"status": result.get("status", "error"), "data": result}
    except Exception as e:
        logger.error(f"手动同步 ARM 分时失败: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/fund/{code}/basket")
async def get_fund_basket(code: str):
    data = fund_service.get_fund_basket(code)
    return {"status": "ok", "data": data}

@app.get("/api/fund/{code}/holding-periods")
async def get_fund_holding_periods(code: str):
    """基金季报持仓分析：可用报告期列表"""
    try:
        data = holding_service.get_periods(code)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"Error getting holding periods for {code}: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/fund/{code}/holdings")
async def get_fund_holdings(code: str, period: str):
    """基金季报持仓分析：某报告期持仓明细、地区分布、变动"""
    try:
        data = holding_service.get_holdings(code, period)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"Error getting holdings for {code}/{period}: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/api/fund/{code}/holding-valuation")
async def get_fund_holding_valuation(code: str, period: str):
    """基金季报持仓分析：季报持仓法实时估值"""
    try:
        data = holding_service.get_valuation(code, period)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"Error getting holding valuation for {code}/{period}: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/fund/{code}/holding-recalc")
async def get_fund_holding_recalc(code: str, period: str = "2026H1", start: str = "2026-07-01"):
    """[AI-2026-09-23 B方案·本地缓存读取] 持仓静态估值核心列（date/official_nav/holding_static_val/err）。

    本机与 ARM 各读各自库的 unified_fund_history.holding_static_val（ARM 自算、本地经
    pull_oil_static_from_arm 日更拉回），**展示时不再 SSH 代理 ARM**。
    诊断细节（etf_prices/fill_warning/note）由前端弹窗按需调 /holding-recalc-detail 取全量。
    """
    try:
        data = holding_service.get_local_static_valuation_rows(code, start)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"holding-recalc 失败: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/fund/{code}/holding-recalc-detail")
def get_fund_holding_recalc_detail(code: str, period: str = "2026H1", start: str = "2026-07-01"):
    """[AI-2026-09-23 B方案] 持仓静态估值全量诊断（含 etf_prices/fill_warning/note）。

    本机运行→SSH 代理 ARM 现算（该端点只读、无写，符合"不新增公网写接口"红线）；
    ARM 运行→本地现算。仅供持仓静态估值弹窗按需展开诊断，不在页面加载时调用。
    """
    try:
        # [AI-2026-09-30] 同步 `def` 端点 → FastAPI 自动丢线程池。本机分支 _arm_api_via_ssh 是同步
        # ssh 子进程（默认 60s 超时），ARM 分支 get_recalc_history 是本地现算（读冻结价+算全期）。
        # 写成 async 会焊死单 worker 事件循环 —— 与 /sync-oil-static 同款事故（客户端 30s 超时 → Network Error）。
        if _is_arm():
            data = holding_service.get_recalc_history(code, period, start)
            return {"status": "ok", "data": data}
        return _arm_api_via_ssh(
            "GET", f"/api/fund/{code}/holding-recalc-detail",
            query={"period": period, "start": start})
    except Exception as e:
        logger.error(f"holding-recalc-detail 失败: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/fund/{code}/sync-oil-static")
async def sync_oil_static_api(code: str):
    """[AI-2026-09-23 B方案] 手动触发 本地←ARM 拉取原油三基金 holding_static_val（pull_oil_static_from_arm）。

    仅本地运行有意义（ARM 上即从自身拉自身，冗余）；前端"同步静态持仓估值"按键调用。
    拉回后前端刷新本地缓存读取，使 最新净值 / 持仓静态估值 卡片反映最新。
    """
    try:
        if _is_arm():
            return {"status": "ok", "data": {"updated": 0, "message": "ARM 端无需同步（持仓静态估值由本机自算自存）"}}
        # [AI-2026-09-26] pull_oil_static_from_arm 内含阻塞式 ssh 子进程；async 处理器里直接调会卡死
        # 整个事件循环（单 worker），ARM 慢/不可达时请求被拖到客户端 30s 超时 → axios 报 "Network Error"。
        # 第一性原理：把阻塞调用丢到线程池，事件循环始终可服务其他请求；SSH 失败也以 JSON 返回而非挂死。
        r = await asyncio.to_thread(holding_service.pull_oil_static_from_arm)
        return {"status": r.get("status", "ok"), "data": r}
    except Exception as e:
        logger.error(f"sync-oil-static 失败: {e}")
        return {"status": "error", "message": str(e)}


# [AI-2026-09-26] 手喂端点 GET/POST /manual-etf-prices 已删：
# OILUSA/1671/1699 由 ARM sampler 自动抓取（SIX 官方 CSV / 雅虎日本），手喂 UI/闭环废止，
# 见 docs/013_2 与 usa_etf_history_sampler.py。

@app.get("/api/fund/{code}/holding-realtime")
def get_fund_holding_realtime(code: str):
    """基金季报持仓分析：持仓实时估值（Model B，季报持仓法 + CL 期货实时价）。

    [AI-2026-09-30] 刻意用同步 `def`：get_realtime_valuation 内部同步 urlopen 抓 CL 实时价
    （timeout=8 × 最多 3 次重试），写成 async 会焊死单 worker 事件循环 → 交给 FastAPI 丢线程池。

    分母读本地 futures_freeze_prices（需先经 /api/fund/sync-freeze 从 ARM 拉取）；
    分子现抓 CL(WTI) 实时价。分母缺失返回 error/freeze_incomplete，不兜底。
    """
    try:
        data = holding_service.get_realtime_valuation(code)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"Error getting holding realtime for {code}: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/fund/{code}/hedge-exposure")
async def get_fund_hedge_exposure(code: str):
    """对冲穿透：底层 ETF 实际持有合约月 + 归一化 CL 对冲分布（对冲页表1/表2）。

    数据来自 etf_contract_exposure 配置表（人工按月维护；指数每月滚动，as_of 为快照日期）。
    静态估值用 ETC 市价即可，但对冲做空的是期货本身，月份必须穿透匹配——两个独立问题。
    """
    try:
        data = holding_service.get_hedge_exposure(code)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"Error getting hedge exposure for {code}: {e}")
        return {"status": "error", "message": str(e)}


# [AI-2026-09-23] H5 原油实时估值独立页（手机被动看板核心 API）
# ARM 自算（CL/MCL 对冲口径，正确），复用 get_realtime_valuation；页面打开即调此接口现算，
# 前端 15-30s 自动/手动刷新；无服务器定时任务（不开页面零消耗）。
OIL_RT_FUNDS = ["160723", "161129", "501018"]
# [AI-2026-09-24] 删除硬编码 OIL_RT_NAMES：原字典三个名称全部写错
# （160723 误作"南方原油"、161129 误作"国泰商品"、501018 误作"南方原油LOF"），
# 且与 DB 权威表 unified_fund_list 构成双重真相源（东哥铁律：DB 唯一权威、绝不兜底）。
# 改为运行时读 unified_fund_list.fund_name，DB 改名即时生效。


def _oil_fund_names(codes) -> dict:
    """从 DB 权威表 unified_fund_list 读基金名称（取代硬编码字典，消除双重真相源）。

    返回值形如 {'161129': '易方达原油'}；DB 查不到该 code 时不返回条目（调用方显代码，不兜底假名）。
    """
    out = {}
    try:
        conn = holding_service._get_conn()
        try:
            q = ",".join("?" * len(codes))
            for c, n in conn.execute(
                    "SELECT fund_code, fund_name FROM unified_fund_list "
                    "WHERE fund_code IN (%s)" % q, tuple(codes)):
                if n:
                    out[str(c)] = n
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"读基金名称失败(降级为代码): {e}")
    return out


def _simplify_oil_contracts(contracts: dict) -> dict:
    """从 get_realtime_valuation 的 contracts 中提取 H5 页面所需精简字段。"""
    out = {}
    for k, c in (contracts or {}).items():
        if not isinstance(c, dict):
            continue
        out[k] = {
            "contract": c.get("contract"),
            "status": c.get("status"),
            "realtime_nav": c.get("realtime_nav"),
            "lof_price": c.get("lof_price"),
            "lof_price_source": c.get("lof_price_source"),
            "realtime_premium": c.get("realtime_premium"),
            "cl_now": c.get("cl_now"),
            "cl_time": c.get("cl_time"),
            "fx_now": c.get("fx_now"),
            "fx_status": c.get("fx_status"),
            "freeze_trade_date": c.get("freeze_trade_date"),
            "message": c.get("message"),
        }
    return out


def _oil_mixed_valuation(contracts: dict, hedge_plan: dict) -> dict | None:
    """混合估值（WTI 动态加权）：V_mix = Σ(weights[i]% × months[i].realtime_nav)。

    口径与本机 HoldingAnalysis.vue 的 mixedValuation/mixedPremium 完全一致（东哥 2026-09-23 拍板上 H5）：
    仅当 hedge_plan 存在且每个月都取到估值时返回，否则 None（不兜底）。
    混合溢价 = LOF 现价 / V_mix − 1（LOF 价取任一合约行的同一市价，与三合约行同口径）。
    """
    if not hedge_plan or not hedge_plan.get("months") or not hedge_plan.get("weights"):
        return None
    months, weights = hedge_plan["months"], hedge_plan["weights"]
    if len(months) != len(weights):
        return None
    nav_sum, ok_all = 0.0, True
    for m, w in zip(months, weights):
        c = (contracts or {}).get(m) or {}
        nav = c.get("realtime_nav")
        if nav is None:
            ok_all = False
            break
        nav_sum += (w / 100.0) * nav
    if not ok_all or nav_sum <= 0:
        return None
    lof_price = next(
        (c.get("lof_price") for c in (contracts or {}).values()
         if isinstance(c, dict) and c.get("lof_price")), None)
    mix = {"months": months, "weights": weights, "realtime_nav": round(nav_sum, 4)}
    if lof_price:
        mix["realtime_premium"] = round(lof_price / nav_sum - 1, 6)
    return mix


def _fund_hedge_plan(fund_code: str) -> dict | None:
    """取该基金的进阶对冲方案（hedge_plan，敞口前两月按比例），失败返回 None 不影响主流程。"""
    try:
        return holding_service.get_hedge_exposure(fund_code).get("hedge_plan")
    except Exception:
        return None


def _collect_oil_rt_funds():
    """[同步·阻塞] 逐只原油基金算 Model B，组装 H5 页精简结构；返回 (funds, overall_ok)。

    ⚠️ 只允许被**同步 def** 端点 /api/oil_rt 调用：get_realtime_valuation 内部是**同步**
    urlopen 抓 CL 实时价（timeout=8 × 最多 3 次重试 + 退避 0.9s ⇒ 单合约最坏 24.9s；
    compute_contracts = WTI 近月±1 + Brent 书月份，最坏 4 个合约 ⇒ 单基金最坏 ≈100s，
    三基金串行最坏 ≈300s）。若改回 async def 直接调用，这段阻塞会压在单 worker 事件循环上，
    期间**所有接口全部无响应**（9-30 实测：主看板轮询 + 持仓静态估值全部排队，watchdog
    卡顿 91→151→211s 单调递增直至死锁）。FastAPI 对非 async 端点自动丢线程池 ⇒ 一行治好。
    """
    names = _oil_fund_names(OIL_RT_FUNDS)  # [AI-2026-09-24] 名称读 DB 权威表
    funds = []
    overall_ok = True
    for code in OIL_RT_FUNDS:
        try:
            d = holding_service.get_realtime_valuation(code)
            if d.get("status") == "ok":
                contracts = _simplify_oil_contracts(d.get("contracts", {}))
                hedge_plan = _fund_hedge_plan(code)
                simplified = {
                    "fund_code": code,
                    "fund_name": names.get(code) or code,
                    "base_date": d.get("base_date"),
                    "base_nav": d.get("base_nav"),
                    "selected_contract": d.get("selected_contract"),
                    "active_contracts": d.get("active_contracts"),
                    "hedge_plan": hedge_plan,
                    "mixed_valuation": _oil_mixed_valuation(contracts, hedge_plan),
                    "status": "ok",
                    "contracts": contracts,
                }
            else:
                overall_ok = False
                simplified = {
                    "fund_code": code, "fund_name": names.get(code) or code,
                    "status": "error", "code": d.get("code"), "message": d.get("message"),
                    "contracts": {},
                }
            funds.append(simplified)
        except Exception as e:
            overall_ok = False
            funds.append({
                "fund_code": code, "fund_name": names.get(code) or code,
                "status": "error", "message": str(e)[:200], "contracts": {},
            })
    return funds, overall_ok


@app.get("/api/oil_rt")
def get_oil_rt():
    """H5 原油实时估值页（手机被动看板）：循环三只原油基金，ARM 自算，无定时任务。

    [AI-2026-09-30] 刻意声明为同步 `def`（**不是** `async def`）：让 FastAPI 把这段同步阻塞
    流程丢进线程池执行。写成 async 会焊死单 worker 事件循环（根因见 _collect_oil_rt_funds 注释）。
    """
    try:
        funds, overall_ok = _collect_oil_rt_funds()
        return {
            "status": "ok" if overall_ok else "partial",
            "data": {
                "funds": funds,
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "ARM 自算（CL/MCL 对冲口径）",
            },
        }
    except Exception as e:
        logger.error(f"oil_rt 失败: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/oil/sync-report-holdings")
async def sync_oil_report_holdings_api():
    """手动触发：把本机三只原油基金的季报持仓同步到 ARM（季度更新自动同步能力）。

    部署后可由 daily_updater 季报解析步骤自动调用（需东哥授权挂接）；亦可手动点按钮触发。
    后台线程执行，不阻塞事件循环。
    """
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: holding_service.sync_report_holdings_to_arm())
        return {"status": result.get("status", "error"), "data": result}
    except Exception as e:
        logger.error(f"同步季报持仓到 ARM 失败: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/fund/sync-freeze")
async def sync_futures_freeze_api():
    """从 ARM 拉 CL 三时点冻结价到本地（每天上午盘前调一次即可）。

    复用 ssh arm 查询通道，仅拉 futures_freeze_prices 小表，不 scp 全库、不碰 ARM 部署。
    后台线程执行，不阻塞事件循环。
    """
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: holding_service.sync_futures_freeze_from_arm())
        return {"status": result.get("status", "error"), "data": result}
    except Exception as e:
        logger.error(f"同步 CL 冻结价失败: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/fund/sync-usa-etf")
async def sync_usa_etf_from_arm_api():
    """从 ARM 拉美股/伦敦/港股 ETF 日 K（usa_etf_daily_prices）全表到本地。

    本地程序非每日运行，ARM 上 usa-etf-history.timer 每日增量累积（截至昨日北京时间），
    本接口仅在用户手动点按钮时把 ARM 整张表拉回本地，因此"停用 N 天后点一次"会补齐
    这 N 天（及此前任何缺失）的全部历史。后台线程执行，不阻塞事件循环。
    """
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: holding_service.sync_usa_etf_from_arm())
        return {"status": result.get("status", "error"), "data": result}
    except Exception as e:
        logger.error(f"同步美股 ETF 日 K 失败: {e}")
        return {"status": "error", "message": str(e)}


# [AI-2026-09-24 东哥需求] ARM 美股价新鲜度检测 + 本机一键重抓推送（仅本机可用）
@app.get("/api/fund/oil-price-freshness")
async def get_oil_price_freshness():
    """只读检测：ARM 是否已抓到「新浪口径的美股最新已收盘交易日」。

    供本机「季报持仓分析」页提示「新浪未抓到凌晨收盘价」+ 一键重抓按钮的判据。
    口径见 holding_service.get_us_price_freshness（与 us_clock 拦截严格一致）。
    仅本机可调（ARM 上调会 SSH 回自己，无 arm 别名必失败）。
    """
    try:
        if _is_arm():
            return {"status": "error", "message": "该检测仅在本机执行"}
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, holding_service.get_us_price_freshness)
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"美股价新鲜度检测失败: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/api/fund/oil-refetch-prices")
async def refetch_oil_prices():
    """[AI-2026-09-24 东哥需求] 一键闭环：本地重抓 → 推 ARM → ARM 重算 → 拉回本地。

    触发场景：ARM 07:30 采集时数据源尚未更新 T-1 收盘价 → ARM 缺价 →
    静态估值不起新行（"宁缺毋假"）。本机直连数据源补抓后推 ARM，
    再驱动 ARM 自算落库，最后拉回本地缓存。全程 SSH/localhost，不经公网写端点。
    [AI-2026-09-26] 补抓源扩为四路：美股/伦敦/港股→新浪/腾讯，JP/CH（OILUSA/1671/1699）
    → sampler 的 SIX 官方 CSV / 雅虎日本（_fallback_fill_missing_etf 统一路由）。
    仅本机可调（ARM 上调会 SSH 回自己，无 arm 别名必失败）。
    """
    try:
        if _is_arm():
            return {"status": "error", "message": "该操作仅在本机执行"}

        def _job():
            out: dict = {}
            # ① 本地直连新浪/腾讯补抓「最新已收盘交易日」缺价标的（本地为权威）
            out["local_fill"] = holding_service.refetch_usa_etf_from_source()
            # ② 本机 → ARM 推价格（增量 INSERT OR REPLACE，绝不删）
            out["push"] = holding_service.push_usa_etf_to_arm()
            # ③ 驱动 ARM 自算（holding-recalc-detail 现算并落 ARM 库）
            recalc: dict = {}
            if out["push"].get("status") == "ok":
                for code in ("160723", "161129", "501018"):
                    recalc[code] = _arm_api_via_ssh(
                        "GET", f"/api/fund/{code}/holding-recalc-detail",
                        query={"start": "2026-01-01"}, timeout=600)
            out["arm_recalc"] = recalc
            # ④ ARM 自算值拉回本地缓存（页面读本地 holding_static_val）
            out["pull"] = holding_service.pull_oil_static_from_arm()
            return out

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _job)

        fill = data.get("local_fill") or {}
        push = data.get("push") or {}
        recalc = data.get("arm_recalc") or {}
        pull = data.get("pull") or {}
        n_recalc_ok = sum(1 for v in recalc.values() if (v or {}).get("status") == "ok")
        n_recalc = len(recalc)
        ok = (push.get("status") == "ok" and pull.get("status") == "ok"
              and n_recalc == 3 and n_recalc_ok == 3)

        parts = []
        filled = fill.get("filled") or {}
        ref_d = fill.get("reference_date") or "-"
        if filled:
            parts.append(f"本机从源补抓 {len(filled)} 只（{ref_d}）")
        else:
            parts.append(f"本机源无新价可补（参考日 {ref_d}）")
        if fill.get("still_missing"):
            parts.append(f"仍缺: {'、'.join(fill['still_missing'])}")
        if fill.get("error"):
            parts.append(f"补抓失败({fill['error']})")
        parts.append(push.get("message") or "推送未执行")
        parts.append(f"ARM 重算 {n_recalc_ok}/{n_recalc} 只")
        parts.append(pull.get("message") or "拉回未执行")
        data["summary"] = "；".join(parts)
        return {"status": "ok" if ok else "error", "data": data}
    except Exception as e:
        logger.error(f"重抓美股价并推 ARM 失败: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/fund/hedge_multipliers")
async def get_hedge_multipliers():
    """获取所有期货乘数配置（前端沙盘可调用）"""
    from arbcore.config.futures_multipliers import list_all_multipliers
    return {"status": "ok", "data": list_all_multipliers()}

@app.get("/api/fund/{code}/valuation_meta")
def get_fund_valuation_meta(code: str):
    """估值元数据 — 委托 fund_service.get_valuation_meta() [AI-2026-07-16 消除代码重复]"""
    try:
        return fund_service.get_valuation_meta(code)
    except Exception as e:
        logger.error(f"Error getting valuation meta for {code}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}


# --- 债券/货币 ETF（现金管理）接口 —— [AI-2026-10-11 东哥拍板] 整组停用 ---
# 债券/货币 ETF（511880/511990/511360/511520）估值**已于 2026-09-18 迁至 ETFarb**
# （`ETFarb/src/core/bond_etf_valuation.py`）。LOFarb「现金管理」分类已空、这些接口无消费者
# ⇒ 保留**路径外壳**（避免前端误调 404），函数体直接回明确提示，不再引用已归档模块。
# 原实现见 `_deprecated/bond_etf_valuation.py`。
_BOND_MOVED = {"status": "moved",
               "message": "债券/货币 ETF（现金管理）估值已迁至 ETFarb（2026-09-18），LOFarb 侧接口停用"}


@app.get("/api/bond/bp-data")
async def get_bp_data(date: str = None):
    """[已停用] 国开债 BP 数据 → 见 ETFarb"""
    return _BOND_MOVED

@app.post("/api/bond/bp-data")
async def save_bp_data(request: Request):
    """[已停用] 保存国开债 BP 数据 → 见 ETFarb"""
    return _BOND_MOVED


# --- Core Fund Configuration (YAML) APIs ---
@app.get("/api/config/funds")
async def get_all_fund_configs():
    """获取 lof_config.yaml 中的所有基金配置"""
    cfg = config_manager_service.load_config()
    return {"status": "ok", "data": cfg.get('funds', [])}

# [AI-2026-07-09] 根治：YAML 改动后自动同步 unified_fund_list 表，主看板即时生效，不再需要手动跑同步脚本
def _sync_config_to_db():
    try:
        cfg = config_manager_service.load_config()
        db.sync_unified_fund_list(cfg.get('funds', []))
    except Exception as e:
        logger.error(f"[配置同步] YAML→数据库失败: {e}")

@app.post("/api/config/funds/upsert")
async def upsert_fund_config(request: Request):
    """新增或修改基金配置，并自动同步数据库"""
    data = await request.json()
    success = config_manager_service.upsert_fund_config(data)
    if success:
        _sync_config_to_db()
    return {"status": "ok" if success else "error"}

@app.delete("/api/config/funds/{code}")
async def delete_fund_config(code: str):
    """从 YAML 中删除基金，并自动同步数据库"""
    success = config_manager_service.delete_fund_config(code)
    if success:
        _sync_config_to_db()
    return {"status": "ok" if success else "error"}

@app.get("/api/config/funds/export")
async def export_fund_config():
    """导出 lof_config.yaml 为文件下载（带时间戳文件名）"""
    from fastapi.responses import Response
    try:
        yaml_content = config_manager_service.export_config()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"lof_config_{ts}.yaml"
        return Response(
            content=yaml_content,
            media_type="application/x-yaml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        logger.error(f"导出 YAML 失败: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/config/funds/import")
async def import_fund_config(file: UploadFile = File(...)):
    """从上传的 YAML 文件导入基金配置，并自动同步数据库"""
    try:
        content = await file.read()
        yaml_text = content.decode('utf-8')
        config_manager_service.import_config(yaml_text)
        _sync_config_to_db()
        return {"status": "ok", "message": "导入成功，旧配置已备份为 .bak"}
    except Exception as e:
        logger.error(f"导入 YAML 失败: {e}")
        return {"status": "error", "message": str(e)}

# [AI-2026-07-09] 动态 TAB：返回 unified_fund_list 中所有去重的基金分类，供主看板动态生成 TAB
@app.get("/api/config/categories")
# [AI-2026-09-02] 改为同步 def：FastAPI 会自动把它丢进线程池执行，
# 同步 SQLite 调用因此永远不会阻塞 asyncio 事件循环（原 async def 会堵住整个 uvicorn）。
def get_fund_categories():
    """返回数据库中所有真实存在的基金分类（去重）"""
    try:
        conn = db._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT category FROM unified_fund_list WHERE category IS NOT NULL AND category != ''"
        ).fetchall()
        conn.close()
        cats = sorted({r[0] for r in rows if r[0]})
        return {"status": "ok", "data": cats}
    except Exception as e:
        logger.error(f"获取基金分类失败: {e}")
        return {"status": "error", "message": str(e)}

# --- IB 核心套利标的配置 APIs ---
@app.get("/api/config/ib_core_symbols")
async def get_ib_core_symbols():
    """获取 IB 核心套利标的白名单"""
    from arbcore.config.source_routing import IB_CORE_ARBITRAGE_SYMBOLS
    return {"status": "ok", "data": IB_CORE_ARBITRAGE_SYMBOLS}

@app.post("/api/config/ib_core_symbols")
async def update_ib_core_symbols(request: Request):
    """更新 IB 核心套利标的白名单（写回 lof_config.yaml 持久化，重启不丢）"""
    from arbcore.config.source_routing import IB_CORE_ARBITRAGE_SYMBOLS, SOURCE_SYMBOL_MAP, US_ETF_MAP
    try:
        data = await request.json()
        symbols = data.get('symbols', [])
        
        if not symbols:
            return {"status": "error", "message": "标的列表不能为空"}
        
        # 验证：所有标的必须在 US_ETF_MAP 中
        for sym in symbols:
            if sym not in US_ETF_MAP:
                return {"status": "error", "message": f"标的 {sym} 不在美股 ETF 映射表中"}
        
        # 更新全局变量
        IB_CORE_ARBITRAGE_SYMBOLS.clear()
        IB_CORE_ARBITRAGE_SYMBOLS.extend(symbols)
        
        # 重建 SOURCE_SYMBOL_MAP
        SOURCE_SYMBOL_MAP['IB'] = list(symbols)
        SOURCE_SYMBOL_MAP['IB_CORE_ONLY'] = list(symbols)
        
        # 重新分流：非核心标的归入 FUTU
        for symbol, source in {**US_ETF_MAP}.items():
            if source == 'IB' and symbol not in symbols:
                if symbol not in SOURCE_SYMBOL_MAP['FUTU']:
                    SOURCE_SYMBOL_MAP['FUTU'].append(symbol)
        
        # 去重排序
        for source in SOURCE_SYMBOL_MAP:
            SOURCE_SYMBOL_MAP[source] = sorted(set(SOURCE_SYMBOL_MAP[source]))

        # [AI-2026-08-05] 持久化写回 lof_config.yaml 的 ib_core_symbols，避免重启丢失
        # 仅替换 ib_core_symbols 块内的列表项，保留文件其余部分与注释
        try:
            import os as _os
            _yaml_path = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..', '..', 'arbcore', 'config', 'lof_config.yaml'))
            with open(_yaml_path, encoding='utf-8') as _f:
                _lines = _f.readlines()
            _out, _i, _in_block = [], 0, False
            while _i < len(_lines):
                _line = _lines[_i]
                if _line.strip() == 'ib_core_symbols:' or _line.startswith('ib_core_symbols:'):
                    _out.append(_line)
                    _in_block = True
                    _i += 1
                    continue
                if _in_block:
                    if _line.strip().startswith('- '):
                        _i += 1
                        continue  # 跳过旧列表项
                    for _s in symbols:
                        _out.append(f'- {_s}\n')
                    _in_block = False
                _out.append(_line)
                _i += 1
            if _in_block:
                for _s in symbols:
                    _out.append(f'- {_s}\n')
            with open(_yaml_path, 'w', encoding='utf-8') as _f:
                _f.writelines(_out)
            logger.info(f"[AI-2026-08-05] IB核心标的已写回YAML: {symbols}")
        except Exception as _e:
            logger.warning(f"IB核心标的写回YAML失败(仅内存生效): {_e}")

        return {"status": "ok", "message": f"IB 核心标的已更新为 {len(symbols)} 只（已持久化）", "data": symbols}
    except Exception as e:
        logger.error(f"更新 IB 核心标的失败: {e}")
        return {"status": "error", "message": str(e)}

# --- [AI-2026-07-07] App-level toggle settings ---
# 全部分类（快照服务遍历用；分类级暂停功能已于 2026-09-23 完整删除，见 docs/013_7）
# [AI-2026-10-11 东哥拍板] 收敛为 LOFarb **实有的 4 类**（原含已废弃的 QDII亚洲/国内LOF/现金管理，
#   会在启动日志刷「跳过空分类(无成员基金)」噪音）。新增分类时请同步 dashboard_snapshot_service.py。
ALL_CATEGORIES = ["黄金原油", "QDII欧美", "QDII日本", "商品"]

# [AI-2026-07-07] 回补缺失指数历史
# [AI-2026-10-10] Private export 端点已剥离至 LOFTrader（/api/private/status + /api/private/export/{code} 移除）
@app.get("/api/private/lazy_calc")
async def lazy_calc(fund_code: str = "162411"):
    """
    幽灵做市商实时计算 — 复用 fund_service.get_valuation_meta() 获取完整估值数据。
    额外计算 Lazy 特有模式（safe/peg）的折溢价。
    """
    if not lazy_trader_instance:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Lazy Trader not loaded"})

    try:
        # 1. 复用现有估值元数据（含 hedge、汇率、实时报价、T-1 基准等）
        meta = fund_service.get_valuation_meta(fund_code)
        if meta.get("status") == "error":
            return JSONResponse(status_code=404, content=meta)

        base_data = meta.get("base_data", {})
        fund_cfg = meta.get("fund_config", {})
        rt_quotes = meta.get("realtime_quotes", {})
        t1_data = meta.get("t1_data", {})
        latest_fx = meta.get("latest_exchange_rate", 7.25)

        # 2. 获取 LOF 实时行情
        lof_price = 0.0
        lof_bid = 0.0
        lof_ask = 0.0
        if market_data_service:
            try:
                rt = market_data_service.get_realtime_quote(fund_code)
                if rt:
                    lof_price = rt.get("price", 0) or 0
                    lof_bid = rt.get("bid") or lof_price
                    lof_ask = rt.get("ask") or lof_price
            except Exception:
                pass
        if lof_price <= 0:
            lof_price = float(t1_data.get("price", 0) or 0)
            lof_bid = lof_price
            lof_ask = lof_price

        # 3. 确定 underlying_symbol 和最相关的 ETF 实时价格
        portfolio = fund_cfg.get("valuation_portfolio", [])
        underlying_symbol = portfolio[0].get("symbol", "") if portfolio else ""
        # [V10.8] basket为空时用 trade_etf（related_index）备用源（如162411→XOP）
        if not underlying_symbol:
            underlying_symbol = fund_cfg.get("trade_etf", "")
        underlying_clean = ""
        if underlying_symbol:
            s = underlying_symbol.replace("^", "")
            for suffix in ["-EU", "-JP", "-HK"]:
                if s.endswith(suffix):
                    s = s[: -len(suffix)]
                    break
            underlying_clean = s

        us_bid = 0.0
        us_ask = 0.0
        us_bid_size = 0
        us_ask_size = 0
        # [V10.8] 优先从 rt_quotes 取；basket为空时 rt_quotes 为空，直接行情驱动备用源
        if underlying_clean and underlying_clean in rt_quotes and rt_quotes[underlying_clean]:
            q = rt_quotes[underlying_clean]
            us_bid = float(q.get("bid", 0) or 0)
            us_ask = float(q.get("ask", 0) or 0)
            us_bid_size = int(q.get("bid_size", 0) or 0)
            us_ask_size = int(q.get("ask_size", 0) or 0)
            if us_bid <= 0:
                us_bid = float(q.get("price", 0) or 0)
            if us_ask <= 0:
                us_ask = float(q.get("price", 0) or 0)
        elif underlying_clean and market_data_service:
            try:
                q = market_data_service.get_realtime_quote(underlying_clean)
                if q:
                    us_bid = float(q.get("bid", 0) or 0)
                    us_ask = float(q.get("ask", 0) or 0)
                    us_bid_size = int(q.get("bid_size", 0) or 0)
                    us_ask_size = int(q.get("ask_size", 0) or 0)
                    if us_bid <= 0:
                        us_bid = float(q.get("price", 0) or 0)
                    if us_ask <= 0:
                        us_ask = float(q.get("price", 0) or 0)
            except Exception as e:
                logger.error(f"[LazyCalc] failed to get quote for {underlying_clean}: {e}")

        # 4. hedge 值（复用 get_valuation_meta 返回的）
        hedge = float(base_data.get("hedge", 0)) if base_data else 0
        position = float(fund_cfg.get("position")) / 100.0 if fund_cfg and fund_cfg.get("position") is not None else None  # [AI-2026-08-07] 缺失即 None，禁止 0.95 默认

        # 5. Lazy 特有溢价计算（safe 砸单 / peg 内卷）
        # 正确公式: val = base_nav * (1 - pos) + (us_price * fx) / hedge  (注意: 第二项不乘pos)
        base_nav = float(base_data.get("nav", 0)) if base_data else 0
        if base_nav > 0 and hedge > 0 and position is not None:  # [AI-2026-08-07] position 可能 None
            val_safe = base_nav * (1 - position) + (us_bid * latest_fx) / hedge
        else:
            val_safe = 0
        premium_safe = (lof_bid / val_safe - 1) * 100 if val_safe > 0 else 0

        if base_nav > 0 and hedge > 0 and position is not None:  # [AI-2026-08-07] position 可能 None
            peg_price = (us_ask - 0.01) if us_ask > 0.01 else us_ask
            val_peg = base_nav * (1 - position) + (peg_price * latest_fx) / hedge
        else:
            val_peg = 0
        premium_peg = (lof_bid / val_peg - 1) * 100 if val_peg > 0 else 0

        # [AI-2026-07-07] 赎回费率优先从 broker_redemption_fees 表读取
        redemption_fee = ledger_service.get_fee_rate(fund_code)
        if redemption_fee <= 0:
            redemption_fee = 0.50

        result = {
            "fund_code": fund_code,
            "fund_name": fund_cfg.get("trade_etf", ""),
            "underlying_symbol": underlying_symbol,
            "hedge": hedge,
            "position": position,
            "fx_rate": latest_fx,
            "lof_price": lof_price,
            "lof_bid": lof_bid,
            "lof_ask": lof_ask,
            "us_bid": us_bid,
            "us_ask": us_ask,
            "us_bid_size": us_bid_size,
            "us_ask_size": us_ask_size,
            "premium_safe": round(premium_safe, 3),
            "premium_peg": round(premium_peg, 3),
            "redemption_fee": redemption_fee,
        }
        return {"status": "ok", "data": result}

    except Exception as e:
        logger.error(f"[LazyCalc] Error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

def _resolve_hedge_for_order(fund_code, underlying_symbol):
    """[AI-2026-09-02] 同步 DB 查询：从 fund_daily_factors 取 hedge，取不到则动态推算。
    只供 asyncio.to_thread 调用 —— 同步 SQLite 调用不得留在事件循环里。
    返回 float 或 None（缺失即 None，禁止 1.0 假常量）。"""
    import pandas as pd
    conn2 = db._get_conn()
    try:
        h_df = pd.read_sql(
            "SELECT hedge FROM fund_daily_factors "
            "WHERE fund_code=? AND hedge IS NOT NULL AND hedge > 0 "
            "ORDER BY date DESC LIMIT 1",
            conn2, params=[fund_code]
        )
        if not h_df.empty:
            return float(h_df.iloc[0]['hedge'])
        # 备用源：动态推算
        pos_df = pd.read_sql(
            "SELECT position FROM fund_daily_factors "
            "WHERE fund_code=? AND position IS NOT NULL ORDER BY date DESC LIMIT 1",
            conn2, params=[fund_code]
        )
        position = float(pos_df.iloc[0]['position']) if not pos_df.empty else None  # [AI-2026-08-07] 缺失即 None
        nav_df = pd.read_sql(
            "SELECT nav, price FROM unified_fund_history "
            "WHERE fund_code=? AND nav>0 ORDER BY date DESC LIMIT 1",
            conn2, params=[fund_code]
        )
        nav = float(nav_df.iloc[0]['nav']) if not nav_df.empty else None  # [AI-2026-08-07] 缺失即 None
        fx_df = pd.read_sql(
            # [AI-2026-10-07] 加 IS NOT NULL：节假日最新一行 mid 为 NULL，
            #   原写法 float(None) 抛 TypeError → 整函数失败 → 下单被误判为"hedge 不足"拒绝。
            #   与 fund_service.get_valuation_meta 同口径：取最近一次已公布中间价。
            #   [AI-2026-10-09] 原参照 sampler_service 已随本地采样移除（分时归 ARM）。
            "SELECT usd_cny_mid FROM exchange_rate "
            "WHERE usd_cny_mid IS NOT NULL ORDER BY date DESC LIMIT 1", conn2
        )
        fx = float(fx_df.iloc[0]['usd_cny_mid']) if not fx_df.empty else None  # [AI-2026-08-07] 缺失即 None，禁止 7.25 假常量
        # hedge = (ETF基价 * 汇率) / (净值 * 仓位)
        # 用 t1 的 etf 收盘价作为基价近似
        etf_base = 0.0
        if underlying_symbol:
            etf_df = pd.read_sql(
                "SELECT COALESCE(NULLIF(netvalue, 0), price) as price "
                "FROM usa_etf_daily_prices WHERE symbol LIKE ? "
                "ORDER BY date DESC LIMIT 1",
                conn2, params=[f"%{underlying_symbol}%"]
            )
            if not etf_df.empty:
                etf_base = float(etf_df.iloc[0]['price'])
        if etf_base > 0 and nav is not None and nav > 0 and position is not None and position > 0 and fx is not None:  # [AI-2026-08-07] guard None
            return (etf_base * fx) / (nav * position)
        return None  # [AI-2026-08-07] SUPREME：推算所需数据不全，禁止 1.0 假常量，缺失即 None 显 --
    finally:
        conn2.close()


@app.post("/api/private/lazy_place_order")
async def lazy_place_order(request: Request):
    """
    幽灵做市商下单接口
    - quantity: LOF 份额数（如 59500），系统自动用 hedge 系数换算出 ETF 对冲股数
    - etf_quantity: 可选，如果前端已算好 ETF 股数，直接使用（绕开换算）
    """
    if not lazy_trader_instance:
        logger.warning("[LazyOrder] lazy_trader_instance is None - orders will be rejected")
        return JSONResponse(status_code=403, content={"status": "error", "message": "Lazy Trader not loaded"})
    try:
        body = await request.json()
        mode = body.get("mode", "safe")          # "safe"/"peg" 向后兼容
        direction = body.get("direction", "open") # "open" 开仓 / "close" 平仓
        fund_code = body.get("fund_code", "162411")
        underlying_symbol = body.get("underlying_symbol", "XOP")
        price = float(body.get("price", 0))        # open→us_bid1, close→us_ask1
        lof_price = float(body.get("lof_price", 0))  # LOF 限价
        lof_quantity = int(body.get("quantity", 0))
        etf_quantity = int(body.get("etf_quantity", 0))
        logger.info(f"[LazyOrder] mode={mode} dir={direction} fund={fund_code} etf={underlying_symbol} etf_qty={etf_quantity} price={price}")

        # 从 fund_daily_factors 获取 hedge 值（与 Analysis.vue 实时沙盘一致）
        if etf_quantity <= 0 and lof_quantity > 0:
            try:
                # [AI-2026-09-02] DB 查询放线程，避免同步 SQLite 阻塞 asyncio 事件循环
                hedge = await asyncio.to_thread(
                    _resolve_hedge_for_order, fund_code, underlying_symbol
                )
            except Exception:
                hedge = None  # [AI-2026-08-07] SUPREME：异常也禁止 1.0 假常量
            # 换算：ETF 股数 = LOF 份数 / hedge（与 Analysis.vue l.1123 完全一致）
            if hedge is None or hedge <= 0:
                # [AI-2026-08-07] 对冲系数缺失/推算失败，无法换算 ETF 数量——拒绝臆测下单，显 -- 
                return JSONResponse(status_code=422, content={
                    "status": "error",
                    "message": "对冲系数 hedge 不足（缺失/推算失败），无法换算 ETF 数量，已停止下单（绝不臆测数量）"
                })
            etf_quantity = max(1, int(round(lof_quantity / hedge)))

        if mode == "peg":
            if direction == "close":
                # [AI-2026-07-16] 加一分排队平仓：IB BUY LMT at bid + 0.01
                results = lazy_trader_instance.place_peg_close_order(
                    underlying_symbol=underlying_symbol,
                    quantity=etf_quantity,
                    us_bid1=price,
                )
            else:
                results = lazy_trader_instance.place_peg_order(
                    underlying_symbol=underlying_symbol,
                    quantity=etf_quantity,
                    us_ask1=price,
                )
        elif direction == "close":
            # [AI-2026-07-16] 立即吃卖一平仓：IB BUY at ask
            results = lazy_trader_instance.place_close_order(
                underlying_symbol=underlying_symbol,
                us_ask1=price,
                etf_quantity=etf_quantity,
            )
        else:
            results = lazy_trader_instance.place_open_order(
                fund_code=fund_code,
                underlying_symbol=underlying_symbol,
                lof_price=lof_price,
                us_bid1=price,
                lof_quantity=lof_quantity,
                etf_quantity=etf_quantity,
            )
        any_ok = any(r.get("success") for r in results)
        logger.info(f"[LazyOrder] mode={mode} dir={direction} → any_ok={any_ok} results={results}")
        return {"status": "ok" if any_ok else "error", "data": results}
    except Exception as e:
        logger.error(f"[LazyOrder] Error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# [AI-2026-07-15] AutoOpen: 自动排队开仓 API
# 流程：POST /queue 排队LOF → 返回 auto_open_id → 前端轮询 /status
#       → LOF成交后 → POST /execute 自动空ETF（或自动执行）
_AUTO_OPEN_FILL_CHECKER = None
_AUTO_OPEN_CHECKER_LOCK = threading.Lock()
_AUTO_CLOSE_FILL_CHECKER = None
_AUTO_CLOSE_CHECKER_LOCK = threading.Lock()

def _start_auto_open_fill_checker(lazy_trader_instance):
    """启动后台线程，轮询持仓检测LOF是否成交"""
    global _AUTO_OPEN_FILL_CHECKER
    with _AUTO_OPEN_CHECKER_LOCK:
        if _AUTO_OPEN_FILL_CHECKER and _AUTO_OPEN_FILL_CHECKER.is_alive():
            return
        def _check_loop():
            logger.info("[AutoOpen] Fill checker started")
            while True:
                try:
                    if lazy_trader_instance:
                        with lazy_trader_instance._open_lock:
                            pending_items = list(lazy_trader_instance._pending_opens.items())
                        for ao_id, pending in pending_items:
                            if pending["status"] != "queued":
                                continue
                            # 轮询持仓变化
                            fc = pending["fund_code"]
                            lof_qty = pending["lof_quantity"]
                            try:
                                # [AI-2026-07-20] 优先用 trade_manager.query_position()（8888 短连接，
                                # 银河/国金/通达信脚本均支持），避免 get_positions() 仅走通达信导致
                                # 银河渠道下永远查不到持仓、自动对冲永不触发。
                                held = 0
                                _ts = lazy_trader_instance.trading_service
                                _tm = getattr(_ts, 'trade_manager', None) if _ts else None
                                if _tm and hasattr(_tm, 'query_position'):
                                    _pos = _tm.query_position(fc)
                                    if isinstance(_pos, dict) and _pos.get('volume'):
                                        held = int(_pos.get('volume', 0) or 0)
                                if held == 0 and _ts:
                                    pos_list = _ts.get_positions()
                                    for p in pos_list:
                                        if p.get("code","") == fc or p.get("code","") == f"{fc}.SZ":
                                            held = int(p.get("volume", 0) or p.get("available", 0))
                                            break
                                    # 如果持仓 ≥ lof_qty，说明已成交
                                    trade_etf = ""
                                    etf_qty = 0
                                    # 从 fund_service 获取 ETF 信息
                                    if lazy_trader_instance.fund_service:
                                        meta = lazy_trader_instance.fund_service.get_valuation_meta(fc)
                                        if meta and isinstance(meta, dict):
                                            cfg = meta.get("fund_config", {}) or {}
                                            trade_etf = cfg.get("trade_etf", "") or ""
                                            # 计算 ETF 股数
                                            bd = meta.get("base_data", {}) or {}
                                            hedge = float(bd.get("hedge", 0) or 0)
                                            if hedge > 0:
                                                etf_qty = max(1, int(round(lof_qty / hedge)))

                                    if held >= lof_qty and trade_etf and etf_qty > 0:
                                        # LOF 已成交，自动执行
                                        min_profit = pending.get("min_profit", 0.25)
                                        result = lazy_trader_instance.auto_short_if_profitable(
                                            ao_id, trade_etf, etf_qty, min_profit
                                        )
                                        status = result.get("status","")
                                        if status == "executed":
                                            logger.info(f"[AutoOpen] ✅ {fc} auto-open executed: {result}")
                                        elif status == "profit_too_low":
                                            logger.warning(f"[AutoOpen] ⚠️ {fc} filled but profit too low: {result}")
                                        elif status == "error":
                                            logger.error(f"[AutoOpen] ❌ {fc} auto-open failed: {result}")
                                    elif held >= lof_qty:
                                        logger.info(f"[AutoOpen] {fc} filled, but no trade_etf/etf_qty found, marking as filled")
                                        # 即使没有 ETF 信息也标记为已成交
                                        with lazy_trader_instance._open_lock:
                                            pending["status"] = "filled"
                                            pending["filled_at"] = datetime.now().isoformat()
                                            pending["msg"] = "filled but no ETF info for auto-short"
                            except Exception as e:
                                logger.debug(f"[AutoOpen] check {fc} positions: {e}")
                except Exception as e:
                    logger.error(f"[AutoOpen] checker error: {e}")
                time.sleep(3)  # 每3秒轮询
        _AUTO_OPEN_FILL_CHECKER = threading.Thread(target=_check_loop, daemon=True)
        _AUTO_OPEN_FILL_CHECKER.start()

def _start_auto_close_fill_checker(lazy_trader_instance):
    """启动后台线程，轮询持仓检测LOF是否已卖出（平仓）"""
    global _AUTO_CLOSE_FILL_CHECKER
    with _AUTO_CLOSE_CHECKER_LOCK:
        if _AUTO_CLOSE_FILL_CHECKER and _AUTO_CLOSE_FILL_CHECKER.is_alive():
            return
        def _check_loop():
            logger.info("[AutoClose] Fill checker started")
            while True:
                try:
                    if lazy_trader_instance:
                        with lazy_trader_instance._close_lock:
                            pending_items = list(lazy_trader_instance._pending_closes.items())
                        for ac_id, pending in pending_items:
                            if pending["status"] != "queued":
                                continue
                            fc = pending["fund_code"]
                            lof_qty = pending["lof_quantity"]
                            initial_pos = pending.get("initial_position", 0)
                            try:
                                # [AI-2026-07-20] 优先用 trade_manager.query_position()（8888 短连接），
                                # 避免 get_positions() 仅走通达信导致银河渠道下查不到持仓、自动平仓对冲不触发。
                                current_held = initial_pos  # 默认无变化
                                _ts = lazy_trader_instance.trading_service
                                _tm = getattr(_ts, 'trade_manager', None) if _ts else None
                                if _tm and hasattr(_tm, 'query_position'):
                                    _pos = _tm.query_position(fc)
                                    if isinstance(_pos, dict) and _pos.get('volume'):
                                        current_held = int(_pos.get('volume', 0) or 0)
                                if current_held == initial_pos and _ts:
                                    pos_list = _ts.get_positions()
                                    for p in pos_list:
                                        if p.get("code","") == fc or p.get("code","") == f"{fc}.SZ":
                                            current_held = int(p.get("volume", 0) or p.get("available", 0))
                                            break
                                    # 如果持仓减少 >= lof_qty，说明LOF已卖出
                                    sold_qty = initial_pos - current_held
                                    if sold_qty >= lof_qty:
                                        trade_etf = ""
                                        etf_qty = 0
                                        if lazy_trader_instance.fund_service:
                                            meta = lazy_trader_instance.fund_service.get_valuation_meta(fc)
                                            if meta and isinstance(meta, dict):
                                                cfg = meta.get("fund_config", {}) or {}
                                                trade_etf = cfg.get("trade_etf", "") or ""
                                                bd = meta.get("base_data", {}) or {}
                                                hedge = float(bd.get("hedge", 0) or 0)
                                                if hedge > 0:
                                                    etf_qty = max(1, int(round(lof_qty / hedge)))

                                        if sold_qty >= lof_qty and trade_etf and etf_qty > 0:
                                            min_profit = pending.get("min_profit", -0.41)
                                            result = lazy_trader_instance.auto_close_if_profitable(
                                                ac_id, trade_etf, etf_qty, min_profit
                                            )
                                            status = result.get("status","")
                                            if status == "executed":
                                                logger.info(f"[AutoClose] ✅ {fc} auto-close executed: {result}")
                                            elif status == "profit_too_low":
                                                logger.warning(f"[AutoClose] ⚠️ {fc} sold but profit too low: {result}")
                                            elif status == "error":
                                                logger.error(f"[AutoClose] ❌ {fc} auto-close failed: {result}")
                                        elif sold_qty >= lof_qty:
                                            logger.info(f"[AutoClose] {fc} sold, but no trade_etf/etf_qty found, marking as filled")
                                            with lazy_trader_instance._close_lock:
                                                pending["status"] = "filled"
                                                pending["filled_at"] = datetime.now().isoformat()
                                                pending["msg"] = "filled but no ETF info for auto-buy"
                            except Exception as e:
                                logger.debug(f"[AutoClose] check {fc} positions: {e}")
                except Exception as e:
                    logger.error(f"[AutoClose] checker error: {e}")
                time.sleep(3)
        _AUTO_CLOSE_FILL_CHECKER = threading.Thread(target=_check_loop, daemon=True)
        _AUTO_CLOSE_FILL_CHECKER.start()

@app.post("/api/private/auto_open/queue")
async def auto_open_queue(request: Request):
    """
    自动排队开仓：
    1. 仅排队 LOF 买一（不操作 ETF）
    2. 启动后台成交检测
    3. 返回 auto_open_id 供前端轮询状态
    """
    global _AUTO_OPEN_FILL_CHECKER
    if not lazy_trader_instance:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Lazy Trader not loaded"})
    try:
        body = await request.json()
        fund_code = body.get("fund_code", "")
        lof_price = float(body.get("lof_price", 0))
        lof_quantity = int(body.get("lof_quantity", 0))
        broker = body.get("broker", "yinhe_qmt")
        min_profit = float(body.get("min_profit", 0.25))

        if not fund_code or lof_price <= 0 or lof_quantity <= 0:
            return JSONResponse(status_code=400, content={"status": "error", "message": "缺少必要参数"})

        # 获取赎回费率
        redemption_fee = ledger_service.get_fee_rate(fund_code, broker)
        if redemption_fee <= 0:
            redemption_fee = 0.50

        # 获取预期利润（用当前 GLD 买一价算）
        meta = fund_service.get_valuation_meta(fund_code)
        rt_val = float((meta or {}).get("rt_val", 0) or 0)
        premium = (lof_price / rt_val - 1) * 100 if rt_val > 0 else 0
        expected_profit = round(abs(premium) - redemption_fee, 3)

        # 排队 LOF
        result = lazy_trader_instance.queue_lof_order(fund_code, lof_price, lof_quantity, broker)
        if result.get("status") != "ok":
            return {"status": "error", "message": "LOF下单失败", "detail": result}

        auto_open_id = result["auto_open_id"]

        # 保存 min_profit 到 pending 记录
        with lazy_trader_instance._open_lock:
            p = lazy_trader_instance._pending_opens.get(auto_open_id)
            if p:
                p["min_profit"] = min_profit

        # 确保后台检测线程已启动
        _start_auto_open_fill_checker(lazy_trader_instance)

        return {
            "status": "ok",
            "auto_open_id": auto_open_id,
            "expected_profit": expected_profit,
            "redemption_fee": redemption_fee,
        }
    except Exception as e:
        logger.error(f"[AutoOpen] queue error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/private/auto_open/execute")
async def auto_open_execute(request: Request):
    """当 LOF 已成交，手动触发或自动触发 ETF 空单执行"""
    if not lazy_trader_instance:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Lazy Trader not loaded"})
    try:
        body = await request.json()
        auto_open_id = body.get("auto_open_id", "")
        if not auto_open_id:
            return JSONResponse(status_code=400, content={"status": "error", "message": "缺少 auto_open_id"})

        pending = lazy_trader_instance.get_auto_open_status(auto_open_id)
        if pending.get("status") == "not_found":
            return JSONResponse(status_code=404, content={"status": "error", "message": "auto_open_id 不存在"})

        if pending["status"] != "queued":
            return {"status": "skipped", "message": f"当前状态 {pending['status']}，无需重复执行"}

        # 获取 ETF 信息
        fund_code = pending["fund_code"]
        lof_qty = pending["lof_quantity"]
        meta = fund_service.get_valuation_meta(fund_code)
        if not meta or not isinstance(meta, dict):
            return JSONResponse(status_code=500, content={"status": "error", "message": "无法获取基金信息"})
        cfg = meta.get("fund_config", {}) or {}
        trade_etf = cfg.get("trade_etf", "") or ""
        bd = meta.get("base_data", {}) or {}
        hedge = float(bd.get("hedge", 0) or 0)
        etf_qty = max(1, int(round(lof_qty / hedge))) if hedge > 0 else 0

        if not trade_etf or etf_qty <= 0:
            return JSONResponse(status_code=500, content={"status": "error", "message": "无法计算ETF对冲数量"})

        min_profit = pending.get("min_profit", 0.25)
        result = lazy_trader_instance.auto_short_if_profitable(
            auto_open_id, trade_etf, etf_qty, min_profit
        )
        return result
    except Exception as e:
        logger.error(f"[AutoOpen] execute error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/private/auto_open/status/{auto_open_id}")
async def auto_open_status(auto_open_id: str):
    """查询排队开仓状态"""
    if not lazy_trader_instance:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Lazy Trader not loaded"})
    state = lazy_trader_instance.get_auto_open_status(auto_open_id)
    if state.get("status") == "not_found":
        return JSONResponse(status_code=404, content={"status": "error", "message": "auto_open_id 不存在"})
    return {"status": "ok", "data": state}

@app.get("/api/private/auto_open/positions/{fund_code}")
async def auto_open_check_position(fund_code: str):
    """查询当前持仓（用于前端判断 LOF 是否已成交）"""
    if not trading_service:
        return {"status": "ok", "held": 0, "msg": "trading_service not available"}
    try:
        pos_list = trading_service.get_positions()
        held = 0
        for p in pos_list:
            if p.get("code","") == fund_code or p.get("code","") == f"{fund_code}.SZ":
                held = int(p.get("volume", 0) or p.get("available", 0))
                break
        return {"status": "ok", "held": held}
    except Exception as e:
        return {"status": "error", "message": str(e), "held": 0}

# [AI-2026-07-20] 新增：前端 preview 显示当前持仓。
# 直接走 trade_manager.query_position（8888 短连接，银河/国金/通达信脚本均支持），
# 不依赖 get_positions() 的通达信登录，确保银河渠道下能正确显示底仓（如 161116 持有 66800 股）。
@app.get("/api/private/position/{fund_code}")
async def get_fund_position(fund_code: str):
    """查询单只基金当前持仓（优先 8888 短连接，降级 get_positions）"""
    if not trading_service:
        return {"status": "ok", "held": 0, "msg": "trading_service not available"}
    try:
        tm = getattr(trading_service, 'trade_manager', None)
        held = 0
        if tm and hasattr(tm, 'query_position'):
            pos = tm.query_position(fund_code)
            if isinstance(pos, dict) and pos.get('volume'):
                held = int(pos.get('volume', 0) or 0)
        if held == 0:
            pos_list = trading_service.get_positions()
            for p in pos_list:
                if p.get("code", "") == fund_code or p.get("code", "") == f"{fund_code}.SZ":
                    held = int(p.get("volume", 0) or p.get("available", 0))
                    break
        return {"status": "ok", "held": held}
    except Exception as e:
        return {"status": "error", "message": str(e), "held": 0}

# [AI-2026-07-15] AutoClose: 自动排队平仓 API
@app.post("/api/private/auto_close/queue")
async def auto_close_queue(request: Request):
    """
    自动排队平仓：
    1. 排队卖出 LOF（不操作 ETF）
    2. 启动后台成交检测
    3. 返回 auto_close_id 供前端轮询状态
    """
    global _AUTO_CLOSE_FILL_CHECKER
    if not lazy_trader_instance:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Lazy Trader not loaded"})
    try:
        body = await request.json()
        fund_code = body.get("fund_code", "")
        lof_price = float(body.get("lof_price", 0))
        lof_quantity = int(body.get("lof_quantity", 0))
        broker = body.get("broker", "yinhe_qmt")
        min_profit = float(body.get("min_profit", -0.41))

        if not fund_code or lof_price <= 0 or lof_quantity <= 0:
            return JSONResponse(status_code=400, content={"status": "error", "message": "缺少必要参数"})

        # 排队卖出 LOF
        result = lazy_trader_instance.queue_lof_close_order(fund_code, lof_price, lof_quantity, broker)
        if result.get("status") != "ok":
            return {"status": "error", "message": "LOF卖出下单失败", "detail": result}

        auto_close_id = result["auto_close_id"]

        # 保存 min_profit 到 pending 记录
        with lazy_trader_instance._close_lock:
            p = lazy_trader_instance._pending_closes.get(auto_close_id)
            if p:
                p["min_profit"] = min_profit

        # 确保后台检测线程已启动
        _start_auto_close_fill_checker(lazy_trader_instance)

        return {
            "status": "ok",
            "auto_close_id": auto_close_id,
        }
    except Exception as e:
        logger.error(f"[AutoClose] queue error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/private/auto_close/execute")
async def auto_close_execute(request: Request):
    """当 LOF 已卖出，手动触发买回 ETF"""
    if not lazy_trader_instance:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Lazy Trader not loaded"})
    try:
        body = await request.json()
        auto_close_id = body.get("auto_close_id", "")
        if not auto_close_id:
            return JSONResponse(status_code=400, content={"status": "error", "message": "缺少 auto_close_id"})

        pending = lazy_trader_instance.get_auto_close_status(auto_close_id)
        if pending.get("status") == "not_found":
            return JSONResponse(status_code=404, content={"status": "error", "message": "auto_close_id 不存在"})

        if pending["status"] != "queued":
            return {"status": "skipped", "message": f"当前状态 {pending['status']}，无需重复执行"}

        fund_code = pending["fund_code"]
        lof_qty = pending["lof_quantity"]
        meta = fund_service.get_valuation_meta(fund_code)
        if not meta or not isinstance(meta, dict):
            return JSONResponse(status_code=500, content={"status": "error", "message": "无法获取基金信息"})
        cfg = meta.get("fund_config", {}) or {}
        trade_etf = cfg.get("trade_etf", "") or ""
        bd = meta.get("base_data", {}) or {}
        hedge = float(bd.get("hedge", 0) or 0)
        etf_qty = max(1, int(round(lof_qty / hedge))) if hedge > 0 else 0

        if not trade_etf or etf_qty <= 0:
            return JSONResponse(status_code=500, content={"status": "error", "message": "无法计算ETF对冲数量"})

        min_profit = pending.get("min_profit", -0.41)
        result = lazy_trader_instance.auto_close_if_profitable(
            auto_close_id, trade_etf, etf_qty, min_profit
        )
        return result
    except Exception as e:
        logger.error(f"[AutoClose] execute error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/private/auto_close/status/{auto_close_id}")
async def auto_close_status(auto_close_id: str):
    """查询排队平仓状态"""
    if not lazy_trader_instance:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Lazy Trader not loaded"})
    state = lazy_trader_instance.get_auto_close_status(auto_close_id)
    if state.get("status") == "not_found":
        return JSONResponse(status_code=404, content={"status": "error", "message": "auto_close_id 不存在"})
    return {"status": "ok", "data": state}

@app.get("/api/private/lazy_status")
async def lazy_status():
    """Diagnostic: check LazyTrader driver status"""
    if not lazy_trader_instance:
        return {"status": "error", "message": "Lazy Trader not loaded"}
    return {
        "status": "ok",
        "data": {
            "ib_connected": bool(getattr(lazy_trader_instance, 'ib_reader', None) and getattr(lazy_trader_instance.ib_reader, 'connected', False)),
            "ib_reader_exists": lazy_trader_instance.ib_reader is not None,
            "galaxy_qmt_exists": lazy_trader_instance.galaxy_qmt is not None,
            "guojin_qmt_exists": lazy_trader_instance.guojin_qmt is not None,
        }
    }

# --- SmartOpenMonitor API (智能开仓/平仓监控) ---
@app.post("/api/private/smart_monitor/start")
async def smart_monitor_start(request: Request):
    if not _smart_start:
        return JSONResponse(status_code=400, content={"status": "error", "message": "SmartOpenMonitor not loaded"})
    data = await request.json()
    fund_code = data.get("fund_code", "")
    direction = data.get("direction", "open")
    target_premium = data.get("target_premium", 0)
    lof_quantity = data.get("lof_quantity", 0)
    trade_etf = data.get("trade_etf", "")
    lof_broker = data.get("lof_broker", "yinhe_qmt")
    hedge_order = data.get("hedge_order", None)  # [AI-2026-08-15] 前端选择器：lof_first / ib_first（None=按fund_config默认）
    # [AI-2026-07-20] 前端算好的各档溢价率（与盘口表 tag 同源），后端直接使用不再自算
    bid_premiums = data.get("bid_premiums", None)
    ask_premiums = data.get("ask_premiums", None)
    bid_vols = data.get("bid_vols", None)
    ask_vols = data.get("ask_vols", None)
    bid_prices = data.get("bid_prices", None)
    ask_prices = data.get("ask_prices", None)
    if not fund_code or not trade_etf or not lof_quantity:
        return JSONResponse(status_code=400, content={"status": "error", "message": "缺少必要参数(fund_code/trade_etf/lof_quantity)"})
    success, msg = _smart_start(
        fund_code, direction, target_premium, lof_quantity, trade_etf, hedge_order, lof_broker,
        bid_premiums=bid_premiums, ask_premiums=ask_premiums,
        bid_vols=bid_vols, ask_vols=ask_vols,
        bid_prices=bid_prices, ask_prices=ask_prices,
        lazy_trader=_smart_lt, fund_service=_smart_fs,
        market_data_service=_smart_mds, trading_service=_smart_ts,
        trade_manager=getattr(_smart_ts, 'trade_manager', None) if _smart_ts else None,
    )
    if success:
        return {"status": "ok", "message": msg}
    return JSONResponse(status_code=400, content={"status": "error", "message": msg})

@app.post("/api/private/smart_monitor/stop")
async def smart_monitor_stop(request: Request):
    if not _smart_stop:
        return JSONResponse(status_code=400, content={"status": "error", "message": "SmartOpenMonitor not loaded"})
    data = await request.json()
    fund_code = data.get("fund_code", "")
    success, msg = _smart_stop(fund_code)
    if success:
        return {"status": "ok", "message": msg}
    return JSONResponse(status_code=400, content={"status": "error", "message": msg})

# [AI-2026-08-15] 用户点"手动撤销美股ETF"：标记 IB 腿已接管，程序放手
@app.post("/api/private/smart_monitor/mark_ib_taken_over")
async def smart_monitor_mark_ib_taken_over(request: Request):
    if not _smart_status:
        return JSONResponse(status_code=400, content={"status": "error", "message": "SmartOpenMonitor not loaded"})
    data = await request.json()
    fund_code = data.get("fund_code", "")
    from private.smart_open_monitor import get_all_monitors
    monitors = get_all_monitors()
    m = monitors.get(fund_code)
    if not m:
        return JSONResponse(status_code=400, content={"status": "error", "message": f"{fund_code} 监控器不存在"})
    m.mark_ib_leg_user_taken_over()
    return {"status": "ok", "message": f"{fund_code} 美股ETF腿已标记用户接管"}

@app.get("/api/private/smart_monitor/status")
async def smart_monitor_status(fund_code: str = Query(""),
                                rt_val: float = Query(0.0),
                                bid_premiums: str = Query(""),
                                ask_premiums: str = Query(""),
                                bid_vols: str = Query(""),
                                ask_vols: str = Query(""),
                                bid_prices: str = Query(""),
                                ask_prices: str = Query("")):
    # [AI-2026-08-19] Monitor 零计算：接收前端盘口算好的溢价率数组（与盘口表 100% 同源），写入 Monitor 实例。
    # Monitor 只比较这些溢价率，不再自算 rt_val/溢价率，杜绝前后端口径分裂。
    if fund_code:
        try:
            from private.smart_open_monitor import get_monitor
            m = get_monitor(fund_code)
            if m:
                def _parse_prem(s):
                    if not s:
                        return None
                    try:
                        return [float(x) for x in s.split(',') if x not in ('', 'None')]
                    except Exception:
                        return None
                bp = _parse_prem(bid_premiums)
                ap = _parse_prem(ask_premiums)
                bv = _parse_prem(bid_vols)
                av = _parse_prem(ask_vols)
                bpr = _parse_prem(bid_prices)
                apr = _parse_prem(ask_prices)
                if bp is not None:
                    m.bid_premiums = bp
                if ap is not None:
                    m.ask_premiums = ap
                # [AI-2026-08-19] 同源修复：盘口量一并写入，深度闸门优先用前端同源量（与盘口表一致）
                if bv is not None:
                    m.bid_vols = bv
                if av is not None:
                    m.ask_vols = av
                # [AI-2026-08-20] 同源修复：盘口价格也由前端推送，Monitor 用前端深度替代后端自抓那路
                if bpr is not None and apr is not None:
                    m._last_depth = {'bid': bpr, 'ask': apr, 'bid_vol': (bv or []), 'ask_vol': (av or [])}
                if rt_val and rt_val > 0:
                    m.backend_rt_val = rt_val  # 保留兼容字段，Monitor 已不用于决策
        except Exception:
            pass
    if not _smart_status:
        return {"status": "error", "message": "SmartOpenMonitor not loaded"}
    return _smart_status()

# [AI-2026-07-23] 获取 SmartMonitor 订单历史
@app.get("/api/private/smart_monitor/history/{direction}")
async def smart_monitor_history(direction: str):
    """获取指定方向的订单历史记录 (direction: open/close)"""
    from private.smart_open_monitor import get_all_monitors
    monitors = get_all_monitors()
    result = {}
    for fc, m in monitors.items():
        if m.direction == direction:
            result[fc] = {
                'direction': m.direction,
                'order_history': m.order_history[-20:],  # 最近 20 条
            }
    return {"status": "ok", "data": result}

# [AI-2026-07-21] 运行时更新目标溢价率（不重启 Monitor）
@app.patch("/api/private/smart_monitor/update_target")
async def smart_monitor_update_target(request: Request):
    if not _smart_update_target:
        return JSONResponse(status_code=400, content={"status": "error", "message": "SmartOpenMonitor not loaded"})
    data = await request.json()
    fund_code = data.get("fund_code", "")
    new_premium = data.get("target_premium", None)
    if new_premium is None:
        return JSONResponse(status_code=400, content={"status": "error", "message": "缺少 target_premium"})
    success, msg = _smart_update_target(fund_code, float(new_premium))
    if success:
        return {"status": "ok", "message": msg}
    return JSONResponse(status_code=400, content={"status": "error", "message": msg})

# [AI-2026-10-09 东哥拍板] 删除 /api/private/lazy_simulate/{status,control} 两个端点。
#   理由（取证结论，非推测）：
#     ① LOFarb 前端**零调用**（全仓 grep `lazy_simulate` 只命中 api/fundApi.ts 的两个封装函数，
#        而这两个函数自身无任何组件 import —— 死封装）；
#     ② 该能力已由 LOFTrader 接管并**真正在用**（LOFTrader 前端 fundApi.ts 调用同一路径，
#        后端 trading_router.py:946+ 有完整实现）。
#   留着只会让本机多一个常驻线程 + 一条启动噪音，且给人「LOFarb 还管幽灵模拟器」的错觉。

# --- Bond ETF BP Override API —— [AI-2026-10-11 东哥拍板] 整组停用（同 _BOND_MOVED，见上） ---
@app.post("/api/bond/bp-override")
async def set_bp_override(request: Request):
    """[已停用] 511520 手动 BP 覆盖 → 见 ETFarb"""
    return _BOND_MOVED

@app.get("/api/bond/bp-override")
async def get_bp_override(code: str = "511520"):
    """[已停用] 读取手动 BP 覆盖 → 见 ETFarb"""
    return _BOND_MOVED

@app.post("/api/bond/bp-override/clear")
async def clear_bp_override(request: Request):
    """[已停用] 清除手动 BP 覆盖 → 见 ETFarb"""
    return _BOND_MOVED


# --- Ledger / Bookkeeping APIs ---
@app.get("/api/ledger/trades")
async def get_ledger_trades(status: str = 'ACTIVE'):
    data = ledger_service.get_all_trades(status=status)
    return {"status": "ok", "data": data}

@app.post("/api/ledger/trades/add")
async def add_ledger_trade(request: Request):
    data = await request.json()
    success = ledger_service.add_trade(data)
    return {"status": "ok" if success else "error"}

@app.post("/api/ledger/trades/close/{trade_id}")
async def close_ledger_trade(trade_id: int):
    success = ledger_service.close_trade(trade_id)
    return {"status": "ok" if success else "error"}

# --- Arbitrage Pairs (V9.2 新账本) ---
@app.get("/api/ledger/pairs")
async def get_ledger_pairs(status: str = None):
    data = ledger_service.get_all_pairs(status=status)
    return {"status": "ok", "data": data}

@app.post("/api/ledger/pairs/add")
async def add_ledger_pair(request: Request):
    data = await request.json()
    try:
        pair_id = ledger_service.add_pair(data)
        return {"status": "ok", "pair_id": pair_id}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/ledger/pairs/update/{pair_id}")
async def update_ledger_pair(pair_id: int, request: Request):
    data = await request.json()
    success = ledger_service.update_pair(pair_id, data)
    return {"status": "ok" if success else "error"}

@app.post("/api/ledger/pairs/delete/{pair_id}")
async def delete_ledger_pair(pair_id: int):
    success = ledger_service.delete_pair(pair_id)
    return {"status": "ok" if success else "error"}

# --- 赎回提醒（OPEN 笔推算可优惠赎回日 / unfinished 待净值）---
@app.get("/api/ledger/alerts")
async def get_ledger_alerts_api():
    data = ledger_service.get_ledger_alerts()
    return {"status": "ok", "data": data}

@app.get("/api/ledger/floating")
async def get_ledger_floating_api():
    """持仓浮动跟盘：OPEN/unfinished 组实时 LOF + 美股/期货价 → 浮动盈亏。
    [AI-2026-09-21] 沿用 A 股交易时段门禁（is_quote_window），美股 ETF 用最近收盘价（非夜盘盘中价）。"""
    data = ledger_service.get_floating_pnl(market_data_service, holding_service, fund_service)
    return {"status": "ok", "data": data}

# --- 自动记录交易（QMT执行回调） ---
@app.post("/api/ledger/auto-record")
async def auto_record_trade(request: Request):
    data = await request.json()
    pair_id = ledger_service.auto_record_trade(data)
    return {"status": "ok" if pair_id > 0 else "error", "pair_id": pair_id}

# --- 获取昨日收盘价（默认填入买入单价） ---
@app.get("/api/market/prev-close/{fund_code}")
async def get_prev_close(fund_code: str):
    price = ledger_service.get_prev_close(fund_code.split('.')[0])
    return {"status": "ok", "price": price}

# --- 获取券商赎回费率（自动关联填入） ---
@app.get("/api/ledger/fee-rate")
async def get_fee_rate(fund_code: str, broker: str = ''):
    rate = ledger_service.get_fee_rate(fund_code, broker)
    return {"status": "ok", "rate": rate}

# --- Fee & Commission Management APIs ---
@app.get("/api/config/fees/{code}")
async def get_fund_fees(code: str):
    data = ledger_service.get_fund_fees(code)
    return {"status": "ok", "data": data}

@app.get("/api/ledger/broker_fees")
async def get_broker_fees():
    data = ledger_service.get_broker_redemption_fees()
    return {"status": "ok", "data": data}

@app.post("/api/ledger/broker_fees/add")
async def add_broker_fee(request: Request):
    data = await request.json()
    success = ledger_service.upsert_broker_redemption_fee(data)
    return {"status": "ok" if success else "error"}

@app.post("/api/ledger/broker_fees/delete/{fee_id}")
async def delete_broker_fee(fee_id: int):
    success = ledger_service.delete_broker_redemption_fee(fee_id)
    return {"status": "ok" if success else "error"}

@app.get("/api/exchange-rate")
async def get_exchange_rate():
    """返回最新 USD/CNY 中间价（供前端账本等使用）"""
    rate = ledger_service._get_usd_rate()
    return {"status": "ok", "rate": round(rate, 4) if rate is not None else None}

# [AI-2026-08-16] 导入 v7 套利账本 Excel -> arbitrage_pairs（upsert）
@app.post("/api/ledger/import-v7")
async def import_v7_ledger(file: UploadFile = File(None), path: str = Form(None)):
    """上传 v7 Excel 套利账本，解析并 upsert 到数据库。返回导入统计。

    [AI-2026-08-20] 新增 path 模式：传 V7 文件绝对路径，后端直接读写该文件——
    T 列汇率/N 列公式回填到【用户原文件】（上传模式回填写入临时文件即删，原文件永远不更新）。
    推荐前端传 path（本地单机，后端与 V7 同机）。
    """
    try:
        if path:
            if not os.path.exists(path):
                return {"status": "error", "message": f"V7 文件不存在: {path}"}
            result = ledger_service.import_v7(path)
            return {"status": "ok", "data": result}
        import tempfile
        content = await file.read()
        suffix = os.path.splitext(file.filename or 'v7.xlsx')[1] or '.xlsx'
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(content)
        tmp.close()
        try:
            result = ledger_service.import_v7(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
        return {"status": "ok", "data": result}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"导入 v7 失败: {e}\n{tb}")
        return {"status": "error", "message": str(e), "detail": tb}

@app.post("/api/config/fees/upsert")
async def upsert_fund_fee(request: Request):
    data = await request.json()
    success = ledger_service.upsert_fund_fee(data)
    return {"status": "ok" if success else "error"}

# --- Trading & Position APIs ---
@app.get("/api/trading/positions")
async def get_trading_positions():
    """获取真实账户持仓"""
    try:
        data = trading_service.get_positions()
        return {"status": "ok", "data": data}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/trading/balance")
async def get_trading_balance():
    """获取账户余额"""
    data = trading_service.get_balance()
    return {"status": "ok", "data": data}

@app.post("/api/trading/order")
async def place_manual_order(request: Request):
    """手动下单接口"""
    data = await request.json()
    res = trading_service.execute_order(
        action=data.get('action'),
        code=data.get('code'),
        volume=data.get('volume'),
        price=data.get('price'),
        broker=data.get('broker', 'tdx'),
        account_id=data.get('account_id')
    )
    return res

@app.post("/api/trading/ib_order")
async def place_ib_order(request: Request):
    """简单 IB 下单接口（实时沙盘用，不走 LazyTrader）"""
    try:
        body = await request.json()
        action = body.get('action', 'BUY')
        symbol = body.get('symbol', '')
        quantity = int(body.get('quantity', 0))
        price = float(body.get('price', 0))
        if not symbol or quantity <= 0 or price <= 0:
            return {"status": "error", "message": "参数不完整: symbol/quantity/price 必填"}
        ib_reader = getattr(market_data_service, 'ib_reader', None)
        if not ib_reader or not getattr(ib_reader, 'connected', False):
            return {"status": "error", "message": "IB 未连接"}
        # [AI-2026-07-02] 直接调用 ib_reader.place_us_order，不走 LazyTrader
        success, msg = ib_reader.place_us_order(symbol, action, quantity, price)
        logger.info(f"[IBOrder] {action} {quantity} {symbol} @ {price} -> {'OK' if success else 'FAIL'}: {msg}")
        return {"status": "ok" if success else "error", "message": msg}
    except Exception as e:
        logger.error(f"[IBOrder] Error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/system/accounts")
async def get_accounts():
    """从隐私配置获取交易账号列表供前端渲染，不暴露给Git"""
    try:
        from arbcore.config.account_private import YH_ACCOUNT_LIST
        return {"status": "ok", "data": YH_ACCOUNT_LIST}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": {}}

@app.post("/api/system/accounts")
async def save_accounts(request: Request):
    """保存交易账号列表到 account_private.py"""
    try:
        data = await request.json()
        accounts = data.get("accounts", {})
        
        import os
        import re
        # 定位 account_private.py
        file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "arbcore", "config", "account_private.py")
        
        if not os.path.exists(file_path):
            return {"status": "error", "message": "account_private.py 不存在"}
            
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # 构建新的字典字符串
        dict_str = "YH_ACCOUNT_LIST = {\n"
        for k in ["1", "2", "3", "4", "5", "6"]:
            val = accounts.get(k, "")
            label = "备用" if k == "6" else f"周{['一','二','三','四','五'][int(k)-1]}使用"
            dict_str += f'    "{k}": "{val}",  # {label}\n'
        dict_str += "}"
        
        # 使用正则替换
        new_content = re.sub(r'YH_ACCOUNT_LIST\s*=\s*\{[^}]*\}', dict_str, content)
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
            
        return {"status": "ok", "message": "账号保存成功，已写入隐私配置"}
    except Exception as e:
        return {"status": "error", "message": f"保存失败: {str(e)}"}

@app.post("/api/system/reconnect_ib")
async def reconnect_ib():
    """重连 IB - 使用 reconnect() 方法，试连 3 次"""
    try:
        # [V10.1] 重置熔断器
        market_data_service._circuit_reset('IB')
        if market_data_service.ib_reader:
            success, msg = market_data_service.ib_reader.reconnect()
            if success:
                system_status.add_milestone("SUCCESS", msg)
                return {"status": "ok", "message": msg}
            else:
                system_status.add_milestone("WARNING", msg)
                return {"status": "error", "message": msg}
        else:
            from arbcore.fetchers.ib_reader import IBReader
            reader = IBReader(db_manager=db)
            market_data_service.ib_reader = reader
            success, msg = reader.reconnect()
            if success:
                system_status.add_milestone("SUCCESS", msg)
                return {"status": "ok", "message": msg}
            else:
                system_status.add_milestone("WARNING", msg)
                return {"status": "error", "message": msg}
    except Exception as e:
        system_status.add_milestone("ERROR", f"IB 重连异常: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/system/reconnect_futu")
async def reconnect_futu():
    """重连富途 - 后台线程异步重连，HTTP 立即返回，避免同步阻塞导致前端超时"""
    try:
        # [V10.1] 重置熔断器
        market_data_service._circuit_reset('富途')

        def _do_reconnect():
            try:
                if market_data_service.futu_reader:
                    success, msg = market_data_service.futu_reader.reconnect()
                else:
                    from arbcore.fetchers.futu_reader import FutuReader
                    reader = FutuReader()
                    market_data_service.futu_reader = reader
                    success, msg = reader.reconnect()
                if success:
                    system_status.add_milestone("SUCCESS", msg)
                    # [AI-2026-08-20] 连上后主动播种订阅+取盘口，否则连上但 prices 空→按钮"无数据"灰
                    market_data_service._seed_futu_prices()
                else:
                    system_status.add_milestone("WARNING", msg)
            except Exception as e:
                system_status.add_milestone("ERROR", f"富途重连异常: {e}")

        # [AI-2026-08-20] 异步化：reconnect() 同步阻塞最长 ~17s（3×5s 超时+重试间隔），
        # 会先触发前端 15s axios 超时导致按钮"没反应"。改为后台线程跑，HTTP 立即返回，
        # 连接结果经 status 轮询体现在状态栏，符合富途已有 30s 懒重连设计。
        t = threading.Thread(target=_do_reconnect, daemon=True)
        t.start()
        return {"status": "pending", "message": "富途重连中，请稍候在状态栏查看"}
    except Exception as e:
        system_status.add_milestone("ERROR", f"富途重连异常: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/system/debug_tdx_order")
async def debug_tdx_order():
    """[调试保留 2026-09-03] 隔离 Monitor，直接走 tdx 通道下指定单，验证 tdx 下单能力。
    固定下 162411 买入 100股 @1.001。用于排查「Monitor 走 yinhe_qmt 报错」与 tdx 通道本身是否报错的区别。
    ★ 调试保留端点（见 docs/012_1）：每次调用会下一笔 162411@1.001 测试单，慎用于正式环境。"""
    tm = getattr(_smart_ts, 'trade_manager', None) if _smart_ts else None
    if not tm:
        return {"status": "error", "message": "trade_manager 不可用"}
    if not getattr(tm, 'tdx_available', False):
        return {"status": "error", "message": "通达信接口未就绪(tdx_available=False)，请先点「通达信」按键初始化"}
    try:
        ok, msg = tm.send_order(broker='tdx', action='BUY', symbol='162411', volume=100, price=1.001)
        # 增强：下单后查当日委托，确认委托编号与状态（Monitor 撤单/成交追踪依赖此字段）
        orders = []
        if ok and hasattr(tm, 'query_tdx_orders'):
            try:
                orders = tm.query_tdx_orders('162411') or []
            except Exception as e:
                orders = [f"query_tdx_orders 异常: {e}"]
        return {
            "status": "ok" if ok else "fail",
            "message": msg,
            "tdx_available": tm.tdx_available,
            "last_tdx_order_id": getattr(tm, 'last_tdx_order_id', ''),
            "tdx_orders_162411": orders
        }
    except Exception as e:
        import traceback
        return {"status": "exception", "message": str(e), "trace": traceback.format_exc()}


@app.post("/api/system/debug_tdx_query")
async def debug_tdx_query(code: str = '162411'):
    """[调试保留 2026-09-03] 只读查询通达信当日委托，不下一笔新单。
    用于验证某基金委托编号(Wtbh)/状态（如下单后点「确认」委托编号是否补全）。
    需先点「通达信」按键初始化 tdx 通道。★ 安全：只读，不触发任何下单。"""
    tm = getattr(_smart_ts, 'trade_manager', None) if _smart_ts else None
    if not tm:
        return {"status": "error", "message": "trade_manager 不可用"}
    if not getattr(tm, 'tdx_available', False):
        return {"status": "error", "message": "通达信接口未就绪(tdx_available=False)，请先点「通达信」按键初始化"}
    try:
        orders = tm.query_tdx_orders(code) or []
        return {"status": "ok", "code": code, "tdx_available": tm.tdx_available, "orders": orders}
    except Exception as e:
        import traceback
        return {"status": "exception", "message": str(e), "trace": traceback.format_exc()}


@app.post("/api/system/reconnect_tdx")
async def reconnect_tdx():
    """重连通达信 - 使用 reconnect() 方法，试连 3 次。
    [2026-09-02] 同时按需连接通达信交易通道(tqcenter)：点按键才连，启动时不再硬连（与银河QMT按键模式一致）。"""
    try:
        # [2026-09-02] 交易通道(tqcenter)按需连接：点「通达信」按键才初始化，启动时不连
        tm = getattr(_smart_ts, 'trade_manager', None) if _smart_ts else None
        trade_note = ''
        if tm and hasattr(tm, '_init_tdx'):
            try:
                tm._init_tdx()
                trade_note = '交易通道已就绪' if tm.tdx_available else '交易通道未就绪(请确认通达信交易软件已开并登录)'
            except Exception as e:
                trade_note = f'交易通道初始化异常: {e}'
        if market_data_service.realtime_manager:
            rm = market_data_service.realtime_manager
            tdx = rm.active_fetchers.get('tdx')
            if tdx:
                # 已在 active_fetchers 中，直接 reconnect
                success, msg = tdx.reconnect()
                combined = f"{msg} | {trade_note}"
                if success:
                    system_status.add_milestone("SUCCESS", combined)
                    return {"status": "ok", "message": combined}
                else:
                    system_status.add_milestone("WARNING", combined)
                    return {"status": "error", "message": combined}
            else:
                # V10.0 启动时跳过了客户端源，需要新创建实例并注册
                from arbcore.fetchers.realtime.tdx import TdxRealtimeFetcher
                tdx = TdxRealtimeFetcher()
                success, msg = tdx.reconnect()
                combined = f"{msg} | {trade_note}"
                if success:
                    rm.active_fetchers['tdx'] = tdx
                    if rm.symbols:
                        tdx.subscribe(rm.symbols)
                    system_status.add_milestone("SUCCESS", f"通达信 {combined}")
                    return {"status": "ok", "message": combined}
                else:
                    system_status.add_milestone("WARNING", f"通达信 {combined}")
                    return {"status": "error", "message": combined}
        else:
            system_status.add_milestone("WARNING", "实时行情管理器未启动")
            return {"status": "error", "message": "实时行情管理器未启动"}
    except Exception as e:
        system_status.add_milestone("ERROR", f"通达信重连异常: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/system/reconnect_galaxy")
async def reconnect_galaxy():
    """银河QMT仅用于下单，不参与行情轮询"""
    try:
        if not market_data_service.realtime_manager:
            system_status.add_milestone("WARNING", "实时行情管理器未启动")
            return {"status": "error", "message": "实时行情管理器未启动"}
        rm = market_data_service.realtime_manager
        galaxy = rm.active_fetchers.get('galaxy')
        if galaxy:
            # 已有实例，重新连接
            success, msg = galaxy.reconnect()
        else:
            # [AI-2026-06-29] 首次创建：仅注册到active_fetchers供按钮状态和下单使用，不订阅行情
            from arbcore.fetchers.realtime.galaxy import GalaxyQmtFetcher
            galaxy = GalaxyQmtFetcher()
            rm.active_fetchers['galaxy'] = galaxy
            success, msg = galaxy.reconnect()
        if success:
            # 更新 lazy trader 的下单通道引用
            global lazy_trader_instance
            if lazy_trader_instance is not None:
                lazy_trader_instance.inject_drivers(
                    ib_reader=getattr(market_data_service, 'ib_reader', None),
                    galaxy_qmt=galaxy,
                    guojin_qmt=rm.active_fetchers.get('guojin')
                )
            logger.info("[银河QMT] 下单通道正常，已注册active_fetchers (不参与行情轮询)")
            system_status.add_milestone("SUCCESS", "银河QMT下单通道正常 (不参与行情)")
            return {"status": "ok", "message": "银河QMT下单通道正常"}
        else:
            system_status.add_milestone("WARNING", msg)
            return {"status": "error", "message": msg}
    except Exception as e:
        logger.warning(f"[银河QMT] 连接失败: {e}")
        system_status.add_milestone("WARNING", f"银河QMT下单通道连接失败: {e}")
        return {"status": "error", "message": f"银河QMT连接失败: {e}"}

@app.post("/api/system/reconnect_guojin")
async def reconnect_guojin():
    """重连国金QMT - 使用 reconnect() 方法，试连 3 次"""
    try:
        if market_data_service.realtime_manager:
            rm = market_data_service.realtime_manager
            guojin = rm.active_fetchers.get('guojin')
            if guojin:
                success, msg = guojin.reconnect()
            else:
                # 懒创建：首次点击时创建实例并注册到管理器
                from arbcore.fetchers.realtime.guojin import GuojinQmtFetcher
                guojin = GuojinQmtFetcher()
                rm.active_fetchers['guojin'] = guojin
                success, msg = guojin.reconnect()
            if success:
                # [AI-2026-06-29] 重连后重新订阅所有已跟踪标的
                if rm.symbols:
                    guojin.subscribe(rm.symbols)
                system_status.add_milestone("SUCCESS", msg)
                return {"status": "ok", "message": msg}
            else:
                system_status.add_milestone("WARNING", msg)
                return {"status": "error", "message": msg}
        else:
            system_status.add_milestone("WARNING", "实时行情管理器未启动")
            return {"status": "error", "message": "实时行情管理器未启动"}
    except Exception as e:
        system_status.add_milestone("ERROR", f"国金QMT重连异常: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/system/reconnect_engine")
async def reconnect_engine():
    res = market_data_service.restart_realtime_engine()
    return res

@app.post("/api/system/trigger/{task}")
async def trigger_task(task: str):
    import subprocess
    # [AI-2026-06-28] 修复净值补采500错误：daily_updater.py 实际在 scheduler/ 下，不在 arbcore/scripts/
    scripts_dir = os.path.normpath(os.path.join(backend_dir, "scheduler"))
    # [AI-2026-10-07] 修正：搬入 src/ 后 backend_dir=<repo>/src/ArbDashboard/backend，
    #   仓库根是上溯 3 层（原 "..","..","LOFarb" 会算成 <repo>/src/LOFarb，不存在）。
    #   注：该变量当前未被引用（历史遗留），保留但改对，避免将来误用。
    lofarb_dir = os.path.normpath(os.path.join(backend_dir, "..", "..", ".."))
    task_map = {
        "011": os.path.join(scripts_dir, "daily_updater.py"),
        "012": [os.path.join(scripts_dir, "daily_updater.py"), "--static-valuation"],
        "nav": [os.path.join(scripts_dir, "daily_updater.py"), "--nav-only"],
        "morning": [os.path.join(scripts_dir, "daily_updater.py"), "--refresh-morning"]
    }
    if task not in task_map:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid task"})

    task_entry = task_map[task]
    if isinstance(task_entry, list):
        script_path = task_entry[0]
        extra_args = task_entry[1:]
    else:
        script_path = task_entry
        extra_args = []
    
    # [V4.1] 尝试多种 Python 路径
    python_exe_candidates = [
        os.path.normpath(os.path.join(backend_dir, "..", ".venv", "Scripts", "python.exe")),  # ArbDashboard/.venv (正确)
        r"C:\Users\milan\AppData\Local\Programs\Python\Python311\python.exe",  # 系统 Python311
        "python",  # PATH
    ]
    
    python_exe = None
    for candidate in python_exe_candidates:
        if os.path.exists(candidate):
            python_exe = candidate
            logger.info(f"✅ 找到 Python: {python_exe}")
            break
    
    if not python_exe:
        error_msg = "未找到可用的 Python 解释器"
        system_status.add_milestone("ERROR", error_msg)
        logger.error(f"❌ {error_msg}")
        return JSONResponse(status_code=500, content={"status": "error", "message": error_msg})
    
    try:
        logger.info(f"🚀 启动任务 {task}: {script_path}")
        logger.info(f"🐍 Python: {python_exe}")
        
        # [V4.1] 验证路径有效性
        script_dir = os.path.dirname(script_path)
        if not os.path.exists(script_dir):
            error_msg = f"脚本目录不存在: {script_dir}"
            system_status.add_milestone("ERROR", error_msg)
            logger.error(f"❌ {error_msg}")
            return JSONResponse(status_code=500, content={"status": "error", "message": error_msg})
        
        if not os.path.exists(script_path):
            error_msg = f"脚本文件不存在: {script_path}"
            system_status.add_milestone("ERROR", error_msg)
            logger.error(f"❌ {error_msg}")
            return JSONResponse(status_code=500, content={"status": "error", "message": error_msg})
        
        cmd = [python_exe, script_path] + extra_args
        launched = _popen_script_once(cmd, script_dir, os.path.basename(script_path))
        if not launched:
            return {"status": "ok", "message": f"Task {task} already running"}
        system_status.add_milestone("INFO", f"后台任务 {task} 已手动启动")
        logger.info(f"✅ 手动触发任务 {task}: {' '.join(cmd)}")
        if task == "nav":
            global _nav_last_updated
            _nav_last_updated["time"] = datetime.now().strftime("%H:%M")
            _nav_last_updated["date"] = datetime.now().strftime("%Y-%m-%d")
        return {"status": "ok", "message": f"Task {task} started in background"}
    except Exception as e:
        error_msg = f"后台任务启动失败: {e}"
        system_status.add_milestone("ERROR", error_msg)
        logger.error(f"❌ {error_msg}")
        return JSONResponse(status_code=500, content={"status": "error", "message": error_msg})

@app.get("/api/system/nav-status")
async def get_nav_status():
    """返回净值最后更新时间，供前端展示提醒"""
    global _nav_last_updated
    today = datetime.now().strftime("%Y-%m-%d")
    today_updated = _nav_last_updated.get("date") == today
    return {
        "status": "ok",
        "data": {
            "last_updated_time": _nav_last_updated.get("time"),
            "last_updated_date": _nav_last_updated.get("date"),
            "today_updated": today_updated
        }
    }

@app.get("/api/system/data-status")
async def get_data_status():
    """返回今日各项数据同步状态（供前端展示）"""
    global _morning_refreshed_today
    today = datetime.now().strftime("%Y-%m-%d")
    sources = {
        "woody_lof_batch": "Woody因子",
        "official_exchange_rate": "官方汇率",
        "futures_data": "期货结算价",
        "jsl_shares_data": "场内份额",
    }
    status = {}
    for key, label in sources.items():
        synced = db.is_access_synced_today(today, source=key)
        status[key] = {"label": label, "synced": synced}
    status["nav"] = {"label": "基金净值", "synced": False}
    status["morning"] = {"label": "清晨数据", "synced": _morning_refreshed_today}
    # 统计
    morning_ok = all(status[k]["synced"] for k in sources)
    return {
        "status": "ok",
        "data": {
            "sources": status,
            "morning_ready": _morning_refreshed_today,
            "all_morning_done": morning_ok,
            "today": today
        }
    }

# [AI-2026-07-25] 已删除 /api/system/health-check 与 /api/system/runtime-health 端点（方案C：健康检查功能无调用方，华而不实）

# [AI-2026-08-22] 旧版文件型信号监测引擎端点已废弃删除（single source of truth = DB 驱动 rule_engine）

# --- [AI-2026-07-01] RuleEngine API (DB驱动，LazyMode 前端用) ---
@app.get("/api/rule_engine/status")
async def get_rule_engine_status():
    if not rule_engine:
        return {"status": "error", "message": "RuleEngine not loaded", "running": False, "rules": []}
    rules = rule_engine.get_all_rules()
    # [AI-2026-08-23] 返回交易数量配置，供前端展示每笔LOF/ETF数量
    qty_map = rule_engine.get_all_trade_configs()
    return {"status": "ok", "running": rule_engine.running, "rules": rules, "trigger_qty": qty_map}

# [AI-2026-08-23] 保存基金交易数量配置
@app.post("/api/rule_engine/trade_config")
async def save_trade_config(request: Request):
    if not rule_engine:
        return JSONResponse(status_code=400, content={"status": "error", "message": "RuleEngine not loaded"})
    data = await request.json()
    fund_code = data.get("fund_code")
    lof_qty = int(data.get("lof_qty", 10000))
    etf_qty = int(data.get("etf_qty", 60))
    if not fund_code:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing fund_code"})
    ok = rule_engine.save_trade_config(fund_code, lof_qty, etf_qty)
    return {"status": "ok" if ok else "error"}

@app.post("/api/rule_engine/toggle")
async def toggle_rule_engine(request: Request):
    if not rule_engine:
        return JSONResponse(status_code=400, content={"status": "error", "message": "RuleEngine not loaded"})
    data = await request.json()
    action = data.get("action")
    if action == "start":
        rule_engine.start()
        return {"status": "ok", "running": True}
    elif action == "stop":
        rule_engine.stop()
        return {"status": "ok", "running": False}
    return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid action"})

@app.post("/api/rule_engine/rule_add")
async def add_rule_engine_rule(request: Request):
    if not rule_engine:
        return JSONResponse(status_code=400, content={"status": "error", "message": "RuleEngine not loaded"})
    data = await request.json()
    # 自动映射 hedge_symbol
    fund_code = data.get("fund_code", "")
    hedge = data.get("hedge_symbol", "")
    if not hedge:
        hedge = rule_engine._lookup_hedge(fund_code)
    result = rule_engine.add_rule({
        "fund_code": fund_code,
        "hedge_symbol": hedge,
        "direction": data.get("direction", "open"),
        "condition": data.get("condition", "lt"),
        "threshold": data.get("threshold", -0.5),
        "pos_constraint": data.get("pos_constraint"),
        "pos_value": data.get("pos_value"),
        "cash_constraint": data.get("cash_constraint"),
        "cash_value": data.get("cash_value"),
        "enabled": data.get("enabled", True),
        "note": data.get("note", ""),
    })
    return result

@app.post("/api/rule_engine/rule_update/{rule_id}")
async def update_rule_engine_rule(rule_id: int, request: Request):
    if not rule_engine:
        return JSONResponse(status_code=400, content={"status": "error", "message": "RuleEngine not loaded"})
    data = await request.json()
    success = rule_engine.update_rule(rule_id, data)
    return {"status": "ok" if success else "error"}

@app.delete("/api/rule_engine/rule/{rule_id}")
async def delete_rule_engine_rule(rule_id: int):
    if not rule_engine:
        return JSONResponse(status_code=400, content={"status": "error", "message": "RuleEngine not loaded"})
    rule_engine.delete_rule(rule_id)
    return {"status": "ok"}

@app.get("/api/rule_engine/logs")
async def get_rule_engine_logs():
    if not rule_engine:
        return {"status": "ok", "logs": []}
    return {"status": "ok", "logs": rule_engine.get_recent_logs()}

# [AI-2026-08-22] SignalDetector 端点已废弃删除（唯一策略引擎 = DB 驱动 rule_engine，端点见上方 /api/rule_engine/*）

# --- Data Source Config APIs ---
@app.get("/api/config/data_sources")
async def get_data_sources(module: str = "realtime_market"):
    data = config_service.get_data_sources(module)
    return {"status": "ok", "data": data}

@app.post("/api/config/data_sources/update")
async def update_data_source(request: Request):
    data = await request.json()
    res = config_service.update_source_config(
        module=data.get('module', 'realtime_market'),
        source_name=data.get('source_name'),
        priority=data.get('priority'),
        is_active=data.get('is_active'),
        config=data.get('config')
    )
    return res

@app.post("/api/config/data_sources/priority")
async def update_priorities(request: Request):
    data = await request.json()
    res = config_service.update_priorities(
        module=data.get('module', 'realtime_market'),
        priorities=data.get('priorities', [])
    )
    # [2026-08-24] 取消实时行情优先级功能：注释掉引擎重启，避免阻塞事件循环导致请求超时
    # market_data_service.restart_realtime_engine()
    return res


# --- Market Data APIs ---
@app.get("/api/market/realtime/{code}")
def get_realtime_quote(code: str):
    quote = market_data_service.get_realtime_quote(code)
    if quote:
        return {"status": "ok", "data": quote}
    return JSONResponse(status_code=404, content={"status": "error", "message": "Quote not found"})


# [AI-2026-08-03] 双源盘口对比（只读，供实时沙盘对比 IB 与富途的时效/准确性）。
# 估值计算仍只用 IB，本接口仅展示两源的 bid/ask/量，不做任何计算/门禁。
@app.get("/api/market/realtime_dual")
def get_realtime_dual(code: str = None, codes: str = None):
    syms = []
    if codes:
        syms = [s.strip() for s in codes.split(',') if s.strip()]
    elif code:
        syms = [code.strip()]
    result = {s: market_data_service.get_dual_quote(s) for s in syms}
    return {"status": "ok", "data": result}


@app.get("/api/funds/realtime_est")
def get_funds_realtime_est():
    """批量实时估值快照 —— [AI-2026-08-26] 优先读快照服务缓存（内存合并，毫秒级稳定）。

    旧实现直接调 fund_service.get_unified_dashboard_data() 全量现算：缓存 TTL 仅 5s，
    push_rt 每 15s 来一次必触发重算，ARM 弱机上 30~60s 算不完 → 25s 超时 → H5 实时估值
    时有时无（实测 36 只 ↔ 0 只反复横跳）。快照服务交易时段本就按分类持续刷新，
    这里直接合并其内存快照，与主面板 /api/dashboard 完全同源同新鲜度。
    快照完全为空（服务刚启动未算完）时才回退现算兜底。
    返回: {status, funds: {code: {price, rt_val, rt_premium, category, name}}}
    """
    data = None
    # ── 首选：读快照服务内存缓存（按分类循环持续刷新，纯内存读取） ──
    try:
        with dashboard_snapshot_service._lock:
            snaps = dict(dashboard_snapshot_service._snapshots)
        merged: list = []
        seen_codes: set = set()
        for snap in snaps.values():
            for r in (snap.get("data") or []):
                code = r.get("fund_code")
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                merged.append(r)
        if merged:
            data = merged
    except Exception as e:
        logger.warning(f"[realtime_est] 读快照缓存失败，回退现算: {e}")
    # ── 兜底：快照为空（刚启动未算完）才现算 ──
    if data is None:
        try:
            data = fund_service.get_unified_dashboard_data()
        except Exception as e:
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    out = {}
    for r in (data or []):
        code = r.get("fund_code")
        if not code:
            continue
        out[code] = {
            "price": r.get("price"),
            # [AI-2026-08-03] realtime_price 是盘中 LOF 实时价（腾讯/新浪），与 DB 收盘价(price) 不同。
            # H5 "现价"字段应优先用 realtime_price（src=realtime 时），否则显示昨收价误导用户。
            "realtime_price": r.get("realtime_price"),
            "rt_val": r.get("rt_val"),
            "rt_premium": r.get("rt_premium"),
            "category": r.get("category"),
            "name": r.get("fund_name"),
            # [AI-2026-10-09] 冻结机制已取消（东哥拍板：程序估值与时间无关）：get_unified_dashboard_data()
            # 不再调 apply_freeze_to_dashboard()，rt_frozen / rt_frozen_note 恒不写入 ⇒ 本透传恒 None。
            # 字段保留仅为 H5 消费端兼容（Dashboard.vue / H5 卡片仍读该键，无值即不画「冻」徽章）。
            "rt_frozen": r.get("rt_frozen"),
            "rt_frozen_note": r.get("rt_frozen_note"),
        }
    return {"status": "ok", "funds": out}

@app.get("/api/funds/realtime_detail")
def get_fund_realtime_detail(code: str = Query(..., description="基金代码")):
    """单只基金实时估值明细（H5 详情页计算依据）。"""
    try:
        detail = fund_service.get_realtime_valuation_detail(code)
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    if not detail:
        return JSONResponse(status_code=404, content={"status": "error", "message": f"未找到 {code} 的实时估值明细"})
    return {"status": "ok", "data": detail}


# [AI-2026-08-05] 生产封装入口：单只实时估值（rt_val + premium + 对冲数量），包 analyze_realtime。
# 供前端 LazyMode / 沙盘估值计算器 替代各自前端的手算估值公式，前后端统一走 canonical 引擎，消除分叉/近似 bug。
@app.get("/api/funds/realtime_calc")
def calc_realtime_valuation(
    code: str = Query(..., description="基金代码"),
    lof_price: float = Query(0.0, description="LOF 实时价（默认0→用基准收盘）"),
    fx: float = Query(0.0, description="当前汇率（默认0→用基准汇率）"),
    etfs: str = Query("", description="各成分实时价 JSON，如 {\"GLD\":381.4,\"^GLD-EU\":381.0}"),
    lof_qty: float = Query(0.0, description="LOF 股数（>0 才返回对冲数量）"),
):
    """单只实时估值（生产封装 analyze_realtime）—— 前端实时估值统一入口。"""
    try:
        import json
        etf_map = json.loads(etfs) if etfs else {}
        from arbcore.analysis import analyze_realtime as _ar
        r = _ar(
            code,
            current_price=lof_price if lof_price > 0 else None,
            current_fx=fx if fx > 0 else None,
            current_etfs=etf_map,
            lof_qty=lof_qty if lof_qty > 0 else None,
        )
        if not r:
            return JSONResponse(status_code=422, content={"status": "error", "message": f"{code} 实时估值计算失败（数据缺失）"})
        return {
            "status": "ok",
            "code": code,
            "rt_val": r.get("rt_val"),
            "premium": r.get("premium"),
            "quantity": r.get("quantity"),
        }
    except Exception as e:
        logger.error(f"[{code}] /api/funds/realtime_calc 异常: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# [AI-2026-08-05] 生产封装入口：期货估值（期货校准 / 纯期货）。包 analyze_realtime_futures /
# analyze_realtime_pure_futures，消除前端沙盘手算分叉。基准价缺失返回 422（不兜底）。
@app.get("/api/funds/realtime_futures_calc")
def calc_realtime_futures_valuation(
    code: str = Query(..., description="基金代码"),
    mode: str = Query("calib", description="calib=期货校准 / pure=纯期货"),
    futures_price: float = Query(..., description="期货合约实时价"),
    calibration: float = Query(1.0, description="期现校准系数（calib 模式用）"),
    lof_price: float = Query(0.0, description="LOF 实时价（默认0→用基准收盘）"),
    fx: float = Query(0.0, description="当前汇率（默认0→用基准汇率）"),
    lof_qty: float = Query(0.0, description="LOF 股数（>0 才返回对冲手数）"),
):
    """期货实时估值（生产封装）—— 前端沙盘期货校准/纯期货统一入口。"""
    try:
        from arbcore.analysis import analyze_realtime_futures, analyze_realtime_pure_futures
        if mode == "pure":
            r = analyze_realtime_pure_futures(
                code, futures_price,
                current_price=lof_price if lof_price > 0 else None,
                current_fx=fx if fx > 0 else None,
                lof_qty=lof_qty if lof_qty > 0 else None)
        else:
            r = analyze_realtime_futures(
                code, futures_price, calibration=calibration,
                current_price=lof_price if lof_price > 0 else None,
                current_fx=fx if fx > 0 else None,
                lof_qty=lof_qty if lof_qty > 0 else None)
        if not r:
            return JSONResponse(status_code=422,
                                content={"status": "error", "message": f"{code} 期货估值计算失败（数据缺失：期货基准价/净值/汇率/position）"})
        return {"status": "ok", "code": code, "mode": r.get("mode"),
                "rt_val": r.get("rt_val"), "premium": r.get("premium"),
                "base_future": r.get("base_future"), "quantity": r.get("quantity")}
    except Exception as e:
        logger.error(f"[{code}] /api/funds/realtime_futures_calc 异常: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/market/historical/nav/{code}")
async def get_hist_nav(code: str, start_date: str = None):
    data = market_data_service.get_historical_nav(code, start_date=start_date)
    return {"status": "ok", "data": data}

@app.get("/api/market/historical/price/{code}")
async def get_hist_price(code: str, start_date: str = None):
    data = market_data_service.get_historical_prices(code, start_date=start_date)
    return {"status": "ok", "data": data}

# --- ETF Rotation APIs (程序4 融合) ---
@app.get("/api/etf-rotation/list")
async def get_etf_rotation_list():
    """获取 ETF 轮动分组配置"""
    data = await asyncio.to_thread(etf_rotation_service.get_rotation_list)
    return {"status": "ok", "data": data}

@app.get("/api/etf-rotation/prices")
async def get_etf_rotation_prices():
    """获取 ETF 轮动实时价格和估值"""
    data = await asyncio.to_thread(etf_rotation_service.get_rotation_prices)
    return {"status": "ok", "data": data}

@app.get("/api/etf-rotation/fx")
async def get_etf_rotation_fx():
    """获取 USD/CNY 实时在岸价"""
    rate = await asyncio.to_thread(etf_rotation_service.get_realtime_fx_spot)
    return {"status": "ok", "data": {"fx_spot": rate}}

@app.get("/api/etf-rotation/history/{group_id}")
async def get_etf_rotation_history(group_id: int):
    """获取某分组的轮动历史数据"""
    data = await asyncio.to_thread(etf_rotation_service.get_group_history, group_id)
    return {"status": "ok", "data": data}


@app.get("/api/silver/ratio")
# [AI-2026-09-02] 改为同步 def：函数体内全是同步 DB/网络调用，交给 FastAPI 线程池执行，
# 避免阻塞 asyncio 事件循环（原 async def 会把整个 uvicorn 一起堵住）。
def get_silver_ratio():
    """白银比价数据: (AG_settle / 1000 × 31.1035 / USDCNH) / SI_close
    与 Woody stockhistorycn.php 完全一致的公式
    
    处理逻辑（对齐 Woody）：
    - 历史日期：从 futures_daily + exchange_rate 读取（稳定）
    - 今日日期：SI 和 CNH 从新浪实时抓取（Woody 每个页面访问都覆写 today row）
    - 比价公式与 Woody stockhistoryparagraph.php 完全一致
    """
    conn = fund_service.db._get_conn()
    today_str = datetime.now().strftime('%Y-%m-%d')
    now_hour = datetime.now().hour
    
    # 21:00 定稿：AG0 夜盘 21:00 开新一天的交易，21:00 后的实时价属于"明天"
    # 对齐 Woody mystockref.php: $iHourMinute < 2100 才 _updateStockHistory
    is_finalized = now_hour >= 21  # 21:00+ 今日数据定稿，不再覆写
    
    try:
        cursor = conn.cursor()
        # [AI-2026-07-08] 改用 在岸价 (usd_cny_spot) -> CNH -> 中间价 三级回退，Woody 实际使用在岸价
        cursor.execute("""
            SELECT
                a.date,
                a.close_price AS ag_close,
                a.settle_price AS ag_settle,
                a.volume AS ag_volume,
                s.close_price AS si_close,
                COALESCE(e.usd_cny_spot, e.usd_cnh, e.usd_cny_mid) AS usd_cny_spot
            FROM futures_daily a
            LEFT JOIN futures_daily s ON a.date = s.date AND s.symbol = 'SI'
            LEFT JOIN exchange_rate e ON a.date = e.date
            WHERE a.symbol = 'AG0'
              AND a.close_price IS NOT NULL
              AND s.close_price IS NOT NULL
              AND (e.usd_cny_spot IS NOT NULL OR e.usd_cnh IS NOT NULL OR e.usd_cny_mid IS NOT NULL)
            ORDER BY a.date DESC
        """)
        rows = cursor.fetchall()
        data = []
        
        # 实时抓取今日 SI 和 在岸价 USDCNY（对齐 Woody：21:00 前每次页面访问都覆写 today row）
        today_si = None
        today_spot = None
        if not is_finalized:
            try:
                si_raw = market_data_service.data_fetcher.fetch_si_from_sina()
                if si_raw and si_raw.get('price', 0) > 0:
                    today_si = si_raw['price']
                # Woody 实际使用在岸价 (USDCNY)，不是 CNH
                spot_raw = market_data_service.data_fetcher.fetch_cny_spot_rate()
                if spot_raw and spot_raw.get('人民币在岸价', 0) > 0:
                    today_spot = spot_raw['人民币在岸价']
            except Exception as e:
                logger.warning(f"[白银比价] 实时获取 SI/USDCNY 失败: {e}")
        else:
            logger.info(f"[白银比价] 今日数据已定稿 (≥21:00)，使用数据库存档值")
        
        for row in rows:
            row_date = row[0]
            ag_close = float(row[1]) if row[1] is not None else None
            ag_settle = float(row[2]) if row[2] is not None else ag_close
            ag_volume = int(row[3]) if row[3] is not None else None
            
            # 今日行：用实时 SI 和 USDCNY 覆盖（对齐 Woody 的 _updateStockHistory 机制）
            if row_date == today_str:
                si_close = today_si if today_si else float(row[4])
                usd_cny_spot = today_spot if today_spot else float(row[5])
            else:
                si_close = float(row[4])
                usd_cny_spot = float(row[5])
            
            ratio = (ag_settle / 1000.0 * 31.1035 / usd_cny_spot) / si_close if (usd_cny_spot > 0 and si_close > 0 and ag_settle) else None
            data.append({
                "date": row_date,
                "ag_close": ag_close,
                "ag_settle": ag_settle,
                "ag_volume": ag_volume,
                "si_close": round(si_close, 2),
                "usd_cny_spot": round(usd_cny_spot, 4),
                "ratio": round(ratio, 4) if ratio else None
            })
        
        # 如果数据库还没有今日行但实时数据可用，拼一个今日行（用 AG0 新浪实时数据）
        if (not data or data[0]['date'] != today_str) and today_si and today_spot:
            try:
                ag0_raw = market_data_service.data_fetcher.fetch_ag0_from_sina()
                if ag0_raw:
                    ag_close = ag0_raw.get('price')
                    ag_settle = ag0_raw.get('settle') or ag_close  # settle = parts[9] (今日结算价)
                    ag_volume = ag0_raw.get('volume')
                    ratio = None
                    if ag_settle and today_si > 0 and today_spot > 0:
                        ratio = (ag_settle / 1000.0 * 31.1035 / today_spot) / today_si
                    today_row = {
                        "date": today_str,
                        "ag_close": ag_close,
                        "ag_settle": ag_settle,
                        "ag_volume": ag_volume,
                        "si_close": round(today_si, 2),
                        "usd_cny_spot": round(today_spot, 4),
                        "ratio": round(ratio, 4) if ratio else None
                    }
                    data.insert(0, today_row)
            except Exception as e:
                logger.warning(f"[白银比价] 实时获取 AG0 失败: {e}")
        
        return {"status": "ok", "data": data}
    except Exception as e:
        logger.error(f"获取白银比价失败: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


# ==============================================================
# [AI-2026-08-02] 东京 H5 后台维护面板（本机主程序内置，东哥专用）
# --------------------------------------------------------------
# 背景：hehuan.qzz.io 由东京 VPS 用 Nginx 静态托管，我们不掌握它的"后台"。
#      但本机主程序手握三样东西：
#        ① 本机 ground-truth 估值（arbcore 现算，可作比对基准）
#        ② 东京 SFTP 凭据（account_private.py 的 VPS_*，路线1 推送脚本已在用）
#        ③ 本机 /api/status（本机实例健康）
#      所以"看东京对不对、不对一键重推"放在本机主程序是最自然的落点。
# 原则：只读公网 JSON + 与本机估值逐只比对 + 手动重推；东京程序零改动。
#      电脑关机时由 ARM 的 systemd timer 自动顶上，两者互补。
# ==============================================================
# 取数逻辑抽在 arbcore/utils/tokyo_client.py（处理了 IPv6 黑洞 + CF 403 两个坑），
# 与命令行诊断脚本 arbcore/scripts/check_tokyo_reachable.py 共用同一份实现。
from arbcore.utils.tokyo_client import (
    TOKYO_BASE,
    fetch_tokyo_json as _tokyo_fetch,
    age_minutes as _age_min,
)

H5WEB_DIR = os.path.join(repo_root, "deploy", "H5web")


@app.get("/api/tokyo/status")
def tokyo_status():
    """东京 H5 当前对外提供的数据是什么、什么时候生成的、新不新鲜。

    [AI-2026-09-30] 同步 `def`：_tokyo_fetch(fetch_tokyo_json, timeout=20s) 本函数调 2 次
    （最坏 40s），写 async 会焊死单 worker 事件循环 → 交给 FastAPI 丢线程池。
    """
    out = {
        "status": "ok",
        "tokyo_base": TOKYO_BASE,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        d = _tokyo_fetch("fund_data.json")
        tabs = d.get("tabs") or {}
        out["fund_data"] = {
            "ok": True,
            "generated_at": d.get("generated_at"),
            "age_minutes": _age_min(d.get("generated_at")),
            "source": (d.get("meta") or {}).get("source"),
            "tabs": {k: len(v or []) for k, v in tabs.items()},
            "total": sum(len(v or []) for v in tabs.values()),
        }
    except Exception as e:
        out["fund_data"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        d = _tokyo_fetch("fund_rt.json")
        out["fund_rt"] = {
            "ok": True,
            "generated_at": d.get("generated_at"),
            "age_minutes": _age_min(d.get("generated_at")),
            "count": len(d.get("funds") or []),
            "note": (d.get("meta") or {}).get("note"),
        }
    except Exception as e:
        out["fund_rt"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        snap = dashboard_snapshot_service.get_snapshot()
        out["local"] = {
            "updated_at": snap.get("updated_at"),
            "stale": snap.get("stale", False),
            "count": len(snap.get("data") or []),
        }
    except Exception as e:
        out["local"] = {"error": f"{type(e).__name__}: {e}"}
    return out


@app.get("/api/tokyo/diff")
def tokyo_diff(tol: float = 0.5):
    """东京当前数据 vs 本机现算估值，逐只比对。

    [AI-2026-09-30] 同步 `def`：_tokyo_fetch 为同步 HTTP（timeout=20s），同 /api/tokyo/status。

    tol: 静态估值相对偏差阈值(%)，超过则标记 deviate。
    """
    try:
        d = _tokyo_fetch("fund_data.json")
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "status": "error", "message": f"读取东京 fund_data.json 失败: {e}"})

    tokyo = {}
    for cat, items in (d.get("tabs") or {}).items():
        for it in (items or []):
            tokyo[str(it.get("code"))] = dict(it, cat=cat)

    try:
        snap = dashboard_snapshot_service.get_snapshot()
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "status": "error", "message": f"读取本机快照失败: {e}"})
    local = {str(f.get("fund_code")): f for f in (snap.get("data") or [])}

    # [AI-2026-08-02] 本机快照还没算出来时直接说清楚，不要把 30 只全标成 extra_tokyo 误导人
    if not local:
        return {
            "status": "local_not_ready",
            "message": "本机估值快照尚未生成（主程序刚启动或未到刷新点），暂无法比对。等本机算出一轮再点「重新比对」。",
            "tokyo_generated_at": d.get("generated_at"),
            "tokyo_source": (d.get("meta") or {}).get("source"),
            "tokyo_total": len(tokyo),
            "local_updated_at": snap.get("updated_at"),
            "total": 0,
            "problem": 0,
            "rows": [],
        }

    rows, n_bad = [], 0
    for code in sorted(set(tokyo) | set(local)):
        t = tokyo.get(code)
        l = local.get(code)
        tsv = (t or {}).get("static_val")
        lsv = (l or {}).get("static_val")
        dev = round((tsv / lsv - 1) * 100, 3) if (tsv and lsv) else None
        if t is None:
            flag = "missing_tokyo"      # 本机有、东京没有（东京数据缺只）
        elif l is None:
            flag = "extra_tokyo"        # 东京有、本机没有（可能是已下架/分类变更）
        elif tsv is None or lsv is None:
            flag = "no_static_val"
        elif abs(dev) > tol:
            flag = "deviate"            # 估值对不上，重点看
        else:
            flag = "ok"
        if flag != "ok":
            n_bad += 1
        rows.append({
            "code": code,
            "name": (t or {}).get("name") or (l or {}).get("fund_name"),
            "cat": (t or {}).get("cat") or (l or {}).get("category"),
            "tokyo_static_val": tsv,
            "local_static_val": round(lsv, 4) if lsv is not None else None,
            "dev_pct": dev,
            "tokyo_price": (t or {}).get("price"),
            "local_price": (l or {}).get("price"),
            "tokyo_static_val_date": (t or {}).get("static_val_date"),
            "tokyo_nav_date": (t or {}).get("nav_date"),
            "flag": flag,
        })
    return {
        "status": "ok",
        "tol_pct": tol,
        "tokyo_generated_at": d.get("generated_at"),
        "tokyo_source": (d.get("meta") or {}).get("source"),
        "local_updated_at": snap.get("updated_at"),
        "total": len(rows),
        "problem": n_bad,
        "rows": rows,
    }


@app.post("/api/tokyo/repush")
async def tokyo_repush(request: Request):
    """用【本机】数据重推东京（export -> upload[-> push_rt]）。

    需要 body {"confirm": true}，避免误触。
    注意：这会把东京上 ARM 推的数据覆盖成本机数据；下次 ARM timer 到点(每10分钟)
    又会推回 ARM 版本。所以这是"人工干预/救火"手段，不是常态。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not body.get("confirm"):
        return JSONResponse(status_code=400, content={
            "status": "error", "message": "需要 confirm=true 才执行重推"})

    steps = [
        ("export 本机估值", os.path.join(H5WEB_DIR, "export_fund_data_json.py")),
        ("upload 东京", os.path.join(H5WEB_DIR, "upload_fund_data.py")),
    ]
    if body.get("include_rt"):
        steps.append(("push_rt 实时快照", os.path.join(H5WEB_DIR, "push_rt.py")))

    results = []
    for name, script in steps:
        if not os.path.exists(script):
            results.append({"step": name, "ok": False, "output": f"脚本不存在: {script}"})
            break
        try:
            p = subprocess.run(
                [sys.executable, script],
                cwd=arbcore_parent, capture_output=True, text=True,
                timeout=180, encoding="utf-8", errors="replace",
            )
            ok = (p.returncode == 0)
            results.append({
                "step": name, "ok": ok, "returncode": p.returncode,
                "output": ((p.stdout or "") + (p.stderr or ""))[-3000:],
            })
            if not ok:
                break
        except Exception as e:
            results.append({"step": name, "ok": False, "output": f"{type(e).__name__}: {e}"})
            break

    all_ok = bool(results) and all(r["ok"] for r in results)
    logger.info(f"[东京维护] 手动重推结果 all_ok={all_ok} steps={[r['step'] for r in results]}")
    return {"status": "ok" if all_ok else "error", "results": results}


@app.get("/api/tokyo/admin")
async def tokyo_admin_page():
    """东京 H5 后台维护面板（自包含单页，无需前端构建）。"""
    from fastapi.responses import HTMLResponse
    page_path = os.path.join(H5WEB_DIR, "tokyo_admin.html")
    if os.path.exists(page_path):
        with open(page_path, "r", encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
    return HTMLResponse("<h3>缺少 deploy/H5web/tokyo_admin.html</h3>", status_code=404)


# ==============================================================
# [V6.5] 静态前端挂载 (公网部署与动静合一)
# 允许使用 512M 小内存 VPS 同时提供 Backend API 和 Frontend
# ==============================================================
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 禁用浏览器缓存前端静态资源，避免重构建后用户仍看到旧 JS/CSS/HTML
NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0, pragma: no-cache"}

class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE_HEADERS)
        return response

# 优先查找上一级目录的 frontend/dist
frontend_dist_path = os.path.join(workspace_root, "frontend", "dist")

if os.path.exists(frontend_dist_path):
    assets_dir = os.path.join(frontend_dist_path, "assets")
    if os.path.exists(assets_dir):
        logger.info(f"Detected frontend dist at {frontend_dist_path}, mounting static files.")
        app.mount("/assets", NoCacheStaticFiles(directory=assets_dir), name="assets")
    # SPA Fallback: 用 middleware 代替 catch-all 路由，避免拦截 /api/* 请求
    from starlette.middleware.base import BaseHTTPMiddleware
    class SPAMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if response.status_code == 404:
                path = request.url.path
                # 静态资源和 API 不做 fallback
                if path.startswith("/api/") or path.startswith("/assets/"):
                    return response
                return FileResponse(os.path.join(frontend_dist_path, "index.html"), headers=NO_CACHE_HEADERS)
            return response
    app.add_middleware(SPAMiddleware)

def kill_port_owner(port: int):
    """
    [Windows 强力补丁] 启动前强行终止占用指定端口的旧残留进程，防止端口冲突闪退。
    """
    if sys.platform != "win32":
        return
    import subprocess
    import re
    import time
    try:
        # 运行 netstat -ano 查找处于 LISTENING 状态的对应端口行
        output = subprocess.check_output(f'netstat -ano | findstr LISTENING | findstr :{port}', shell=True).decode('utf-8')
        lines = output.strip().split('\n')
        for line in lines:
            if not line: continue
            parts = re.split(r'\s+', line.strip())
            if len(parts) >= 5:
                # 最后一列为 PID，倒数第二列是 LISTENING
                if parts[-2] == 'LISTENING':
                    pid = int(parts[-1])
                    if pid > 0 and pid != os.getpid():
                        logger.info(f"🚨 [端口防护] 检测到端口 {port} 被旧进程 (PID: {pid}) 占用，正在强行终止释放端口...")
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        logger.info(f"✅ [端口防护] 成功终止旧进程 {pid}，端口 {port} 已释放。")
                        time.sleep(1) # 稍等 1 秒让操作系统彻底释放句柄
    except subprocess.CalledProcessError:
        # findstr 没找到任何内容时会抛出 CalledProcessError，说明没有进程在监听此端口，为正常现象
        pass
    except Exception as e:
        logger.error(f"⚠️ [端口防护] 清理端口 {port} 残留进程失败: {e}")

# ==============================================================
# 穿透分析 API
# ==============================================================
@app.get("/api/penetration/{fund_code}/{period}")
async def get_penetration(fund_code: str, period: str):
    """
    获取基金穿透分析数据

    Args:
        fund_code: 基金代码，如 160723
        period: 报告期，如 2026Q2
    """
    from services.penetration_service import calculate_penetration
    result = calculate_penetration(fund_code, period)

    if 'error' in result:
        return JSONResponse(status_code=404, content=result)

    return {"status": "ok", "data": result}


@app.get("/api/penetration/funds")
async def list_penetration_funds():
    """获取支持穿透分析的基金列表"""
    from services.penetration_service import load_penetration_rules
    rules = load_penetration_rules()

    funds = []
    for symbol in rules.keys():
        funds.append({
            'symbol': symbol,
            'name': symbol,  # 可从其他表获取名称
            'has_rules': True
        })

    return {'funds': funds}


if __name__ == "__main__":
    # [AI-2026-08-24] 无条件清理端口：无论从哪里启动（start_dashboard.bat/runback.bat/手动），
    # 都确保不会有两个后端实例同时竞争 8000 端口。
    kill_port_owner(8000)
    # [AI-2026-10-07] 监听地址可由环境变量覆盖：默认 0.0.0.0（本机开发机行为不变）；
    # ARM 生产环境在 arb-dashboard.service 里设 ARBDASH_BIND_HOST=127.0.0.1，
    # 只挂回环 lo ⇒ 即使 Oracle 安全组误开，8000 也不会暴露到公网。
    _bind_host = (os.environ.get("ARBDASH_BIND_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    uvicorn.run(app, host=_bind_host, port=8000, log_level="info", access_log=False)

