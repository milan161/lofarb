import asyncio
import logging
from datetime import datetime
import pandas as pd

logger = logging.getLogger(__name__)

def _scalar_level(v):
    """[AI-2026-08-24] A股实时源(tdx/sina/guojin/galaxy/tencent)的bid/ask为5档list，
    IB/FUTU分支为标量买一/卖一。统一取首档（list[0]）或原值，避免list>int崩溃。"""
    if isinstance(v, (list, tuple)) and v:
        return float(v[0]) if v[0] is not None else 0
    return v

# [配套改进 2026-09-26] 中国法定节假日（A股休市日）排除，根治"把休市日误判为采样断档"。
# 来源：国务院办公厅《关于2026年部分节假日安排的通知》(2025-11-04)。
# 每年需更新此表；补班日(调休上班的周末)单独列出，优先于周末判定。
_CN_HOLIDAYS_2026 = {
    # 元旦 1/1-1/3
    '2026-01-01','2026-01-02','2026-01-03',
    # 春节 2/15-2/23（腊月廿八至正月初七）
    '2026-02-15','2026-02-16','2026-02-17','2026-02-18','2026-02-19','2026-02-20','2026-02-21','2026-02-22','2026-02-23',
    # 清明 4/4-4/6
    '2026-04-04','2026-04-05','2026-04-06',
    # 劳动节 5/1-5/5
    '2026-05-01','2026-05-02','2026-05-03','2026-05-04','2026-05-05',
    # 端午 6/19-6/21
    '2026-06-19','2026-06-20','2026-06-21',
    # 中秋 9/25-9/27（不调休）
    '2026-09-25','2026-09-26','2026-09-27',
    # 国庆 10/1-10/7
    '2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-07',
}
# 调休补班日：本为周末，但法定上班（算交易日）。来源同上。
_CN_MAKEUP_WORKDAYS_2026 = {
    '2026-01-04','2026-02-14','2026-02-28','2026-05-09','2026-09-20','2026-10-10',
}

def is_trading_day(d):
    """A股交易日判定：周一~周五，排除法定假日；补班日(调休上班的周末)算交易日。"""
    if not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day) if hasattr(d, 'date') else d
    ds = d.strftime('%Y-%m-%d')
    if ds in _CN_MAKEUP_WORKDAYS_2026:
        return True
    if ds in _CN_HOLIDAYS_2026:
        return False
    return d.weekday() < 5

class IntradaySamplerService:
    """
    分时数据采样服务 (每分钟执行一次)
    负责在交易时段采集实时价格、实时估值、实时溢价率并存入数据库。
    """
    def __init__(self, db_manager, market_data_service, config_service):
        # [AI-2026-10-15] 延迟 import 打破独立进程导入时的循环依赖
        # （sampler_service → dynamic_valuation → valuation_data_engine/unified_valuation
        #  → ... → 间接回到 sampler_service）。移至 __init__ 内，运行时所有模块已加载，
        # 不再循环；且未来即便在 main.py 重新实例化 sampler 也更稳健。
        from arbcore.calculators.dynamic_valuation import DynamicValuationCalculator
        self.db = db_manager
        self.market_data = market_data_service
        self.config_service = config_service
        self.calculator = DynamicValuationCalculator(db_manager)
        self.running = False
        self._task = None
        self.active_watchlist = []
        # [AI-2026-10-15] 采样目标基金（与东哥指定常用基金一致）；
        # 同时作为启动后订阅实时源的符号集合（独立守护进程不会自动订阅 → 缓存空 → 取价 None）。
        self.target_codes = {'162411', '164701', '161116'}
        self.lof_prices = {}   # [2026-08-21] LOF买卖一价缓存
        self.etf_prices = {}   # [2026-08-21] ETF买卖一价缓存

    def _load_db_basket(self, fund_code: str):
        """[AI-2026-09-22 第2步断源] 读 fund_basket_weights 最新日期篮子（yaml 同构 list[dict]）。

        采样取价清单与计算口径统一以 DB 为唯一权威，不再读 yaml 的 valuation_portfolio /
        hedging_portfolio 旧口径（旧口径与 DB 权威不一致 → 会订到 DB 里根本不存在的标的，
        把富途订阅额度撑爆）。无行返回空 list —— 不兜底 yaml：缺失即缺失，表现为
        「该基金本轮无标的可取价」，而不是静默回落到另一套口径。
        """
        try:
            conn = self.db._get_conn()
            rows = conn.execute(
                "SELECT underlying_symbol, weight FROM fund_basket_weights "
                "WHERE fund_code = ? "
                "AND date = (SELECT MAX(date) FROM fund_basket_weights WHERE fund_code = ?) "
                "ORDER BY weight DESC",
                (fund_code, fund_code)
            ).fetchall()
            conn.close()
            return [{'symbol': r[0], 'weight': r[1]} for r in rows if r[0]]
        except Exception as e:
            logger.warning(f"采样读取DB篮子失败 {fund_code}: {e}")
            return []

    async def start(self):
        if self.running: return

        # [AI-2026-08-18] 启用分时采样服务：
        # - 只采样重点基金（target_codes = {'162411'}），DB 压力可控
        # - 汇率改用 DB 美元中间价（exchange_rate.usd_cny_mid），零网络请求
        # - 实时价格从内存缓存读取，零网络请求
        # - 每60秒采样一次，仅 A股交易时段（9:30-15:00）
        enable_sampler = True

        if not enable_sampler:
            logger.info("ℹ️ 分时采样服务已根据配置禁用 (enable_intraday_sampler 默认为 False)")
            return
            
        self.running = True

        # [AI-2026-10-15] 订阅目标基金到实时源（腾讯/新浪缓存依赖订阅才能取到价）。
        # 独立守护进程里 realtime_manager 不会自动订阅这些代码 → 缓存为空 →
        # get_realtime_quote 返回 None → LOF 价格线 0 行。这里在启动后显式订阅，
        # 与 arb-dashboard 行为一致（看板会订阅其配置基金）。ETF 篮子标的(GLD/XOP)由
        # 富途自动连接守护按白名单订阅，本处只订阅 LOF 主代码。
        try:
            rm = getattr(self.market_data, 'realtime_manager', None)
            if rm is not None:
                rm.subscribe(list(self.target_codes))
                logger.info(f"📡 分时采样已订阅实时源符号: {sorted(self.target_codes)}")
        except Exception as e:
            logger.warning(f"⚠️ 订阅实时源符号失败(非致命): {e}")

        self._task = asyncio.create_task(self._sampling_loop())
        logger.info("分时采样服务已启动")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
        logger.info("⏹️ 分时采样服务已停止")

    def is_market_open(self):
        """判断是否为 A 股交易时间 (9:30-11:30, 13:00-15:00)，并排除法定节假日"""
        now = datetime.now()
        # 排除周末与法定节假日（补班日已在内判定为交易日）
        if not is_trading_day(now):
            return False

        current_time = now.strftime('%H:%M')
        if '09:30' <= current_time <= '11:30' or '13:00' <= current_time <= '15:00':
            return True
        return False

    async def _sampling_loop(self):
        while self.running:
            # [AI-2026-09-22] 轮次对齐自然分钟：slot 为本轮起算时刻，落库时间戳由它决定。
            slot = datetime.now()
            try:
                if self.is_market_open():
                    # [修复] 同步网络/DB 调用不应跑在事件循环上，整体丢线程池避免 head-of-line 阻塞
                    await asyncio.to_thread(self._perform_sample_sync, slot)
            except Exception as e:
                import traceback
                logger.error(f"🚨 采样循环异常: {e}")
                logger.error(traceback.format_exc())

            # [AI-2026-09-22] 原实现是「跑完再 sleep 60s」⇒ 周期 = 60s + 本轮耗时，
            # 上游一慢（富途额度满/重连、DB 写锁竞争）周期就变成 2~8 分钟且**永久累积漂移**，
            # 分时序列出现大面积分钟空档。现改为「睡到本轮起算时刻的下一个整分」：
            # 慢轮次只吃掉自己那几格，恢复正常后立刻回到逐分钟，不再一路漂下去。
            elapsed = (datetime.now() - slot).total_seconds()
            await asyncio.sleep(max(1.0, 60.0 - (elapsed % 60.0)))

    def _perform_sample_sync(self, slot=None):
        try:
            # 加载所有的配置基金
            all_config_funds = []
            try:
                cfg = self.config_service.get_full_config() or {}
                all_config_funds = cfg.get('funds', []) or []
            except Exception as e:
                logger.error(f"采样服务读取配置基金失败: {e}")

            # [AI-2026-08-20] 东哥指定常用基金：162411（华宝油气）+ 164701（汇添富贵金属）+ 161116（易方达黄金）
            # 最多5只，每只每天约240条（4小时×60分钟），5只 = 1200条/天，10天 = 12000条 ≈ 2MB
            # [AI-2026-10-15] 改用 self.target_codes（唯一来源，避免与 __init__ 重复定义漂移）
            target_codes = self.target_codes
            
            funds_to_sample = []
            for f in all_config_funds:
                if not isinstance(f, dict):
                    continue
                code = str(f.get('code', '')).strip()
                if code in target_codes:
                    funds_to_sample.append(f)
            
            logger.info(f"📊 采样服务临时限定处理测试基金: {len(funds_to_sample)} 只")
            if not funds_to_sample:
                return
            
            # [AI-2026-08-18] 改用美元中间价汇率（从 DB 读，不新增网络请求）
            # 东哥要求：LOF 基金只使用美元中间价，不需要新浪在岸价
            current_fx = None
            try:
                conn = self.db._get_conn()
                # [配套改进 2026-09-26] 跳过法定假日 NULL 行（如中秋 9/25 央行不发布中间价），
                # 回退到最近有效汇率，避免假日采样因 FX 缺失而 0 行 / 误触发守卫。
                row = conn.execute(
                    "SELECT usd_cny_mid FROM exchange_rate "
                    "WHERE usd_cny_mid IS NOT NULL ORDER BY date DESC LIMIT 1"
                ).fetchone()
                conn.close()
                if row and row[0]:
                    current_fx = float(row[0])
                    logger.info(f"📊 采样服务使用美元中间价汇率: {current_fx}")
            except Exception as e:
                logger.warning(f"⚠️ 获取美元中间价汇率失败: {e}")

            # [A-守卫] FX 缺失守卫：usd_cny_mid 为 NULL/表空 → 估值必失败、本轮大概率 0 行
            if current_fx is None:
                logger.warning("⚠️ [采样守卫] exchange_rate.usd_cny_mid 缺失或为空，实时估值将失败，本轮可能 0 行写入")

            # [修复] 构建完整符号的实时价格字典（如 ^INDA-EU → 35.5）
            current_etfs = {}
            
            # 第一步：收集所有待采样基金对应的美股ETF（用于实时估值计算）
            us_etf_symbols = set()
            for f in funds_to_sample:
                if f is None:
                    continue
                # [AI-2026-09-22 第2步断源] 取价清单改读 fund_basket_weights 最新日期（DB 唯一权威），
                # 不再读 yaml 的 valuation_portfolio / hedging_portfolio 旧口径。采样取价与计算口径
                # 由此统一（计算侧 DynamicValuationCalculator 本就用 DB _basket 覆盖 yaml portfolio）。
                _fcode = str(f.get('code', '')).strip()
                portfolio = self._load_db_basket(_fcode)
                
                for item in portfolio:
                    if item is None:
                        continue
                    symbol = item.get('symbol', '')  # 完整符号（如 ^INDA-EU）
                    if not symbol:
                        continue
                    
                    # 过滤掉A股ETF代码（6位纯数字或带SZ/SH前缀）
                    # [AI-2026-10-08 方案B] 采样取价集合只放真实可订阅的裸 ETF 符号。
                    # '^GLD-EU' 这类 ^ 区域快照符号（Woody 合成，非交易所真实标的）不进采样清单，
                    # 折叠成裸符号再入集合（'^GLD-EU'→'GLD'），避免对不存在的合成符号走实时盘口取价
                    # （此前 ^GLD-EU 与 GLD 各采一次、价格完全相同，纯属冗余且语义错误）。
                    clean_symbol = symbol.lstrip('^')
                    base_symbol = clean_symbol
                    for suffix in ['-EU', '-JP', '-HK']:
                        if base_symbol.endswith(suffix):
                            base_symbol = base_symbol[:-len(suffix)]
                            break

                    if clean_symbol.isdigit() and len(clean_symbol) == 6:
                        continue
                    if symbol.upper().startswith(('SZ', 'SH')):
                        continue

                    # [核心安全阀解除] 白名单限制已废除
                    # 因为混合基金的重负载美股(TSMC, NVDA等)已经硬路由至富途分流
                    # 剩下的核心套利ETF(GLD, USO, XOP等)不到20只，盈透(IB)完全可以全量接管夜盘流式订阅

                    # 添加到待采集集合（裸符号，去重）
                    us_etf_symbols.add(base_symbol)
            
            logger.info(f"📈 采样服务需要采集的美股ETF: {len(us_etf_symbols)} 只 ({', '.join(list(us_etf_symbols)[:5])})")
            
            # 第二步：采集所有美股ETF的实时价格（9:30-15:00）
            for symbol in us_etf_symbols:
                q = self.market_data.get_realtime_quote(symbol)
                if q and q.get('price'):
                    current_etfs[symbol] = q['price']
                    # [2026-08-24] 补订 ORDER_BOOK 获取真实买卖一价
                    futu_code = symbol.lstrip('^')
                    if hasattr(self.market_data, '_fetch_order_book'):
                        ob_bid, ob_ask, _, _, _, _ = self.market_data._fetch_order_book(futu_code)
                        if ob_bid and ob_bid > 0 and ob_ask and ob_ask > 0:
                            q['bid'] = ob_bid
                            q['ask'] = ob_ask
                    # [2026-08-21] 保存 ETF 买卖一价
                    if 'etf_prices' not in dir(self):
                        self.etf_prices = {}
                    self.etf_prices[symbol] = {
                        'bid': q.get('bid', 0),
                        'ask': q.get('ask', 0),
                        'price': q['price']
                    }
                    logger.info(f"📈 采样ETF: {symbol} price={q['price']}, bid={q.get('bid')}, ask={q.get('ask')}")

            # [A-守卫] ETF 取价守卫：待采美股ETF全部取价失败 → 行情源/OpenD链路断
            _etf_ok = sum(1 for s in us_etf_symbols if s in current_etfs)
            if us_etf_symbols and _etf_ok == 0:
                logger.warning(f"⚠️ [采样守卫] {len(us_etf_symbols)} 只美股ETF全部取价失败(行情源/OpenD链路断?)，本轮估值必缺")

            # 第三步：采集自选LOF基金的实时价格
            for f in funds_to_sample:
                if f is None:  # [修复] 跳过None元素
                    continue
                
                # 获取LOF基金实时价格
                code = str(f.get('code', ''))
                if not code or not code.isdigit():
                    continue
                
                if code.isdigit() and len(code) in [5, 6]:
                    q = self.market_data.get_realtime_quote(code)
                    if q and q.get('price'):
                        current_etfs[code] = q['price']
                        # [2026-08-21] 保存 LOF 买卖一价
                        if 'lof_prices' not in dir(self):
                            self.lof_prices = {}
                        self.lof_prices[code] = {
                            'bid': q.get('bid', 0),
                            'ask': q.get('ask', 0),
                            'price': q['price']
                        }
            
            # 执行采样
            # [AI-2026-09-22] 时间戳改用「本轮起算时刻」(slot) 而非落库时刻 datetime.now()。
            # 取价段若被上游阻塞数分钟，用落库时刻会把 14:44 采到的数据标成 15:11
            # （既越过收盘、又在序列里拉出假空档）。分钟位必须由轮次起点决定。
            now = slot or datetime.now()
            date_str = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H:%M')
            conn = self.db._get_conn()
            try:
                cursor = conn.cursor()
                written = 0
                for fund in funds_to_sample:
                    if fund is None:  # [修复] 跳过None元素
                        continue
                    code = fund['code']
                    # [2026-08-24] 修复：LOF价格从lof_prices获取，不是current_etfs
                    price = self.lof_prices.get(code, {}).get('price', 0)
                    if price <= 0:
                        continue
                    
                    # 计算实时估值（传入完整符号格式的current_etfs）
                    res = self.calculator.calculate(fund, current_fx, current_etfs)
                    if res and res.get('rt_val') and res['rt_val'] > 0:
                        rt_val = res['rt_val']
                        premium = (price / rt_val - 1) * 100

                        # [2026-08-21] 计算真实开仓/平仓溢价率
                        # open_premium = (LOF_ask1 / backendRtValSafe - 1) * 100  # 买LOF吃卖一
                        # close_premium = (LOF_bid1 / backendRtValPeg - 1) * 100   # 卖LOF吃买一
                        lof_ask = _scalar_level(self.lof_prices.get(code, {}).get('ask', 0))
                        lof_bid = _scalar_level(self.lof_prices.get(code, {}).get('bid', 0))

                        # backendRtValSafe: 用ETF买一价（bid）计算
                        #   开仓时卖空ETF吃买一（低价），成本保守→估值偏低→溢价偏高
                        # backendRtValPeg: 用ETF卖一价（ask）计算
                        #   平仓时买平ETF吃卖一（高价），成本激进→估值偏高→溢价偏低
                        # [AI-2026-09-22 第2步断源] 主标的改取 DB 篮子权重最大者（与计算口径一致）
                        portfolio = self._load_db_basket(code)
                        etf_symbol = portfolio[0].get('symbol', '') if portfolio else ''
                        etf_bid = _scalar_level(self.etf_prices.get(etf_symbol, {}).get('bid', 0)) if etf_symbol else 0
                        etf_ask = _scalar_level(self.etf_prices.get(etf_symbol, {}).get('ask', 0)) if etf_symbol else 0

                        # 构建safe/peg两种估值所需的ETF价格字典
                        etfs_safe = dict(current_etfs)  # 默认用last价
                        etfs_peg = dict(current_etfs)
                        if etf_symbol:
                            # safe估值：用ETF bid价（开仓卖空ETF吃买一，成本保守）
                            if etf_bid > 0:
                                etfs_safe[etf_symbol] = etf_bid
                            # peg估值：用ETF ask价（平仓买平ETF吃卖一，成本激进）
                            if etf_ask > 0:
                                etfs_peg[etf_symbol] = etf_ask

                        # 计算backendRtValSafe和backendRtValPeg
                        res_safe = self.calculator.calculate(fund, current_fx, etfs_safe)
                        res_peg = self.calculator.calculate(fund, current_fx, etfs_peg)
                        backend_rt_val_safe = res_safe.get('rt_val', 0) if res_safe else 0
                        backend_rt_val_peg = res_peg.get('rt_val', 0) if res_peg else 0

                        open_premium = None
                        close_premium = None
                        if lof_ask > 0 and backend_rt_val_safe > 0:
                            open_premium = (lof_ask / backend_rt_val_safe - 1) * 100
                        if lof_bid > 0 and backend_rt_val_peg > 0:
                            close_premium = (lof_bid / backend_rt_val_peg - 1) * 100

                        cursor.execute("""
                            INSERT INTO fund_intraday_quotes
                            (fund_code, date, time, price, rt_val, premium, open_premium, close_premium,
                             lof_bid1, lof_ask1, etf_bid1, etf_ask1)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (code, date_str, time_str, price, rt_val, premium,
                              open_premium, close_premium,
                              lof_bid, lof_ask, etf_bid, etf_ask))
                        written += 1
                conn.commit()
                # [A-守卫] 整轮写库守卫：有基金要采却 0 行 → 取价/估值全断，盲窗！
                if len(funds_to_sample) > 0 and written == 0:
                    logger.warning(f"⚠️ [采样守卫] 本轮意图采样 {len(funds_to_sample)} 只基金但 0 行写入，疑似行情源/FX/估值链路静默失败")
            except Exception as e:
                logger.error(f"❌ 采样写入数据库失败: {e}")
                import traceback
                logger.error(traceback.format_exc())
            finally:
                conn.close()
                
        except Exception as e:
            import traceback
            logger.error(f"❌ _perform_sample 异常: {e}")
            logger.error(traceback.format_exc())
            raise
