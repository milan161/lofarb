# -*- coding: utf-8 -*-
# dynamic_valuation.py - 盘中实时动态估值引擎 (工业级 V2.2)

import pandas as pd
import logging
import time
from typing import Dict, Any, Optional
# [AI-2026-07-27] calculate() 直调统一估值核心，不再经 valuation_math 间接
from .unified_valuation import basket_valuation
from .valuation_data_engine import assemble_dynamic_components
from arbcore.analysis.common import EUR_FUND_CODES

logger = logging.getLogger(__name__)

class DynamicValuationCalculator:
    def __init__(self, db_manager):
        self.db = db_manager
        # 缓存 T-1 基准数据，避免盘中高频调用时反复查库卡死 IO
        self._base_data_cache = {}
        self._cache_timestamp = {}
    
    def refresh_cache(self):
        """刷新基准数据缓存"""
        self._base_data_cache.clear()
        self._cache_timestamp.clear()

    def get_base_data(self, fund_code: str) -> Optional[Dict[str, Any]]:
        """获取 T-1 完美基准数据 (自带 10 分钟自动过期机制)"""
        current_time = time.time()
        if fund_code in self._base_data_cache:
            # 10分钟 (600秒) 过期机制
            if current_time - self._cache_timestamp.get(fund_code, 0) < 600:
                return self._base_data_cache[fund_code]
            else:
                # 缓存过期，安全剔除
                del self._base_data_cache[fund_code]


        conn = self.db._get_conn()
        try:
            # [AI-2026-07-23] 根据基金类别选择汇率字段：QDII日本用 jpy_cny_mid，其余用 usd_cny_mid
            cat_df = pd.read_sql(
                "SELECT category FROM unified_fund_list WHERE fund_code = ?",
                conn, params=(fund_code,)
            )
            category_fx = str(cat_df.iloc[0]['category']).strip() if not cat_df.empty else ''
            fx_col = ('eur_cny_mid' if fund_code in EUR_FUND_CODES
                      else 'jpy_cny_mid' if category_fx == 'QDII日本'
                      else 'usd_cny_mid')
            category = category_fx  # 复用

            # ── 第一步：独立取各数据源最新值 ──────────────────────────────
            # [AI-2026-10-12 东哥拍板·撤woody] position 改读 unified_fund_list.pos_ratio
            #   （DB 绝对权威，AGENTS 铁律15；woody 对 QDII日本停更后 pos_ratio 为稳定硬编码值，
            #    东哥明确授权"不依赖woody，静态路径用pos_ratio，可接受硬编码"）。
            #   原读 fund_daily_factors.position（woody写入），撤woody后会冻结成陈旧值，
            #   改读 pos_ratio 后与主面板实时分支(fund_service L1912/L1938)口径完全一致。
            pos_df = pd.read_sql(
                "SELECT pos_ratio FROM unified_fund_list WHERE fund_code = ?",
                conn, params=(fund_code,)
            )
            _pos_ratio = None
            if not pos_df.empty and pd.notna(pos_df.iloc[0]['pos_ratio']):
                _pos_ratio = float(pos_df.iloc[0]['pos_ratio'])

            # hedge/calibration 仍从 fund_daily_factors 取最新（QDII日本撤woody后为None，不兜底）
            factor_df = pd.read_sql(
                "SELECT date, hedge, calibration FROM fund_daily_factors "
                "WHERE fund_code = ? AND hedge IS NOT NULL "
                "ORDER BY date DESC LIMIT 1",
                conn, params=(fund_code,)
            )
            if not factor_df.empty:
                fr = factor_df.iloc[0]
                base_row: Dict[str, Any] = {
                    'position': _pos_ratio,
                    'hedge': fr['hedge'],
                    'calibration': fr['calibration'],
                }
            else:
                base_row = {'position': _pos_ratio, 'hedge': None, 'calibration': None}

            # unified_fund_history：nav/close 每天更新，独立取最新
            # [AI-2026-08-21] 修复：只取 nav IS NOT NULL 的行，避免取到盘中空行导致 calculate() 返回 None
            history_df = pd.read_sql(
                "SELECT date, nav, price FROM unified_fund_history "
                "WHERE fund_code = ? AND nav IS NOT NULL "
                "ORDER BY date DESC LIMIT 1",
                conn, params=(fund_code,)
            )
            if history_df.empty:
                return None
            hr = history_df.iloc[0]
            base_date = str(hr['date'])
            base_row['date'] = base_date
            base_row['nav'] = hr['nav']  # 保证非 NULL
            base_row['close'] = hr['price']  # 可能有收盘价

            # exchange_rate：用 history 的最新日期取
            rate_df = pd.read_sql(
                f"SELECT {fx_col} as exchange_rate FROM exchange_rate WHERE date = ?",
                conn, params=(base_date,)
            )
            if not rate_df.empty and pd.notna(rate_df.iloc[0]['exchange_rate']):
                base_row['exchange_rate'] = rate_df.iloc[0]['exchange_rate']
            else:
                base_row['exchange_rate'] = None

            # ── 第二步：美股 ETF 基准日校验（仅 QDII欧美/黄金原油/商品/混合跨境）
            # [AI-2026-10-11 东哥定调] 分类「白银」已并入「商品」（161226 白银 + 159985 豆粕）。
            #   二者 related_index 均为空 ⇒ 本块对它们本就是 no-op，改动仅为保持白名单与 DB 分类一致。
            us_categories = {'黄金原油', 'QDII欧美', '混合跨境', '商品'}
            if category in us_categories:
                # 先尝试从 fund_basket_weights 获取 ETF 代码，缺失则补充用 related_index
                etf_syms = pd.read_sql(
                    "SELECT DISTINCT underlying_symbol FROM fund_basket_weights WHERE fund_code = ?",
                    conn, params=(fund_code,)
                )
                sym_list = []
                if not etf_syms.empty:
                    sym_list = [s.replace('^', '') for s in etf_syms['underlying_symbol'].tolist() if s]
                if not sym_list:
                    ri_df = pd.read_sql(
                        "SELECT related_index FROM unified_fund_list WHERE fund_code = ?",
                        conn, params=(fund_code,)
                    )
                    if not ri_df.empty:
                        ri = str(ri_df.iloc[0]['related_index'] or '').strip()
                        if ri and ri != '-' and ri != '0' and ri != 'nan':
                            sym_list = [ri]
                # [AI-2026-11-11 东哥拍板「乙·只消噪」] 只消噪，不动估值口径、不改任何估值数值。
                # `znb_*` 是 woody 自造的「现货指数内部代号」（003_1 §127：非期货、非可交易标的，
                # 前缀沿用新浪全球指数行情符号），后缀为标的指数：NKY/TPX/DAX/CAC/SENSEX。
                # 它**不是美股 ETF**，永远不可能出现在 usa_etf_daily_prices 里 ⇒
                # 本块原本每轮必然 etf_count=0 → 打 1 条 ⏭️ INFO → 回溯 SQL 恒空 → base_date 不变。
                # 结论：这块校验对纯 znb_* 篮子是恒等 no-op，只产生噪音日志 + 白跑 2 段 SQL/轮。
                # 按「符号性质」识别并跳过（不是按基金代码加白名单打补丁）：
                #   · 不打 INFO 日志、不跑 COUNT/回溯 SQL；
                #   · base_date 保持原值 ⇒ 与跳过前**结果完全一致**，估值口径零变更。
                # 边界：篮子含真实 ETF（XOP/GLD/USO...）时不触发，原逻辑照常执行。
                if sym_list and all(str(s).startswith('znb_') for s in sym_list):
                    logger.debug(
                        f"  [{fund_code}] 篮子为纯 znb_* 合成指数代号({','.join(map(str, sym_list))})，"
                        f"非美股ETF，跳过美股ETF基准日校验（设计内预期，非数据缺失）"
                    )
                    sym_list = []

                if sym_list:
                    placeholders = ','.join('?' for _ in sym_list)
                    etf_count = conn.execute(
                        f"SELECT COUNT(*) FROM usa_etf_daily_prices "
                        f"WHERE symbol IN ({placeholders}) AND date = ? AND price > 0",
                        (*sym_list, base_date)
                    ).fetchone()[0]
                    if etf_count == 0:
                        # 基准日无ETF数据 → 向前回溯找最近一个有数据的日期
                        logger.info(f"  ⏭️ [{fund_code}] 基准日 {base_date} 无美股ETF数据，向前回溯最近有效日期...")
                        corrected_df = pd.read_sql(f"""
                            SELECT date, nav, price
                            FROM unified_fund_history
                            WHERE fund_code = ? AND (nav IS NOT NULL OR price IS NOT NULL)
                              AND EXISTS (
                                  SELECT 1 FROM usa_etf_daily_prices e
                                  WHERE e.symbol IN ({placeholders}) AND e.date = unified_fund_history.date AND e.price > 0
                              )
                            ORDER BY date DESC LIMIT 1
                        """, conn, params=(fund_code, *sym_list))
                        if not corrected_df.empty:
                            cd = corrected_df.iloc[0]
                            base_date = str(cd['date'])
                            base_row['date'] = base_date
                            base_row['nav'] = cd['nav']
                            base_row['close'] = cd['price']
                            # 重新取该日汇率
                            cr_df = pd.read_sql(
                                f"SELECT {fx_col} as exchange_rate FROM exchange_rate WHERE date = ?",
                                conn, params=(base_date,)
                            )
                            if not cr_df.empty and pd.notna(cr_df.iloc[0]['exchange_rate']):
                                base_row['exchange_rate'] = cr_df.iloc[0]['exchange_rate']
                            logger.info(f"  ✅ [{fund_code}] 回溯后基准日调整为 {base_date}")

            # [AI-2026-07-27] 删除旧的「向前取最近 hedge」填补（原第②级，当年三条路径里最不精确的）：
            # 删除后仅剩两级：① 魔法公式(hedge在) ② 矩阵(篮子)标准公式(hedge缺，自然降级)。
            # 改为：hedge 缺失时不再用陈旧值填补——calculate() 会直接落到
            # 矩阵(篮子)标准公式，仅依赖 usa_etf_daily_prices.netvalue(Yahoo) +
            # exchange_rate(官方中间价) + yaml 权重/仓位，全链路可脱离 Woody 独立计算。

            # [AI-2026-07-21] 补充底层 ETF 基准价格：查询 fund_basket_weights 判断基金会是否为多篮子
            # 有 basket 条目的基金（如161116→GLD+^GLD-EU）必须取 price（市场价格），矩阵公式需要真实价格变化率
            # 无 basket 的单主ETF（如162411→XOP）取 netvalue（净值），因为 hedge 魔术公式不直接使用 base_price
            basket_count = conn.execute(
                "SELECT COUNT(*) FROM fund_basket_weights WHERE fund_code=?",
                (fund_code,)
            ).fetchone()[0]
            _price_col = 'price' if basket_count > 0 else 'netvalue'
            # [AI-2026-08-04] 同时查 price 和 netvalue：
            # base_price 按 _price_col 分流（矩阵用 price，魔法展示用 netvalue）；
            # 但 current_price 退化时应统一取市场价(price)，故额外存 _mkt 后缀供退化使用。
            etf_df = pd.read_sql(
                f"SELECT symbol, {_price_col} as price, price as mkt_price, netvalue as mkt_nav, date "
                "FROM usa_etf_daily_prices WHERE date = ?",
                conn, params=(base_date,)
            )
            if not etf_df.empty:
                for _, r in etf_df.iterrows():
                    sym = r['symbol']
                    base_row[sym] = r['price']
                    # 市场价（price 优先，缺失则 netvalue），供 current_price 退化时统一取用
                    mkt = r['mkt_price'] if pd.notna(r['mkt_price']) and r['mkt_price'] > 0 else r['mkt_nav']
                    base_row[sym + '_mkt'] = mkt
                    if sym.startswith('^'):
                        base_row[sym[1:]] = r['price']
                        base_row[sym[1:] + '_mkt'] = mkt
                else:
                    base_row['^' + sym] = r['price']
                    base_row['^' + sym + '_mkt'] = mkt
            # [AI-2026-07-21 用户要求] 不兜底静默填充，数据缺失就让其缺失，真实暴露

            # [AI-2026-08-16] 篮子以 fund_basket_weights 表为权威（不读 yaml 篮子）。
            # 查最新一日的篮子成分+权重注入 base_row['_basket']，供 calculate 覆盖 yaml portfolio。
            # 与 static_valuation 同逻辑：db 有篮子用 db，无篮子(单ETF/指数/国内LOF)则 fallback yaml。
            bw_df = pd.read_sql(
                "SELECT underlying_symbol, weight FROM fund_basket_weights "
                "WHERE fund_code = ? AND date = (SELECT MAX(date) FROM fund_basket_weights WHERE fund_code = ?)",
                conn, params=(fund_code, fund_code)
            )
            if not bw_df.empty:
                base_row['_basket'] = [
                    {'symbol': r['underlying_symbol'], 'weight': float(r['weight'])}
                    for _, r in bw_df.iterrows()
                    if pd.notna(r['weight']) and float(r['weight']) != 0
                ]

            # position/hedge/calibration 已在顶部独立查询 fund_daily_factors 获取。
            # 若仍为 None：有 basket 的基金（QDII欧美/黄金原油等）缺 position 属异常；
            # 无 basket 的基金（国内LOF/指数等）本就不依赖 position，缺失属预期。
            if base_row.get('position') is None or pd.isna(base_row.get('position')):
                if basket_count > 0:
                    logger.warning(f"  ⚠️ [{fund_code}] fund_daily_factors 无任何有效 position 行，position=None，估值可能失真")
                else:
                    logger.debug(f"  [{fund_code}] 无 basket 且 fund_daily_factors 无 position（国内LOF等预期如此），position=None")

            self._base_data_cache[fund_code] = base_row
            self._cache_timestamp[fund_code] = time.time()
            return base_row
        except Exception as e:
            logger.error(f"获取 {fund_code} 基准数据失败: {e}")
            return None
        finally:
            conn.close()

    def calculate(self, fund_config: Dict, current_fx: float, current_etfs: Dict[str, float]) -> Optional[Dict[str, Any]]:
        """
        实时估值矩阵推演

        [AI-2026-07-27] 重构：直调统一估值核心 basket_valuation（估值引擎 + 数据引擎分离）。
        - 数据装配（组件价格解析、hedge 路由、仓位回溯）全部下沉到 assemble_dynamic_components；
          hedge 是否可用由 lof_config.yaml `valuation_routing` 的 dynamic_hedge 列驱动
          （实时路径取的是交易标的价 SPY/QQQ/XOP，与 woody hedge 计价标的一致，index 类允许魔法）。
        - basket_valuation 内部自动路由：hedge>0 且单组件 → 魔法公式；否则矩阵(篮子)标准公式。
          与旧「先试魔法、失败落矩阵」两段式逻辑数学等价。
        """
        code = str(fund_config.get('code', ''))
        base_data = self.get_base_data(code)
        if not base_data: return None

        # [AI-2026-08-16] 篮子以 DB 为权威：get_base_data 注入 db 篮子则覆盖 yaml portfolio。
        db_basket = base_data.get('_basket')
        if db_basket:
            fund_config = dict(fund_config)
            fund_config['valuation_portfolio'] = db_basket
            fund_config['hedging_portfolio'] = db_basket

        assembled = assemble_dynamic_components(fund_config, base_data, current_etfs)
        if not assembled['ok']:
            return None

        rt_val = basket_valuation(
            assembled['base_nav'],
            assembled['position'],
            assembled['components'],
            assembled['fx_base'],
            current_fx,
            hedge=assembled['hedge'],
        )

        if rt_val:
            return {
                'rt_val': round(rt_val, 4),
                'base_date': base_data['date'],
                'premium': (fund_config.get('current_price', 0) / rt_val - 1) if fund_config.get('current_price', 0) > 0 else None
            }
        return None

    def calculate_detail(self, fund_config: Dict, current_fx: float,
                         current_quotes: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """实时估值明细推演（供 H5 详情页展示计算依据）。

        [AI-2026-08-03] 与 calculate() 数学等价，但额外返回每个组件的盘口/来源/权重/基准价，
        以及仓位、汇率、hedge、基准净值等中间变量，方便用户验证 rt_val 是怎么算出来的。

        current_quotes: {基础代码: {'bid','ask','bid_size','ask_size','price','source',...}}，
                        由 fund_service 通过 market_data_service.get_realtime_quote() 获取。
        返回: {
            'rt_val': float, 'base_date': str, 'premium': float|None,
            'base_nav': float, 'position': float, 'fx_base': float, 'fx_current': float,
            'hedge': float|None,
            'components': [
                {'symbol': str, 'weight': float, 'base_price': float, 'current_price': float,
                 'bid': float, 'ask': float, 'bid_size': float, 'ask_size': float, 'source': str}
            ]
        } 或 None
        """
        code = str(fund_config.get('code', ''))
        base_data = self.get_base_data(code)
        if not base_data:
            return None

        # [AI-2026-08-16] 篮子以 DB 为权威：get_base_data 注入 db 篮子则覆盖 yaml portfolio。
        db_basket = base_data.get('_basket')
        if db_basket:
            fund_config = dict(fund_config)
            fund_config['valuation_portfolio'] = db_basket
            fund_config['hedging_portfolio'] = db_basket

        # 提取实时价（优先 bid）用于估值核心计算
        current_prices = {
            sym: (q.get('bid') or q.get('price') or 0)
            for sym, q in current_quotes.items()
        }
        assembled = assemble_dynamic_components(fund_config, base_data, current_prices)
        if not assembled['ok']:
            return None

        rt_val = basket_valuation(
            assembled['base_nav'],
            assembled['position'],
            assembled['components'],
            assembled['fx_base'],
            current_fx,
            hedge=assembled['hedge'],
        )
        if rt_val is None:
            return None

        # 把完整盘口/来源补进 components
        detailed_components = []
        for c in assembled['components']:
            full_sym = c.get('symbol', '')
            base_sym = full_sym.lstrip('^')
            for suffix in ('-EU', '-JP', '-HK'):
                if base_sym.endswith(suffix):
                    base_sym = base_sym[:-len(suffix)]
                    break
            q = current_quotes.get(base_sym) or {}
            detailed_components.append({
                'symbol': full_sym,
                'weight': round(c.get('weight', 0), 6),
                'base_price': round(c.get('base_price', 0), 4),
                'current_price': round(c.get('current_price', 0), 4),
                'bid': round(q.get('bid', 0), 4) if q.get('bid') else None,
                'ask': round(q.get('ask', 0), 4) if q.get('ask') else None,
                'bid_size': q.get('bid_size') if q.get('bid_size') else None,
                'ask_size': q.get('ask_size') if q.get('ask_size') else None,
                'source': q.get('source', '-'),
            })

        return {
            'rt_val': round(rt_val, 4),
            'base_date': base_data['date'],
            'premium': round((fund_config.get('current_price', 0) / rt_val - 1) * 100, 3)
            if fund_config.get('current_price', 0) > 0 else None,
            'base_nav': round(assembled['base_nav'], 4),
            'position': round(assembled['position'], 6) if assembled['position'] is not None else None,
            'fx_base': round(assembled['fx_base'], 6) if assembled['fx_base'] else None,
            'fx_current': round(current_fx, 6) if current_fx else None,
            'hedge': round(assembled['hedge'], 6) if assembled['hedge'] else None,
            'components': detailed_components,
        }
