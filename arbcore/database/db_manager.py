import os
import sqlite3
import threading
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from .managers.fund_manager import FundManager
from .managers.market_manager import MarketManager
from .managers.system_manager import SystemManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# [AI-2026-08-17] 定位 arb_master.db：优先显式参数 / ARB_MASTER_DB 环境变量；
# 否则从本文件向上遍历祖先目录，找第一个含 database/ 子目录者作为项目根
# （兼容本地 src/arbcore 与 ARM 直接 arbtest/arbcore 两种部署结构），
# 避免硬数 '../..' 层数在 ARM 上算错项目根、连到 /home/ubuntu/database 空库的 bug。
def _resolve_db_path(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get('ARB_MASTER_DB')
    if env:
        return env
    here = os.path.abspath(os.path.dirname(__file__))
    # [AI-2026-10-06] 单一真库 = <ARB根>/_shared/arb_master.db（与 ETFarb core/db.py 一致）。
    # 向上遍历祖先找 <祖先>/_shared/arb_master.db，命中即用（标准 clone 布局：_shared 与各程序同级），
    # 避免向上找 database/ 时命中 LOFarb/database/ 下的空库/旧库
    # （2026-10-06 空库致分类标签全丢的事故根因：搬目录后 _resolve_db_path 命中 LOFarb/database 空库）。
    # ARM 部署无 _shared，此回退不命中，仍走下方 database/ 上溯 → /home/ubuntu/arbtest/database/arb_master.db。
    cur = here
    while True:
        cand = os.path.join(cur, '_shared', 'arb_master.db')
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    cur = here
    fallback = None
    while True:
        candidate = os.path.join(cur, 'database')
        # 数据目录特征：database/ 存在，且不是 arbcore 自身代码目录（含 db_manager.py 即代码目录，跳过）
        if os.path.isdir(candidate) and not os.path.exists(os.path.join(candidate, 'db_manager.py')):
            dbf = os.path.join(candidate, 'arb_master.db')
            # [AI-2026-10-07] 文件级校验：database/ 目录存在 ≠ 其中有主库。
            # 旧写法只判「目录存在」即返回 candidate/arb_master.db，而 LOFarb/database/ 目录确实存在
            # （放 arb_tran.db / 缓存 json），其中却并无 arb_master.db ⇒ 返回一条不存在的路径 ⇒
            # DatabaseManager.__init__ 的 os.makedirs + sqlite3.connect（默认 READWRITE|CREATE）
            # 静默落盘一个 0 字节空库 —— 即 10-06「命中空库致分类标签全丢」的完整触发链。
            # 现在只有库文件确实存在才返回；「目录在而库缺失」者仅记为 fallback（供首次建库）。
            if os.path.exists(dbf):
                return dbf
            if fallback is None:
                fallback = dbf
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    # 全仓未见已存在的主库：命中过 database/ 目录者返回该预期路径（首次建库由调用方创建）；
    # 否则退回原三层上溯逻辑（极不可能走到）。
    if fallback:
        return fallback
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    return os.path.join(base_dir, 'database', 'arb_master.db')


# [AI-2026-10-07] 主库启动自检（防呆）—— 挡住两类事故：
#   ① 主库文件缺失（路径搬迁 / env 未设 / 布局变化）
#   ② 主库文件存在，但其实是空库或错库（**10-06「分类标签全丢」的真实形态**：
#      文件被 makedirs+connect 造出来后「存在」了，任何 os.path.exists 检查都会放行）
# 只读（mode=ro）打开，绝不创建 / 修改任何数据。返回 (ok, problems)，不抛异常，
# 由调用方决定是告警还是拒绝启动。
def check_master_db(db_path=None, required_tables=None):
    import pathlib

    path = os.path.abspath(db_path or _resolve_db_path())
    problems = []
    if not os.path.exists(path):
        return False, [f"主库文件不存在: {path}"]
    if os.path.getsize(path) == 0:
        return False, [f"主库为 0 字节空文件: {path}（sqlite 允许连上，但没有任何表）"]

    # 默认体检项：基金列表 + 篮子权威表（DB 篮子权威，二者为空即视为错库）
    checks = required_tables or (("unified_fund_list", 1), ("fund_basket_weights", 1))
    try:
        # 用 as_uri() 生成跨平台正确 URI；mode=ro 保证文件不存在时直接报错且不落盘
        conn = sqlite3.connect(pathlib.Path(path).as_uri() + "?mode=ro", uri=True, timeout=5.0)
    except Exception as e:
        return False, [f"主库只读打开失败: {type(e).__name__}: {e}（路径: {path}）"]

    try:
        for tbl, min_rows in checks:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                if n < min_rows:
                    problems.append(f"表 {tbl} 行数={n}（应 ≥ {min_rows}）→ 疑似空库/错库")
            except sqlite3.OperationalError as e:
                problems.append(f"表 {tbl} 不可读: {e}")
    finally:
        conn.close()
    return (not problems), problems


class DatabaseManager:
    def __init__(self, db_path=None, include_trading=False):
        if db_path is None:
            # [AI-2026-08-17] 改用 _resolve_db_path：向上找含 database/ 的祖先目录定位项目根，
            #   根治 ARM 部署无 src/ 层导致 '..','..','..' 算错项目根、连到 /home/ubuntu/database 空库的 bug
            db_path = _resolve_db_path()
            
        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        self.db_path = db_path
        self.lock = threading.Lock()
        # [AI-2026-08-16] include_trading: True 时建交易对账表(user_trades/broker_redemption_fees/arbitrage_pairs/fund_fees)，
        # 用于 arb_tran.db（交易独立库）。master 库(False)不建交易表，保证隐私与 share 导出干净。
        self.include_trading = include_trading
        
        # Composition: delegate to specialized managers
        self.funds = FundManager(self.db_path, self.lock)
        self.market = MarketManager(self.db_path, self.lock)
        self.system = SystemManager(self.db_path, self.lock)
        
        self.init_db()
        
    def _get_conn(self):
        # [AI-2026-09-02] 不再每个连接执行 PRAGMA journal_mode=WAL：WAL 是数据库级持久属性，
        # 设一次即永久生效；重复设在模式需变更时抢瞬时排他锁，遇并发写会把调用方（含 asyncio
        # 主线程）死死卡住 —— loop-watchdog 抓到的 60s 全接口超时即由此而来。
        # 改为进程内首次连接时确保一次（见 managers.base.ensure_wal_once）。
        from .managers.base import ensure_wal_once
        ensure_wal_once(self.db_path)
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        return conn
    
    def init_db(self):
        with self.lock:
            conn = self._get_conn()
            # [AI-2026-07-25] 已删除 fund_data 建表与 ALTER（fund_data 表已废弃，零调用方死代码）
            conn.execute('DROP TABLE IF EXISTS futures_data')
            conn.execute('DROP TABLE IF EXISTS future_calibration')
            conn.execute('DROP TABLE IF EXISTS macro_data')
            conn.execute('DROP TABLE IF EXISTS api_sync_status')

            # [AI-2026-07-25] 已删除 system_health 建表（表已废弃，零调用方死代码）
            conn.execute('''CREATE TABLE IF NOT EXISTS exchange_rate (date TEXT PRIMARY KEY, usd_cny_mid REAL, hkd_cny_mid REAL, usd_cnh REAL, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')))''')
            try: conn.execute('ALTER TABLE exchange_rate ADD COLUMN hkd_cny_mid REAL')
            except sqlite3.OperationalError: pass
            try: conn.execute('ALTER TABLE exchange_rate ADD COLUMN usd_cnh REAL')
            except sqlite3.OperationalError: pass
            # [AI-2026-07-09] 新增 jpy_cny_mid 列支持日元汇率（QDII日本估值）
            try: conn.execute('ALTER TABLE exchange_rate ADD COLUMN jpy_cny_mid REAL')
            except sqlite3.OperationalError: pass
            # [AI-2026-10-07] 新增 eur_cny_mid / eur_cny_spot 列支持欧元汇率（QDII欧洲德/法ETF估值）
            try: conn.execute('ALTER TABLE exchange_rate ADD COLUMN eur_cny_mid REAL')
            except sqlite3.OperationalError: pass
            try: conn.execute('ALTER TABLE exchange_rate ADD COLUMN eur_cny_spot REAL')
            except sqlite3.OperationalError: pass

            conn.execute('''CREATE TABLE IF NOT EXISTS usa_etf_daily_prices (date TEXT NOT NULL, symbol TEXT NOT NULL, price REAL, netvalue REAL, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (date, symbol))''')
            try: conn.execute('ALTER TABLE usa_etf_daily_prices ADD COLUMN netvalue REAL')
            except sqlite3.OperationalError: pass

            conn.execute('''CREATE TABLE IF NOT EXISTS futures_daily (date TEXT NOT NULL, symbol TEXT NOT NULL, settle_price REAL, calibration REAL, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (date, symbol))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS fund_basket_weights (date TEXT NOT NULL, fund_code TEXT NOT NULL, underlying_symbol TEXT NOT NULL, weight REAL, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (date, fund_code, underlying_symbol))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS fund_daily_factors (date TEXT NOT NULL, fund_code TEXT NOT NULL, calibration REAL, hedge REAL, position REAL, nav REAL, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (date, fund_code))''')
            try: conn.execute('ALTER TABLE fund_daily_factors ADD COLUMN nav REAL')
            except sqlite3.OperationalError: pass

            conn.execute('''CREATE TABLE IF NOT EXISTS raw_api_data (date TEXT NOT NULL, source TEXT NOT NULL, raw_content TEXT, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (date, source))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS access_sync_status (sync_date TEXT NOT NULL, access_source TEXT NOT NULL, sync_time TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (sync_date, access_source))''')

            conn.execute('CREATE INDEX IF NOT EXISTS idx_fund_code_date ON fund_daily_factors (fund_code, date DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_etf_prices_date ON usa_etf_daily_prices(date DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_fund_basket ON fund_basket_weights(fund_code, date DESC)')

            # [AI-2026-09-03] 基金季报披露持仓（160723 MVP）：支持按报告期存储前十大基金/股票投资明细
            conn.execute('''
                CREATE TABLE IF NOT EXISTS fund_report_holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fund_code TEXT NOT NULL,
                    report_period TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    symbol TEXT,
                    name TEXT NOT NULL,
                    name_en TEXT,
                    region TEXT,
                    currency TEXT,
                    type TEXT,
                    operation_mode TEXT,
                    manager TEXT,
                    weight REAL NOT NULL,
                    market_value REAL,
                    is_stock INTEGER DEFAULT 0,
                    sort_order INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(fund_code, report_period, sort_order, is_stock)
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_report_holdings ON fund_report_holdings(fund_code, report_period, sort_order)')

            conn.execute('''CREATE TABLE IF NOT EXISTS etf_raw_api_data (date TEXT NOT NULL, source TEXT NOT NULL, raw_content TEXT, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (date, source))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS etf_rotation_list (group_id INTEGER, lof_code TEXT, lof_name TEXT, etf_code TEXT, etf_name TEXT, track_index TEXT, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (lof_code, etf_code))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS fund_purchase_status (fund_code TEXT PRIMARY KEY, purchase_status TEXT, redemption_status TEXT, purchase_fee TEXT, redemption_fee TEXT, purchase_limit REAL, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')))''')
            # 兼容旧表：如果 purchase_limit 列不存在则添加
            try:
                conn.execute("ALTER TABLE fund_purchase_status ADD COLUMN purchase_limit REAL")
            except:
                pass
            
            # 数据源配置中心 (VUE 控制台的核心)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS data_source_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    module TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    priority INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    config_json TEXT DEFAULT '{}',
                    updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                    UNIQUE(module, source_name)
                )
            ''')
            
            # 种子数据：初始化实时行情的优先级
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM data_source_config WHERE module = 'realtime_market'")
            if cursor.fetchone()[0] == 0:
                initial_sources = [
                    ('realtime_market', 'tdx', 1, 1, '{"desc": "通达信内存直连"}'),
                    ('realtime_market', 'guojin', 2, 1, '{"desc": "国金QMT (xtquant)"}'),
                    ('realtime_market', 'tencent', 3, 1, '{"desc": "腾讯财经"}'),
                    ('realtime_market', 'sina', 4, 1, '{"desc": "新浪财经轮询"}')
                ]
                conn.executemany('''
                    INSERT INTO data_source_config (module, source_name, priority, is_active, config_json)
                    VALUES (?, ?, ?, ?, ?)
                ''', initial_sources)
                
                # 种子数据：初始化历史数据的优先级
                historical_sources = [
                    ('historical_nav', 'eastmoney', 1, 1, '{"desc": "东方财富净值"}'),
                    ('historical_price', 'sina', 1, 1, '{"desc": "新浪/腾讯价格"}')
                ]
                conn.executemany('''
                    INSERT INTO data_source_config (module, source_name, priority, is_active, config_json)
                    VALUES (?, ?, ?, ?, ?)
                ''', historical_sources)

            # [AI-2026-07-07] 应用级 key-value 设置表
            conn.execute('''CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
            )''')
            # [AI-2026-09-23] 分类级暂停功能(paused_categories)与死字段(skip_qdii_asia_index)已删除（见 docs/013_7）。
            #   不再写入任何种子；存量库的这两行由运维脚本/手动 DELETE 清理。

            conn.execute('''CREATE TABLE IF NOT EXISTS unified_fund_list (category TEXT, fund_code TEXT PRIMARY KEY, fund_name TEXT, related_index TEXT, pos_ratio REAL DEFAULT 0.95, target_type TEXT DEFAULT 'ETF', data_source TEXT DEFAULT '')''')
            # [AI-2026-07-25] 已删除 jsl_fund_list 建表（表已废弃，零调用方死代码）
            # [AI-2026-07-25] 补齐 fund_watchlist：原代码从未建此表，全新环境会 no such table 导致"我的关注"报错。加 IF NOT EXISTS 保护。
            conn.execute('''CREATE TABLE IF NOT EXISTS fund_watchlist (fund_code TEXT PRIMARY KEY, added_at TIMESTAMP DEFAULT (datetime('now','localtime')))''')
            try: conn.execute('ALTER TABLE unified_fund_list ADD COLUMN target_type TEXT DEFAULT \'ETF\'')
            except sqlite3.OperationalError: pass
            # [V11.0] 补齐 idx_code / idx_name 列（兼容旧版分享数据库）
            try: conn.execute("ALTER TABLE unified_fund_list ADD COLUMN idx_code TEXT DEFAULT '-'")
            except sqlite3.OperationalError: pass
            try: conn.execute("ALTER TABLE unified_fund_list ADD COLUMN idx_name TEXT DEFAULT '-'")
            except sqlite3.OperationalError: pass
            # [AI-2026-10-11 东哥定调] 新增 data_source 列：标记「数据源 / 估值办法」——
            #   'woody' = 采用 woody 估值办法、需从 woody API 取 position/hedge 等因子；
            #   'self'  = 用本项目自己的估值算法、不用 WOODY（东哥 2026-11-11 定义，权威 → 000_3 §7.15.7）；
            #             商品期货自算基金（161226 白银 AG0 / 159985 豆粕 M0，step12 硬编码）均归此类。
            #   ''（空）= 未标注的历史遗留；代码只判 'woody'，其余值一律等价「非 woody」。
            #   ⚠️ 旧注释举例的 'm0' / 'silver' 是期货品种标签，放本列属语义错位，11-11 已统一 'self'。
            #   该信息原先只存 lof_config.yaml 的 data_source 字段，step1 抓取范围据此筛选；
            #   DB 权威化后，抓取范围改读本列（YAML 仅退化兜底），与 step5 的 symbol_master 同源手法。
            try: conn.execute("ALTER TABLE unified_fund_list ADD COLUMN data_source TEXT DEFAULT ''")
            except sqlite3.OperationalError: pass

            # JSL Index history and realtime tables
            conn.execute('''CREATE TABLE IF NOT EXISTS index_history (symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL, source TEXT, PRIMARY KEY (symbol, date))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS index_realtime_quotes (symbol TEXT PRIMARY KEY, name TEXT, last_price REAL, prev_close REAL, pct_change REAL, quote_time TEXT, source TEXT, updated_at TEXT)''')

            # Create unified_fund_history if it doesn't exist
            conn.execute('''
                CREATE TABLE IF NOT EXISTS unified_fund_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    fund_code TEXT NOT NULL,
                    price REAL,
                    price_change REAL,
                    nav REAL,
                    nav_date TEXT,
                    volume REAL,
                    trade_volume REAL,
                    shares REAL,
                    shares_added REAL,
                    turnover_rate TEXT,
                    static_val REAL,
                    rt_val REAL,
                    premium REAL,
                    rt_premium REAL,
                    index_close REAL,
                    index_pct REAL,
                    calibration REAL,
                    purchase_status TEXT,
                    redemption_status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, fund_code)
                )
            ''')

            # [V3.8] 新增分时采样表，用于展示深度分析的分时曲线
            conn.execute('''
                CREATE TABLE IF NOT EXISTS fund_intraday_quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fund_code TEXT NOT NULL,
                    date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    price REAL,
                    rt_val REAL,
                    premium REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_intraday_code_date ON fund_intraday_quotes(fund_code, date)')

            if self.include_trading:
                # [V4.6] 新增实盘交易对账表 (强化多账号与快速赎回支持)
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS user_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fund_code TEXT NOT NULL,
                        fund_name TEXT,
                        account_suffix TEXT, -- 账号尾号 (如 1 或 6)
                        action TEXT, -- BUY / SELL / REDEEM
                        volume INTEGER,
                        price REAL,
                        amount REAL,
                        hedge_symbol TEXT,
                        hedge_price REAL,
                        hedge_vol INTEGER,
                        fees REAL DEFAULT 0,
                        trade_date TEXT DEFAULT (date('now', 'localtime')),
                        remind_date TEXT, -- 赎回提醒日
                        status TEXT DEFAULT 'ACTIVE', -- ACTIVE / CLOSED
                        notes TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # [V4.6] 基金费率与佣金设置表
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS fund_fees (
                        fund_code TEXT PRIMARY KEY,
                        redemption_fee_rate REAL DEFAULT 0.5, -- 正常赎回费率 (%)
                        commission_rate REAL DEFAULT 0, -- 券商返佣或折扣 (%)
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # 券商赎回费表
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS broker_redemption_fees (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        category TEXT,
                        fund_code TEXT,
                        broker_name TEXT,
                        fee_rate TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(fund_code, broker_name)
                    )
                ''')

                # [V9.2] 套利对账本（匹配Excel格式：A股买卖+美股空平+盈亏汇总）
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS arbitrage_pairs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fund_code TEXT,
                        fund_name TEXT,
                        -- A股买入
                        buy_date TEXT,
                        buy_price REAL,
                        buy_volume INTEGER,
                        buy_amount REAL,
                        buy_account TEXT,
                        -- A股卖出/赎回
                        sell_date TEXT,
                        sell_price REAL,
                        sell_amount REAL,
                        redemption_fee REAL DEFAULT 0,
                        -- 美股做空
                        hedge_symbol TEXT,
                        short_date TEXT,
                        short_price REAL,
                        short_volume INTEGER,
                        short_amount REAL,
                        -- 美股买平
                        cover_date TEXT,
                        cover_price REAL,
                        cover_amount REAL,
                        us_commission REAL DEFAULT 0,
                        -- 汇总
                        pnl_rmb REAL,
                        pnl_usd REAL,
                        status TEXT DEFAULT 'ACTIVE',
                        buy_notes TEXT,
                        sell_notes TEXT,
                        notes TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

            conn.commit()
            conn.close()

    # Delegate methods
    # [AI-2026-07-25] 已删除 save_fund_data / update_fund_valuation 委托（fund_data 表废弃死代码）
    def upsert_fund_factor(self, *args, **kwargs): return self.funds.upsert_fund_factor(*args, **kwargs)
    def update_fund_pos_ratio(self, *args, **kwargs): return self.funds.update_fund_pos_ratio(*args, **kwargs)
    def upsert_fund_basket_weight(self, *args, **kwargs): return self.funds.upsert_fund_basket_weight(*args, **kwargs)
    def prune_fund_basket_weights(self, *args, **kwargs): return self.funds.prune_fund_basket_weights(*args, **kwargs)
    def get_latest_fund_factor(self, *args, **kwargs): return self.funds.get_latest_fund_factor(*args, **kwargs)
    def get_fund_basket(self, *args, **kwargs): return self.funds.get_fund_basket(*args, **kwargs)
    # [AI-2026-07-25] 已删除 get_latest_fund_price / batch_save_fund_prices / sync_jsl_fund_list / get_jsl_fund_list 委托（表废弃死代码）
    def sync_unified_fund_list(self, *args, **kwargs): return self.funds.sync_unified_fund_list(*args, **kwargs)
    def get_unified_fund_list(self, *args, **kwargs): return self.funds.get_unified_fund_list(*args, **kwargs)
    def batch_save_fund_purchase_status(self, *args, **kwargs): return self.funds.batch_save_fund_purchase_status(*args, **kwargs)
    def get_fund_purchase_status(self, *args, **kwargs): return self.funds.get_fund_purchase_status(*args, **kwargs)
    def save_unified_history(self, *args, **kwargs): return self.funds.save_unified_history(*args, **kwargs)

    def upsert_exchange_rate(self, *args, **kwargs): return self.market.upsert_exchange_rate(*args, **kwargs)
    def upsert_hkd_exchange_rate(self, *args, **kwargs): return self.market.upsert_hkd_exchange_rate(*args, **kwargs)
    def upsert_futures_daily(self, *args, **kwargs): return self.market.upsert_futures_daily(*args, **kwargs)
    def upsert_usa_etf_price(self, *args, **kwargs): return self.market.upsert_usa_etf_price(*args, **kwargs)
    def upsert_usa_etf_netvalue(self, *args, **kwargs): return self.market.upsert_usa_etf_netvalue(*args, **kwargs)
    def upsert_index_history(self, *args, **kwargs): return self.market.upsert_index_history(*args, **kwargs)
    # [AI-2026-11-11] step5 批量落库（单事务，替代逐行 upsert）
    def bulk_upsert_usa_etf_prices(self, *args, **kwargs): return self.market.bulk_upsert_usa_etf_prices(*args, **kwargs)
    def bulk_upsert_index_history(self, *args, **kwargs): return self.market.bulk_upsert_index_history(*args, **kwargs)

    def get_latest_usa_etf_date(self, *args, **kwargs): return self.market.get_latest_usa_etf_date(*args, **kwargs)
    def get_latest_futures_price(self, *args, **kwargs): return self.market.get_latest_futures_price(*args, **kwargs)
    def batch_save_futures_data(self, *args, **kwargs): return self.market.batch_save_futures_data(*args, **kwargs)

    def save_raw_api_data(self, *args, **kwargs): return self.system.save_raw_api_data(*args, **kwargs)
    def get_raw_api_data(self, *args, **kwargs): return self.system.get_raw_api_data(*args, **kwargs)
    def mark_access_synced(self, *args, **kwargs): return self.system.mark_access_synced(*args, **kwargs)
    def is_access_synced_today(self, *args, **kwargs): return self.system.is_access_synced_today(*args, **kwargs)
    def remove_access_sync_status(self, *args, **kwargs): return self.system.remove_access_sync_status(*args, **kwargs)
    def save_health_status(self, *args, **kwargs): return self.system.save_health_status(*args, **kwargs)
    # [AI-2026-07-25] 已删除 get_health_status 委托（方案C：健康检查功能无调用方，死代码）
    def cleanup_old_data(self, *args, **kwargs): return self.system.cleanup_old_data(*args, **kwargs)
    def drop_deprecated_tables(self, *args, **kwargs): return self.system.drop_deprecated_tables(*args, **kwargs)
    def vacuum_database(self, *args, **kwargs): return self.system.vacuum_database(*args, **kwargs)
    def get_data_source_config(self, *args, **kwargs): return self.system.get_data_source_config(*args, **kwargs)
    def update_data_source_config(self, *args, **kwargs): return self.system.update_data_source_config(*args, **kwargs)

    # [AI-2026-07-07] app_settings key-value 读写
    def get_app_setting(self, key: str, default: str = '0') -> str:
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            conn.close()
            return row[0] if row else default
        except Exception:
            return default

    def set_app_setting(self, key: str, value: str):
        try:
            conn = self._get_conn()
            conn.execute("""INSERT INTO app_settings (key, value, updated_at)
                            VALUES (?, ?, datetime('now', 'localtime'))
                            ON CONFLICT(key) DO UPDATE SET value=?, updated_at=datetime('now', 'localtime')""",
                         (key, value, value))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to set app_settings[{key}]: {e}")

    # Compatibility methods
    def mark_api_synced(self, *args, **kwargs): return self.mark_access_synced(*args, **kwargs)
    def is_api_synced_today(self, *args, **kwargs): return self.is_access_synced_today(*args, **kwargs)

    def sync_etf_rotation_list(self, df):
        with self.lock:
            try:
                conn = self._get_conn()
                conn.execute('DROP TABLE IF EXISTS etf_rotation_list')
                conn.execute('''CREATE TABLE etf_rotation_list (group_id INTEGER, lof_code TEXT, lof_name TEXT, etf_code TEXT, etf_name TEXT, track_index TEXT, updated_at TIMESTAMP DEFAULT (datetime('now', 'localtime')), PRIMARY KEY (lof_code, etf_code))''')
                for _, row in df.iterrows():
                    conn.execute('INSERT INTO etf_rotation_list (group_id, lof_code, lof_name, etf_code, etf_name, track_index) VALUES (?, ?, ?, ?, ?, ?)', (int(row['组别']), str(row['LOF基金代码']).split('.')[0].zfill(6), str(row['LOF基金名称']), str(row['ETF基金代码']).split('.')[0].zfill(6), str(row['ETF基金名称']), str(row['跟踪指数'])))
                conn.commit()
                logger.info(f"Successfully synced {len(df)} rotation config items.")
            except Exception as e: logger.error(f"Failed to sync rotation config: {e}")
            finally: conn.close()
