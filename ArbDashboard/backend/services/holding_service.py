"""
基金季报持仓分析服务（160723 MVP）。

提供：
- 报告期列表
- 某报告期持仓明细、地区分布、与上期变动
- 季报持仓法实时估值（报告期净值 × Σ权重 × 标的涨跌幅）

数据依赖：
- fund_report_holdings：季报解析后的持仓
- unified_fund_history：报告日基金净值
- usa_etf_daily_prices：底层标的报告日收盘价
- market_data_service.get_realtime_quote：底层标的实时行情
"""
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional


# ---------------------------------------------------------------------------
# 季报持仓法静态估值（Model A）配置
# ---------------------------------------------------------------------------
# 权重唯一权威 = DB fund_report_holdings.weight（小数存储，×100 才是百分比）。
# 这里只保留"代码取代不了 DB"的两项元数据：

# 1) 季报持仓 symbol -> 估值篮子 symbol 的等价合并。
#    同一底层资产在不同币种/交易所发行的份额（WisdomTree Brent ETC 的
#    USD(BRNT)/GBP(BRNG)/EUR(BNQA) 份额）净值走势一致、差异仅为汇率，
#    统一用主份额 BRNT 的 USD 价格序列代表，避免"有权重却没有独立价格源"。
SYMBOL_ALIAS: Dict[str, str] = {
    "BRNG": "BRNT",
    "BNQA": "BRNT",
}

# 2) 单个标的可用性：该标的在报告期适用区间内的价格点数 / 该区间净值天数。
#    低于此值说明它只有报告日当天的快照（如 1671 只有 2 个点），
#    硬算会退化成"前填一个僵死价格"参与加权，必须从篮子剔除。
MIN_SYMBOL_DAYS_RATIO = 0.70

# 3) 覆盖率门控：篮子可算权重 / 该报告期全部基金持仓权重。
#    低于此值说明该期缺失标的过多，复算结果不可信，仅返回不落库。
MIN_COVERAGE = 0.90

# 汇率口径：按币种取对应 cny_mid（USD->usd_cny_mid / JPY->jpy_cny_mid / HKD->hkd_cny_mid）。
# 逐标的 (1+本地涨跌)×(1+该币种汇率涨跌)-1 加权，才是 NAV(CNY) 的真实构成。
# 纯 USD 篮子（160723/161129 五只）与旧"全 usd_only"数值完全一致；
# 含 JPY/HKD 篮子（501018 的 1699/1671、161129 的 03175）必须用对应汇率，否则误差系统性偏大。
FX_MODE = "by_currency"

# 币种 -> exchange_rate 列名（GBP 暂无列，缺则退化为 USD 并告警）
CURRENCY_FX_COL: Dict[str, str] = {
    "USD": "usd_cny_mid",
    "JPY": "jpy_cny_mid",
    "HKD": "hkd_cny_mid",
    "EUR": "eur_cny_mid",
}

# 4) 前填标注：区分"真实休市前填 ✅"与"非假期数据缺失前填 ⚠️"。
#    东哥 2026-09-12 立规：前填只可用于真实交易假期（美/港/日/欧/英假期不同），
#    绝不可以把"数据源没爬到行情"偷偷当前填掩盖；缺价必须明显标注供人工核实。
#    标的 -> 上市市场（用于查该市场当日是否真休市）。BRNG/BNQA 已 alias 成 BRNT(UK)。
SYMBOL_MARKET: Dict[str, str] = {
    # 美股（NYSE/Nasdaq）
    "OILK": "US", "BNO": "US", "USO": "US", "DBO": "US",
    "XLE": "US", "XOP": "US", "OILUSA": "CH",
    # 伦敦（LSE）ETC/ETF
    "CRUD": "UK", "BRNT": "UK",
    # 港股
    "03175": "HK",
    # 日股
    "1699": "JP", "1671": "JP",
}

# 各市场 2026 休市日（只列确定的主要假期；宁可少列——漏列会触发"⚠️非假期缺价"
# 提示交由人工核实，绝不掩盖成正常前填）。
MARKET_HOLIDAYS: Dict[str, set] = {
    "US": {
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
    },
    "UK": {
        "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04",
        "2026-05-25", "2026-08-31", "2026-12-25", "2026-12-28",
    },
    "HK": {
        "2026-01-01", "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-04-03", "2026-04-06", "2026-04-07", "2026-05-01",
        # [AI-2026-10-08] 佛诞（农历四月初八 = 2026-05-24）适逢周日 ⇒ 翌日 5-25 补假。
        # 原表漏列，03175（上市 HK）在该日无价被判成"⚠️非假期缺价"（假报警）。
        "2026-05-25",
        "2026-06-19", "2026-07-01", "2026-09-25", "2026-10-01",
        "2026-10-26", "2026-12-25", "2026-12-26", "2026-12-28",
    },
    "JP": {
        "2026-01-01", "2026-01-12", "2026-02-11", "2026-03-20",
        "2026-04-29", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22", "2026-09-23",
        "2026-10-12", "2026-11-03", "2026-11-23",
    },
    "CH": {
        "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01",
        "2026-08-01", "2026-12-25", "2026-12-26",
    },
}

_MISSING_MARKET_DEFAULT = "US"  # 未知标的默认按美股判断（美股休市最多，避免误判为"非假期"漏标）


def _market_of(symbol: str) -> str:
    return SYMBOL_MARKET.get(symbol, _MISSING_MARKET_DEFAULT)


def is_market_holiday(symbol: str, dt: str) -> bool:
    """该标的对应市场在某日是否真实休市（用于区分合法前填与数据漏抓）。
    周末（周六/周日）所有市场均不开市，统一视为非交易日——缺价属合法前填，
    绝不标红成\"⚠️非假期缺价\"（避免把周末误判成数据缺失）。"""
    try:
        wd = __import__("datetime").date.fromisoformat(dt).weekday()  # 0=Mon..6=Sun
    except Exception:
        wd = -1
    if wd >= 5:  # 周六/周日
        return True
    return dt in MARKET_HOLIDAYS.get(_market_of(symbol), set())


def _last_trading_day(symbol: str, on_or_before: str, max_back: int = 20) -> Optional[str]:
    """该标的所属市场在 on_or_before 当日或之前的最后一个交易日（跳过周末 + 该市场假期）。

    JP/CH 等无自动源标的的"是否落后"基准必须用它 —— 各国假期不同，
    拿美股参考日当基准会把"日股放假"误判成"缺价"（09-21~09-23 为日本假期，实测踩到）。
    """
    from datetime import date as _date
    from datetime import timedelta as _td
    try:
        d = _date.fromisoformat(on_or_before)
    except Exception:
        return None
    for _ in range(max_back):
        s = d.isoformat()
        if not is_market_holiday(symbol, s):
            return s
        d -= _td(days=1)
    return None


# ---------------------------------------------------------------------------
# 同步兜底补抓（东哥 2026-09-15 提议，配合 sync_usa_etf_from_arm）
# 背景：ARM sampler 每日 06:00 抓取，新浪美股日K部分标的更新延迟（9-14 实证：
# 多数标的当时停在 9-11，北京时间 19:39 才补齐），ARM 缺 → 同步后本地同缺。
# 兜底：同步完成后，对本地缺"最新已收盘交易日"收盘价的标的，本地直连源补抓
# 一次；仍缺则在返回里列出供前端报警。ARM 缺 + 本地也缺的概率大幅降低。
# 源与 sampler 一致：美股→新浪 US_MinKService；伦敦→腾讯 ukXXX；港股→腾讯 hkXXXX；
# 日股(JP)/瑞股(CH) [AI-2026-09-26] 已由 ARM sampler 自动抓取（SIX 官方 CSV / 雅虎日本），
# 本机重抓兜底直接复用 sampler 抓取函数（见 _fetch_jpch_daily_closes），013_2 §9.1 旧手喂口径废止。

_SINA_US_DAILY_URL = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_/"
                      "US_MinKService.getDailyK?symbol={sym}&___qn=3")
_TENCENT_KLINE_URL = ("https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
                      "param={code},day,,,10,qfq")


def _fetch_jpch_daily_closes(sym_key: str) -> Dict[str, float]:
    """复用 sampler 的 JP/CH 源抓日线收盘（OILUSA→SIX 官方 CSV，1671/1699→雅虎日本）。

    返回 {YYYY-MM-DD: price}；import 失败/抓取失败返回空 dict（调用方记 still_missing，
    不抛异常）。延迟 import：holding_valuation 为 namespace 包，backend 在 sys.path 即可。
    """
    try:
        from holding_valuation.usa_etf_history_sampler import fetch_symbol
        return fetch_symbol(sym_key, n=30)
    except Exception:
        return {}


def _http_get_text(url: str, timeout: int = 15) -> str:
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn" if "sina" in url else "https://gu.qq.com",
    })
    return urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode("gbk", "ignore")


def _sina_us_daily_closes(symbol: str) -> Dict[str, float]:
    """新浪美股日K最近收盘（{date: close}）。jsonp 用 find('(')~rfind(')') 截取。"""
    import json
    raw = _http_get_text(_SINA_US_DAILY_URL.format(sym=symbol))
    i, j = raw.find("("), raw.rfind(")")
    if i < 0 or j <= i:
        return {}
    arr = json.loads(raw[i + 1:j])
    return {x["d"]: float(x["c"]) for x in arr if x.get("d") and x.get("c")}


def _tencent_daily_closes(code: str) -> Dict[str, float]:
    """腾讯日K最近收盘（{date: close}）。
    [BUG-2026-10-06-05] 腾讯 K 线被 WAF 拦截：腾讯失败按市场兜底——
    港股 hkXXX → 东财（免费端仅返回日期，实际降级为空，依赖 ARM sampler）；
    美股 → 新浪 US_MinKService（_sina_us_daily_closes 已存在）；
    伦敦 ukXXX → 腾讯 usfqkline（ifzq.gtimg.cn 全路径 200，含本机住宅网络）。
        ⚠️ 注意：伦敦走 usfqkline，不是 txfqkline（后者 controller 已下架，死端点）。"""
    import json
    try:
        path = "usfqkline" if code.startswith("uk") else "fqkline"
        raw = _http_get_text(
            f"https://ifzq.gtimg.cn/appstock/app/{path}/get?param={code},day,,,10,qfq")
        obj = json.loads(raw)
        node = (obj.get("data") or {}).get(code) or {}
        days = node.get("day") or node.get("qfqday") or []
        out: Dict[str, float] = {}
        for d in days:
            if isinstance(d, (list, tuple)) and len(d) >= 3 and d[0]:
                try:
                    out[str(d[0])] = float(d[2])
                except (TypeError, ValueError):
                    continue
        if out:
            return out
        raise RuntimeError("腾讯返回空")
    except Exception as e:
        logger.warning(f"[holding] 腾讯 {code} 失败({e})，按市场兜底")
        if code.startswith("hk"):
            return _eastmoney_hk_daily_closes(code[2:])   # 东财港股兜底（降级）
        if code.startswith(("us", "gb")) or not code[0].isdigit():
            return _sina_us_daily_closes(code)            # 美股新浪兜底
        return {}                                          # 伦敦 ukXXX 无免费源


def _eastmoney_hk_daily_closes(hk_code: str) -> Dict[str, float]:
    """[BUG-2026-10-06-05] 东财港股兜底。免费端 kline 仅返回日期无 OHLC，
    故通常返回空 dict（优雅降级，不刷屏）；若东财恢复完整数据则自动生效。"""
    try:
        secid = f"116.{hk_code}" if (len(hk_code) == 5 and hk_code.isdigit()) else f"100.{hk_code}"
        em_url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
                  f"&ut=fa5fd1947f08e6d3cb8d1e3a8a3c7b2e&fields1=f1f1f2&fields2=f51f52f53f54f55f56"
                  f"&klt=101&fqt=0&end=20500101&lmt=10")
        txt = _http_get_text(em_url)
        em = json.loads(txt)
        kl = (em.get("data") or {}).get("klines") or []
        out: Dict[str, float] = {}
        for row in kl:
            parts = row.split(",")
            if len(parts) >= 3 and parts[0] and parts[2]:
                try:
                    out[parts[0]] = float(parts[2])
                except (TypeError, ValueError):
                    continue
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 持仓实时估值（Model B）配置
# ---------------------------------------------------------------------------
# 分母 = CL(WTI) 合约月冻结价（来自 ARM futures_freeze_prices，每天上午盘前拉一次）。
# 自 2026-09-17 起改为"两个对冲合约"：有效近月=当月+2（USO 在每月初 5-8 个工作日滚仓，
# 中下旬实际持有"次次月"合约），只保留近月与 +1 两月。
# 例: 9 月 → [2611,2612]；10 月自动滚动为 [2612,2701]。2610（主连）底层已滚过，估值对对冲无意义，移除。
#
# [2026-09-26 东哥拍板] 拆「必需三点 + 可选增强」，为 501018 日本腿补 0230 分母：
#   FREEZE_POINTS = 参与 base_date 判定的必需三点；
#   EXTRA_POINTS  = 只用于逐腿取价的增强点（0230 = TSE 15:30 JST），缺失时回落 1600。
# ⚠️ FREEZE_POINTS 绝不能追加 "0230"：base_date 判定是下方 _value_one_contract 调用方的
#   `HAVING COUNT(DISTINCT f.point) >= len(FREEZE_POINTS)`。历史每一天都只有 3 点
#   ⇒ 永远不满足 ⇒ freeze_base_date = None ⇒ 全库 fallback 到 nav_base_date，口径整体漂移。
FREEZE_POINTS = ["1130", "1430", "1600"]   # 必需：base_date 判定分母，长度不可改
EXTRA_POINTS = ["0230"]                     # 可选增强：仅 1671/1699（日本腿）取价用


def get_active_cl_contracts(as_of_date=None):
    """返回当前活跃的两个 CL 合约 YYMM 列表 [近月, +1月]。

    规则（2026-09-17 东哥拍板）：底层 ETF 已滚过 2610，主连 2610 估值对对冲无意义，
    只保留两个对冲合约。有效近月 = 当月 + 2（USO 在每月初 5-8 个工作日滚仓，
    中下旬实际持有"次次月"合约：如 9 月 → 近月=11 月 [2611,2612]）；
    下月自动滚动（10 月 → [2612,2701]）。
    """
    from datetime import date as _date
    if as_of_date is None:
        as_of_date = _date.today()
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


CL_CONTRACTS = get_active_cl_contracts()  # 模块加载时算一次（当天不变）
# 篮子标的 -> 其确定自身净值的 NY 时点（与 Model A 口径一致）：
#   CRUD = CME WTI 14:30 EDT；BRNT = ICE Brent 11:30 EDT（本期用 CL 带残差）；
#   1671/1699 = TSE 收盘 15:30 JST = 02:30 EDT（东京 Simplex WTI / 野村原油多头指数，
#               501018 日本腿共 11.04%；[2026-09-26] 补上，此前被兜底归到 1600，
#               分母锚点错位 13.5h，实测系统性 -1.74%（对估值 -0.18%））；
#   其余美股/港股原油 ETF 净值按 NY 收盘近似 -> 16:00。
# 取价时若映射到的点在 freeze 中缺失（历史日 / 未采到），逐腿回落 "1600"（见 _value_one_contract）。
POINT_BY_SYMBOL: Dict[str, str] = {
    "CRUD": "1430",
    "BRNT": "1130",
    "1671": "0230",
    "1699": "0230",
}

# ---------------------------------------------------------------------------
# 对冲穿透配置（etf_contract_exposure）：底层 ETF -> 实际持有合约月
# ---------------------------------------------------------------------------
# 用途：对冲页"该空哪个月"。静态估值(013_2, ETC 市价)与对冲(期货月份匹配)是两个独立问题：
# 估值用 ETC 市价即可，但对冲做空的是期货本身，必须穿透到底层 ETC 实际持有的合约月。
# 数据来源：ETF 官网持仓快照(USO/OILK/BNO) + 彭博官方日程表(CRUD=BCLMT4T, BRNT/BRNG=BCOMCO4T)。
# ⚠️ 指数每月滚动，本表需人工按月维护（as_of = 快照日期）：
#   - USO 每月初(4-8 工作日)滚仓；CRUD(BCLMT4T) 每月重置前移一个月；
#   - BRNT/BRNG(BCOMCO4T) 奇数月 6-10 工作日滚仓（2026-11 中旬 2701→2703）；
#   - OILK 三层梯式(Dec/Jun/Dec)基本稳定。
# 坑：CME 月代码 F/G/H/J/K/M/N/Q/U/V/X/Z —— CLZ6=Dec26（旧 yaml 把它标成 NOV2026 是错的）。
# ⚠️ futures_ratio 语义 =「名义敞口倍数」= 合同金额(期货+互换名义价值) ÷ 净资产，NOT「期货市值占净值比」！
#   期货 ETF 买期货只需保证金，剩余资产买国债现金；用「期货市值÷(期货+国债+现金)」算敞口会把合同金额
#   加进分母、自我稀释 → 敞口算成 ~44%/52%（错，系统性低估、空不够）。USO/BNO 都是 1 倍跟踪油价，
#   产品定义就接近满仓。USCF 官网 9/4 持仓表：USO 名义敞口 18.10÷17.21≈105%，BNO 6.44÷6.04≈107%
#   （SEC 10-Q 6/30 独立验证 BNO 108%）。详见已发表纠正文《3-纠正昨天原油基金的底层资产分析》。
# 种子结构：(etf, weight_pct, futures_ratio, structure, contract_month, inner_ratio, variety, mcl_ok, in_book)
#   in_book=1 → 计入表2 月份敞口聚合；in_book=0 → 结构特殊（动态单月/曲线分散），仅表1 标注「待核实」，不计入。
#   futures_ratio =「名义敞口倍数」= 合同金额(期货+互换名义价值) ÷ 净资产（非「期货市值占净值比」）。
#   weight_pct 单位为「占基金净值%」（季报权重 ×100）。
ETF_EXPOSURE_SEED = {
    "160723": [
        ("CRUD", 18.87, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2611", 1.0 / 3, "WTI", 1, 1),
        ("CRUD", 18.87, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2612", 1.0 / 3, "WTI", 1, 1),
        ("CRUD", 18.87, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2701", 1.0 / 3, "WTI", 1, 1),
        ("USO", 18.78, 1.05, "直接持期货+互换（每月初滚仓；名义敞口≈105%=合同金额÷净资产，非44%）", "2611", 1.0, "WTI", 1, 1),
        ("OILK", 18.54, 1.0, "直接持期货（官网披露三层梯式均布，全在远月；名义敞口≈满仓）", "2612", 1.0 / 3, "WTI", 0, 1),
        ("OILK", 18.54, 1.0, "直接持期货（官网披露三层梯式均布，全在远月；名义敞口≈满仓）", "2706", 1.0 / 3, "WTI", 0, 1),
        ("OILK", 18.54, 1.0, "直接持期货（官网披露三层梯式均布，全在远月；名义敞口≈满仓）", "2712", 1.0 / 3, "WTI", 0, 1),
        ("BNO", 18.37, 1.067, "直接持Brent近月（名义敞口≈107%=合同金额÷净资产，非52%；SEC 10-Q印证108%）", "2611", 1.0, "Brent", 0, 1),
        ("BRNT", 14.90, 1.0, "TRS→彭博BCOMCO4T（单合约，双月滚；2026-09-16 已滚至2701）", "2701", 1.0, "Brent", 0, 1),
        ("BRNG", 3.67, 1.0, "同BRNT（英镑份额）", "2701", 1.0, "Brent", 0, 1),
    ],
    # 161129（2026Q2 季报权重）：DBO 为动态单月、OILUSA 无（本基无）；DBO 标待核实
    "161129": [
        ("CRUD", 19.75, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2611", 1.0 / 3, "WTI", 1, 1),
        ("CRUD", 19.75, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2612", 1.0 / 3, "WTI", 1, 1),
        ("CRUD", 19.75, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2701", 1.0 / 3, "WTI", 1, 1),
        ("BRNT", 18.57, 1.0, "TRS→彭博BCOMCO4T（单合约，双月滚；2026-09-16 已滚至2701）", "2701", 1.0, "Brent", 0, 1),
        ("USO", 17.05, 1.05, "直接持期货+互换（每月初滚仓；名义敞口≈105%=合同金额÷净资产，非44%）", "2611", 1.0, "WTI", 1, 1),
        ("BNO", 12.57, 1.067, "直接持Brent近月（名义敞口≈107%=合同金额÷净资产，非52%；SEC 10-Q印证108%）", "2611", 1.0, "Brent", 0, 1),
        ("03175", 7.81, 1.0, "三星 S&P GSCI 原油 ER：近月滚动 WTI（韩元计价，结构同 USO 近月）", "2611", 1.0, "WTI", 1, 1),
        ("DBO", 18.20, 1.0, "DBIQ Optimum Yield：单张WTI，每月初从未来1-13月选滚动收益最优月（动态，需实测当前合约）", "待实测", 1.0, "WTI", 0, 0),
    ],
    # 501018（2026Q2 季报权重）：OILUSA 为 CMCI 曲线多期限分散，标待核实
    "501018": [
        ("BRNT", 18.79, 1.0, "TRS→彭博BCOMCO4T（单合约，双月滚；2026-09-16 已滚至2701）", "2701", 1.0, "Brent", 0, 1),
        ("BNO", 18.67, 1.067, "直接持Brent近月（名义敞口≈107%=合同金额÷净资产，非52%；SEC 10-Q印证108%；Nov26约9/30到期，9/18实测50/50滚动中）", "2611", 0.5, "Brent", 0, 1),
        ("BNO", 18.67, 1.067, "直接持Brent近月（名义敞口≈107%=合同金额÷净资产，非52%；SEC 10-Q印证108%；Nov26约9/30到期，9/18实测50/50滚动中）", "2612", 0.5, "Brent", 0, 1),
        ("CRUD", 18.66, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2611", 1.0 / 3, "WTI", 1, 1),
        ("CRUD", 18.66, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2612", 1.0 / 3, "WTI", 1, 1),
        ("CRUD", 18.66, 1.0, "TRS→彭博BCLMT4T（永久跳过近月，M2/M3/M4等权，每月重置）", "2701", 1.0 / 3, "WTI", 1, 1),
        ("USO", 18.41, 1.05, "直接持期货+互换（每月初滚仓；名义敞口≈105%=合同金额÷净资产，非44%）", "2611", 1.0, "WTI", 1, 1),
        ("1699", 5.62, 1.0, "野村 NOMURA 原油多头指数：WTI 2611/2612/2701 各约1/3（实测持仓）", "2611", 1.0 / 3, "WTI", 1, 1),
        ("1699", 5.62, 1.0, "野村 NOMURA 原油多头指数：WTI 2611/2612/2701 各约1/3（实测持仓）", "2612", 1.0 / 3, "WTI", 1, 1),
        ("1699", 5.62, 1.0, "野村 NOMURA 原油多头指数：WTI 2611/2612/2701 各约1/3（实测持仓）", "2701", 1.0 / 3, "WTI", 1, 1),
        ("1671", 5.42, 1.0, "东京 Simplex WTI：NYMEX WTI 近月（日元计价）", "2611", 1.0, "WTI", 1, 1),
        ("OILUSA", 8.47, 1.0, "UBS CMCI：WTI 曲线多期限分散(3月41%/6月22%/1年19%/2年11%/3年6%)，非单月", "曲线分散", 1.0, "WTI", 0, 0),
    ],
}
ETF_EXPOSURE_AS_OF = "2026-09-17"
ETF_EXPOSURE_SOURCE = "ETF官网持仓 + 彭博官方日程表 + 东京/韩国ETF招股书(1671/1699/03175) + 东哥纠正文(名义敞口倍数)"


class HoldingService:
    def __init__(self, db, market_data_service=None):
        self.db = db
        self.market_data_service = market_data_service

    def _get_conn(self):
        from arbcore.database.managers.base import ensure_wal_once
        ensure_wal_once(self.db.db_path)
        return sqlite3.connect(self.db.db_path, timeout=15.0)

    def get_periods(self, fund_code: str) -> List[Dict[str, str]]:
        """返回该基金可用的报告期列表（按报告日倒序，仅保留季度 Q1~Q4，过滤 H1/H2 等半年报）。"""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT report_period, MIN(report_date) AS report_date,
                       COUNT(DISTINCT symbol) AS holding_count
                FROM fund_report_holdings
                WHERE fund_code = ? AND report_period GLOB '[0-9]*Q[1-4]'
                GROUP BY report_period
                ORDER BY report_date DESC
                """,
                (fund_code,),
            )
            rows = cur.fetchall()
            return [
                {"period": r[0], "date": r[1], "holding_count": r[2]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_holdings(self, fund_code: str, report_period: str) -> Dict[str, Any]:
        """返回某报告期的持仓明细、地区分布、与上期变动。"""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT id, fund_code, report_period, report_date, symbol, name, name_en,
                       region, currency, type, operation_mode, manager, weight,
                       market_value, is_stock, sort_order
                FROM fund_report_holdings
                WHERE fund_code = ? AND report_period = ?
                ORDER BY is_stock, sort_order
                """,
                (fund_code, report_period),
            )
            rows = cur.fetchall()
            holdings = [
                {
                    "id": r[0],
                    "fund_code": r[1],
                    "report_period": r[2],
                    "report_date": r[3],
                    "symbol": r[4],
                    "name": r[5],
                    "name_en": r[6],
                    "region": r[7],
                    "currency": r[8],
                    "type": r[9],
                    "operation_mode": r[10],
                    "manager": r[11],
                    "weight": r[12],
                    "market_value": r[13],
                    "is_stock": bool(r[14]),
                    "sort_order": r[15],
                }
                for r in rows
            ]

            # Top10 持仓：按权重降序取前10（包含股票和基金，按 sort_order 分组后按权重排序）
            holdings_sorted = sorted(holdings, key=lambda x: -(x["weight"] or 0.0))
            top10 = holdings_sorted[:10]
            # 重新编号 sort_order
            for i, h in enumerate(top10):
                h["display_order"] = i + 1

            # 地区分布：按 region 聚合 weight（仅基金持仓，不含股票；股票单独列）
            region_map: Dict[str, float] = {}
            for h in holdings:
                if h["is_stock"]:
                    continue
                region = h["region"] or "其他"
                region_map[region] = region_map.get(region, 0.0) + (h["weight"] or 0.0)
            region_distribution = [
                {"region": k, "weight": v, "pct": round(v * 100, 2)}
                for k, v in sorted(region_map.items(), key=lambda x: -x[1])
            ]

            # 与上期变动：先算出上期Top10，再用上期Top10构建prev_symbols（只比较有资格进前十的）
            prev_period, prev_date = self._get_previous_period(conn, fund_code, report_period)
            prev_symbols = {}
            if prev_period:
                cur2 = conn.execute(
                    "SELECT symbol, name, weight FROM fund_report_holdings WHERE fund_code=? AND report_period=? ORDER BY weight DESC",
                    (fund_code, prev_period),
                )
                # 只取上期按权重前10的持仓（含股票），作为"上期有资格进前十"的基准
                for i, (sym, name, weight) in enumerate(cur2.fetchall()):
                    if i >= 10:
                        break
                    key = sym or name
                    prev_symbols[key] = {"symbol": sym, "name": name, "weight": weight}

            # 给 top10 注入 prev_weight
            for h in top10:
                key = h["symbol"] or h["name"]
                h["prev_weight"] = prev_symbols.get(key, {}).get("weight")

            exited = []
            for key, p in prev_symbols.items():
                if key not in {h["symbol"] or h["name"] for h in top10}:
                    exited.append(p)
            new_in = []
            for key, c in {h["symbol"] or h["name"]: h for h in top10}.items():
                if key not in prev_symbols:
                    new_in.append({
                        "symbol": c["symbol"],
                        "name": c["name"],
                        "weight": c["weight"],
                    })
            changed = []
            for h in top10:
                key = h["symbol"] or h["name"]
                if key in prev_symbols:
                    delta = (h["weight"] or 0.0) - (prev_symbols[key]["weight"] or 0.0)
                    if abs(delta) >= 0.0001:
                        changed.append({
                            "symbol": h["symbol"],
                            "name": h["name"],
                            "current_weight": h["weight"],
                            "prev_weight": prev_symbols[key]["weight"],
                            "delta": delta,
                            "delta_pct": round(delta * 100, 2),
                        })

            return {
                "fund_code": fund_code,
                "report_period": report_period,
                "report_date": holdings[0]["report_date"] if holdings else None,
                "holdings": top10,
                "region_distribution": region_distribution,
                "prev_period": prev_period,
                "prev_date": prev_date,
                "exited": exited,
                "new_in": new_in,
                "changed": sorted(changed, key=lambda x: -abs(x["delta"])),
            }
        finally:
            conn.close()

    def _get_previous_period(self, conn, fund_code: str, report_period: str):
        """按报告日找上一个有数据的报告期。"""
        cur = conn.execute(
            "SELECT report_date FROM fund_report_holdings WHERE fund_code=? AND report_period=? LIMIT 1",
            (fund_code, report_period),
        )
        row = cur.fetchone()
        if not row:
            return None, None
        report_date = row[0]
        cur2 = conn.execute(
            """
            SELECT report_period, report_date
            FROM fund_report_holdings
            WHERE fund_code = ? AND report_date < ?
            GROUP BY report_period, report_date
            ORDER BY report_date DESC
            LIMIT 1
            """,
            (fund_code, report_date),
        )
        row2 = cur2.fetchone()
        return row2 if row2 else (None, None)

    def get_valuation(self, fund_code: str, report_period: str) -> Dict[str, Any]:
        """季报持仓法实时估值。

        公式（简化版）：
            realtime_nav = report_nav * (1 + Σ(weight_i * (current_price_i / base_price_i - 1)))

        说明：
        - 报告期净值来自 unified_fund_history.nav
        - 底层标的报告日价格优先取 usa_etf_daily_prices.price
        - 当前价格来自 market_data_service.get_realtime_quote
        - MVP 暂不做汇率调整（底层标的价格波动远大于汇率波动）
        """
        conn = self._get_conn()
        try:
            holdings_info = self.get_holdings(fund_code, report_period)
            report_date = holdings_info["report_date"]
            holdings = [h for h in holdings_info["holdings"] if not h["is_stock"]]

            # 取报告期基金净值
            nav = None
            cur = conn.execute(
                "SELECT nav FROM unified_fund_history WHERE fund_code=? AND date=? AND nav IS NOT NULL AND nav>0",
                (fund_code, report_date),
            )
            row = cur.fetchone()
            if row:
                nav = float(row[0])

            # 逐标估值
            components = []
            total_contribution = 0.0
            valid_weight_sum = 0.0
            for h in holdings:
                symbol = h["symbol"]
                weight = h["weight"] or 0.0
                if not symbol:
                    components.append({
                        **h,
                        "base_price": None,
                        "current_price": None,
                        "change_pct": None,
                        "contribution": None,
                        "status": "missing_symbol",
                    })
                    continue

                # 报告日价格
                base_price = None
                cur2 = conn.execute(
                    "SELECT price FROM usa_etf_daily_prices WHERE symbol=? AND date=? AND price IS NOT NULL AND price>0",
                    (symbol, report_date),
                )
                r2 = cur2.fetchone()
                if r2:
                    base_price = float(r2[0])

                # 当前实时价格
                current_price = None
                if self.market_data_service:
                    try:
                        q = self.market_data_service.get_realtime_quote(symbol)
                        if q:
                            current_price = q.get("price") or q.get("bid") or q.get("last")
                            if current_price:
                                current_price = float(current_price)
                    except Exception:
                        pass

                if base_price and current_price and base_price > 0:
                    change_pct = current_price / base_price - 1.0
                    contribution = weight * change_pct
                    total_contribution += contribution
                    valid_weight_sum += weight
                    status = "ok"
                else:
                    change_pct = None
                    contribution = None
                    status = []
                    if base_price is None:
                        status.append("missing_base_price")
                    if current_price is None:
                        status.append("missing_current_price")
                    status = "|".join(status) if status else "unknown"

                components.append({
                    **h,
                    "base_price": base_price,
                    "current_price": current_price,
                    "change_pct": change_pct,
                    "contribution": contribution,
                    "status": status,
                })

            realtime_nav = None
            realtime_available = False
            if nav is not None and valid_weight_sum > 1e-6:
                realtime_nav = nav * (1.0 + total_contribution)
                realtime_available = True

            return {
                "fund_code": fund_code,
                "report_period": report_period,
                "report_date": report_date,
                "report_nav": nav,
                "realtime_nav": realtime_nav,
                "realtime_available": realtime_available,
                "total_change_pct": total_contribution,
                "valid_weight_sum": valid_weight_sum,
                "components": components,
            }
        finally:
            conn.close()

    def _load_period_weights(self, fund_code: str) -> List[Dict[str, Any]]:
        """从 DB 读取各季度报告期的持仓权重，按报告日升序。

        权重唯一权威 = fund_report_holdings.weight（小数存储，×100 得百分比）。
        规则：
        - 仅取季度报告期 Q1~Q4；半年报 H1/H2 与 Q2/Q4 同报告日且重复，已过滤
        - 股票持仓（is_stock=1）不进篮子
        - 按 SYMBOL_ALIAS 合并同一底层资产的多币种份额
        """
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT report_period, report_date, symbol, weight, is_stock, currency
                FROM fund_report_holdings
                WHERE fund_code = ? AND report_period GLOB '[0-9]*Q[1-4]'
                  AND weight IS NOT NULL
                ORDER BY report_date
                """,
                (fund_code,),
            )
            by_date: Dict[str, Dict[str, Any]] = {}
            for period, rdate, sym, w, is_stock, currency in cur.fetchall():
                if is_stock or not sym:
                    continue
                slot = by_date.setdefault(
                    rdate, {"period": period, "weights": {}, "cur": {}, "total": 0.0}
                )
                slot["total"] += w * 100.0
                basket_sym = SYMBOL_ALIAS.get(sym, sym)
                slot["weights"][basket_sym] = (
                    slot["weights"].get(basket_sym, 0.0) + w * 100.0
                )
                # 记录币种（别名合并后同资产同币种，取首个即可）
                if basket_sym not in slot["cur"]:
                    slot["cur"][basket_sym] = (currency or "USD").upper()
            return [
                {
                    "period": v["period"],
                    "report_date": k,
                    "weights": v["weights"],
                    "cur": v["cur"],
                    "total": round(v["total"], 4),
                }
                for k, v in sorted(by_date.items())
            ]
        finally:
            conn.close()

    @staticmethod
    def _pick_period(periods: List[Dict[str, Any]], date: str) -> Optional[Dict[str, Any]]:
        """按期切换：报告日 R 的持仓适用于 R 之后（不含 R）到下一份报告日之间。

        Q1(3-31) -> 4-01~6-30；Q2(6-30) -> 7-01~9-30。periods 必须按报告日升序。
        """
        chosen = None
        for p in periods:
            if p["report_date"] < date:
                chosen = p
            else:
                break
        return chosen

    def get_recalc_history(
        self,
        fund_code: str,
        report_period: str = None,
        start: str = "2026-07-01",
        min_coverage: float = None,
    ) -> Dict[str, Any]:
        """持仓静态估值历史（季报持仓法 Model A，按币种 FX 折算口径）。

        与旧版（160723 硬编码 Q2 五只权重）的区别：
        1. 权重按期切换 —— 报告日 R 的持仓只用于 R 之后到下一份报告日之间，
           绝不用一份权重复算全历史（用错期实测把误差放大 1.8~20 倍）
        2. 权重来源 DB（fund_report_holdings），不再硬编码，因此 161129 等基金通用
        3. 篮子可算权重覆盖率低于 MIN_COVERAGE 的报告期，仅返回不落库

        report_period 参数已废弃（保留仅为兼容前端调用），权重由日期自动路由。
        链式基数 = 官方净值(t-1)，不累积漂移。
        结果落库 unified_fund_history.holding_static_val。
        """
        cov_gate = MIN_COVERAGE if min_coverage is None else min_coverage
        periods = self._load_period_weights(fund_code)
        if not periods:
            return {
                "fund_code": fund_code, "start": start, "count": 0, "rows": [],
                "periods": [], "fx_mode": FX_MODE, "error": "no_report_holdings",
            }

        all_symbols = sorted({s for p in periods for s in p["weights"]})

        conn = self._get_conn()
        try:
            # [AI-2026-09-12] 不再过滤 nav IS NOT NULL：T+1 未公布净值的交易日（如 9-11 周五）
            # 仍应生成静态估值行——它依赖的是上一非空净值(T-1, 已公布)而非当日净值。
            # official_nav 缺失日返回 NULL（前端显示 '-'），但 holding_static_val 照常计算。
            nav_rows = conn.execute(
                "SELECT date, nav FROM unified_fund_history "
                "WHERE fund_code=? AND date>=? ORDER BY date",
                (fund_code, start),
            ).fetchall()
            price_rows = {}
            for s in all_symbols:
                price_rows[s] = {
                    r[0]: r[1] for r in conn.execute(
                        "SELECT date, price FROM usa_etf_daily_prices "
                        "WHERE symbol=? AND date>=? AND price IS NOT NULL AND price>0 ORDER BY date",
                        (s, start),
                    ).fetchall()
                }
            # [AI-2026-09-26 东哥拍板] 行挂靠改为「篮子 ETF 收盘价交易日」：官方净值未公布
            # 且净值源表连日期行都没有的日子（如 QDII T+1 的 2026-09-25）也生成估值行。
            # 依据：估值依赖的是上一非空净值(T-1)，当日净值只影响误差列（未公布显示 '-'，
            # 公布后重算自动回填）。已验证历史影响面=0：ETF 有价而净值表无行的日期仅新增
            # 当日，历史行的 prev_date 锚点不变。落库相应改为 upsert（净值表本无该行时插行，
            # 净值链路 save_unified_history 亦是 upsert，互不覆盖）。
            etf_dates = sorted({dt for rows in price_rows.values() for dt in rows})
            if etf_dates:
                _nav_map = dict(nav_rows)
                nav_rows = [(dt, _nav_map.get(dt))
                            for dt in sorted(set(_nav_map) | set(etf_dates))]
            fx_rows: Dict[str, Dict[str, float]] = {}
            for col in CURRENCY_FX_COL.values():
                fx_rows[col] = {
                    r[0]: r[1] for r in conn.execute(
                        f"SELECT date, {col} FROM exchange_rate "
                        f"WHERE {col} IS NOT NULL AND date>=? ORDER BY date",
                        (start,),
                    ).fetchall()
                }
            # [AI-2026-09-15] 美股时钟：参考大盘 ETF(SPY/QQQ) 在库中的最新日
            # = 美股最新"已收盘且已入库"的交易日。篮子含美股标的的 QDII，
            # 目标日 d 超过该时钟（且 d 非美股假期）时，美股部分必为前填凑数
            # ——只剩汇率噪声的假估值（9-15 实测：est=2.3583 纯汇率噪声）。
            # 该行不生成、不落库（宁缺毋假，东哥 2026-09-15 拍板）。
            us_clock_row = conn.execute(
                "SELECT MAX(date) FROM usa_etf_daily_prices "
                "WHERE symbol IN ('SPY','QQQ') AND price IS NOT NULL AND price>0"
            ).fetchone()
            us_clock = us_clock_row[0] if us_clock_row else None
            # [AI-2026-09-24 方案A] 待补日清单（只读）：正是被上面 us_clock 拦掉、未生成的那些 NAV 日。
            # 复用已算出的 us_clock，保证与拦截判定口径完全同源。
            pending_dates = self._us_price_pending_dates(conn, fund_code, start, us_clock)
        finally:
            conn.close()

        if not nav_rows:
            return {
                "fund_code": fund_code, "start": start, "count": 0, "rows": [],
                "periods": periods, "fx_mode": FX_MODE, "error": "no_nav",
            }

        # 每个报告期：按各自的适用区间 (本报告日, 下一报告日] 判定标的价格可得性。
        # 只有报告日快照、区间内没有连续序列的标的（1671 / BRNG 之类）必须剔除，
        # 否则前填会让它以"僵死价格"参与加权，等于凭空稀释篮子波动。
        nav_dates = [d for d, _ in nav_rows]
        for idx, p in enumerate(periods):
            lo = p["report_date"]
            hi = periods[idx + 1]["report_date"] if idx + 1 < len(periods) else nav_dates[-1]
            span = [d for d in nav_dates if lo < d <= hi]
            if not span:
                p.update(usable={}, pos=0.0, dropped=sorted(p["weights"]),
                         coverage=0.0, span=[])
                continue
            s0, s1 = span[0], span[-1]
            usable, dropped = {}, []
            for s, w in p["weights"].items():
                pts = sum(1 for d in price_rows.get(s, {}) if s0 <= d <= s1)
                if pts / len(span) >= MIN_SYMBOL_DAYS_RATIO:
                    usable[s] = w
                else:
                    dropped.append(s)
            pos = sum(usable.values())
            p.update(usable=usable, pos=round(pos, 4), dropped=sorted(dropped),
                     coverage=round(pos / p["total"], 4) if p["total"] > 0 else 0.0,
                     span=[s0, s1])

        def _fill(d: dict, dt: str):
            if dt in d:
                return d[dt]
            keys = [k for k in d if k <= dt]
            return d[keys[-1]] if keys else None

        rows = []
        # [AI-2026-10-08 东哥拍板·宁缺毋假收口] 被拦下的日期（非假期缺价，见下方拦截分支），
        # 与 pending_dates 合并进返回的 pending，供前端灰色「待补」占位。
        blocked_dates: List[Dict[str, Any]] = []
        stat: Dict[str, Dict[str, float]] = {}
        prev_nav = None
        # [AI-2026-09-28 锚点错配修复，东哥拍板方案A] prev_nav_date = 基数 prev_nav 所对应的
        # 篮子交易日。篮子变动起点必须与基数**同源**：此前取「紧邻前一行日期」而基数取
        # 「上一个非空净值」，当中间夹着 nav 为空的行（QDII T+1 未公布 / 跨假期顺延）时
        # 两者会错配一天 ⇒ 该行漏掉一段涨跌、估值系统性偏低。
        # 实测：2026-09-25（9-24 净值因中秋假期顺延未公布）三只原油 LOF 偏低 2.3~2.7%。
        prev_nav_date = None
        for i, (d, nav) in enumerate(nav_rows):
            # 美股时钟拦截：d 尚未收盘/未入库（非假期）→ 整行跳过，不生成不落库。
            # 假期日放行（假期前填合法）；prev_nav 链与其它 skip 分支保持一致。
            if us_clock and d > us_clock and not is_market_holiday("USO", d):
                if nav is not None:
                    prev_nav = nav
                    prev_nav_date = d
                continue
            prev_date = prev_nav_date      # 与 prev_nav 同源（2026-09-28 修复锚点错配）
            if prev_nav is None:
                # 还没有可用于推算(T-1)的上一个非空净值，跳过当日；
                # 仅当当日自身 nav 非空时才更新 prev_nav。
                if nav is not None:
                    prev_nav = nav
                    prev_nav_date = d
                continue
            per = self._pick_period(periods, d)
            if per is None or not per["usable"]:
                if nav is not None:
                    prev_nav = nav
                    prev_nav_date = d
                continue

            syms = list(per["usable"])
            p0 = {s: _fill(price_rows[s], prev_date) for s in syms}
            p1 = {s: _fill(price_rows[s], d) for s in syms}
            if any(p0[s] is None or p1[s] is None or p0[s] <= 0 for s in syms):
                prev_nav = nav
                prev_nav_date = d if nav is not None else None
                continue

            # 前填标注：区分"真实休市前填 ✅"与"非假期数据缺失前填 ⚠️"（东哥 2026-09-12 立规）。
            # 缺价的日期可能是 prev_date(T-1) 或 d(T 日)，分别按该标的市场判断是否真休市。
            # 判定**前置**到计算之前：非假期缺价要整行拦掉，不能等算完再判。
            note_parts: List[str] = []
            fill_warning = False
            for s in syms:
                miss_dates = [dt for dt in (prev_date, d) if dt not in price_rows[s]]
                for md in miss_dates:
                    if is_market_holiday(s, md):
                        note_parts.append(f"假期前填:{s}({md})")
                    else:
                        note_parts.append(f"⚠️非假期缺价:{s}({md})")
                        fill_warning = True

            # [AI-2026-10-08 东哥拍板·「腿价齐再算」] 非假期缺价 ⇒ 该行**不生成、不落库**。
            #
            # 原行为：_fill 前填一个"僵死价格"参与加权，照常落库，仅以 fill_warning 做展示标记。
            # 这与「宁缺毋假」铁律相悖，实证代价：2026-10-06 160723 = 2.270211 ——
            #   USO 当日(10-06)价在源里尚未出现（10-07 07:30 采样日志：USO 本地末条仍是
            #   10-05/143.99），08:05 的清晨刷新便前填 10-05 价算出该行；而同期 CRUD/BRNT
            #   （伦敦腿）当日是大跌的 —— 于是出现了"USO 明明涨、估值却跌"的自相矛盾，
            #   且要等下一次全量重算才可能被覆盖。完整数据应为 2.272917。
            #
            # 现改为：整行跳过（既不 append 到 rows、也不推进 prev_nav 的**取值**），
            # 同时登记到 blocked_dates ⇒ 前端以灰色「待补」行占位。
            # 代价是"少一行"，但原因可见；远胜留一行伪值误导核对。
            # 边界：**假期前填仍合法**（is_market_holiday 为真），该分支不受影响。
            if fill_warning:
                blocked_dates.append({
                    "date": d,
                    "report_period": per["period"],
                    "official_nav": round(nav, 6) if nav is not None else None,
                    "us_clock": us_clock,
                    "reason": "leg_price_missing",
                    "note": "; ".join(note_parts),
                    "message": "底层标的当日缺价（非假期），按「宁缺毋假」未生成估值",
                })
                if nav is not None:
                    prev_nav = nav
                    prev_nav_date = d
                continue
            # 逐标的 FX：本地涨跌 × 该币种汇率涨跌，再按权重加权成 r_basket。
            # 纯 USD 篮子（160723/161129 五只）每标的使用 usd_cny_mid，与旧口径数值一致；
            # 含 JPY/HKD 篮子（501018 的 1699/1671、161129 的 03175）按对应币种汇率折算。
            # 缺某标的的相关币种汇率则跳日（不兜底假数据）。
            pos = sum(per["usable"].values())
            r_basket = 0.0
            fx_detail: Dict[str, Dict[str, Any]] = {}
            fx_missing = False
            for s in syms:
                r_local = p1[s] / p0[s] - 1.0
                cur = per["cur"].get(s, "USD")
                col = CURRENCY_FX_COL.get(cur, "usd_cny_mid")
                fxc0 = _fill(fx_rows[col], prev_date)
                fxc1 = _fill(fx_rows[col], d)
                if fxc0 is None or fxc1 is None or fxc0 <= 0:
                    fx_missing = True
                    break
                r_fx = fxc1 / fxc0 - 1.0
                r_i = (1.0 + r_local) * (1.0 + r_fx) - 1.0
                r_basket += (per["usable"][s] / pos) * r_i
                # [AI-2026-10-05 东哥需求·导出核对 CSV] 除 r_fx 外额外带出汇率**两日原值**
                # fx0/fx1：人工核对时必须能从「汇率前日→汇率当日」独立复算 r_fx，
                # 而不是拿程序算好的 r_fx 去验证程序自己（自证等于没验）。
                fx_detail[s] = {
                    "cur": cur,
                    "fx0": round(fxc0, 6),
                    "fx1": round(fxc1, 6),
                    "r_fx": round(r_fx, 6),
                }
            if fx_missing:
                prev_nav = nav
                prev_nav_date = d if nav is not None else None
                continue

            est = prev_nav * (1.0 + pos / 100.0 * r_basket)
            # 当日官方净值缺失(T+1未公布)时估值仍可算(依赖上一非空净值)，但误差无法计算
            err = (est / nav - 1.0) if nav is not None else None

            # [AI-2026-09-22] 前填/沿用提示：标的在 T 日(d)无价即视为"沿用上一交易日收盘价"，
            # 与是否假期无关；区别于 fill_warning(非假期缺价=真缺数据)。供前端琥珀色提醒用户。
            # （note_parts / fill_warning 已前置到本行之前算出；走到这里 fill_warning 必为 False。）
            carried_symbols = [s for s in syms if d not in price_rows.get(s, {})]
            carried_forward = len(carried_symbols) > 0
            usd_ref = _fill(fx_rows["usd_cny_mid"], d)
            # [AI-2026-10-05 东哥需求·导出核对 CSV] 一并带出 USD/CNY 前一日值，
            # 让「汇率」这一项在人工核对时也是两日原值（而非只有 T 日 + 程序算的涨跌）。
            usd_ref_prev = _fill(fx_rows["usd_cny_mid"], prev_date)
            rows.append({
                "date": d,
                "report_period": per["period"],
                "coverage": per["coverage"],
                "official_nav": round(nav, 6) if nav is not None else None,
                "holding_static_val": round(est, 6),
                "err_pct": round(err * 100, 4) if err is not None else None,
                "err_bp": round(err * 10000, 2) if err is not None else None,
                "prev_official_nav": round(prev_nav, 6),
                # [AI-2026-10-08 东哥需求·锚点透明化] 本行 T-2（涨跌起点/分母）所对应的
                # 交易日 = 基数 prev_nav 的净值日（长假/停牌期间会早于本行日期数天）。
                # 纯展示字段、不参与任何计算；前端据此把展开表表头标成
                # 「T-2→T-1 收盘（自锚点 MM-DD 累计）」，避免被误读为"当日涨跌"。
                "anchor_date": prev_date,
                # [AI-2026-10-05 东哥需求·导出核对 CSV] 复算所需的三个"原始输入"，
                # 此前只存在于 periods[]（期级）与内部变量里，逐行导出后 CSV 才能自洽：
                #   pos_pct        = 该行有效权重和(%)，即 est = prev_nav*(1 + pos_pct/100*r_basket) 的 pos。
                #                    命名与 Model B（get_realtime_valuation）的 pos_pct 对齐 —— 两处
                #                    语义相同（r_basket 归一化的分母），不另立叫法。
                #   weights        = 该行各腿**原始**权重(%)（未按 pos 归一，归一由公式里 /pos_pct 完成）
                #   basket_change_pct = r_basket × 100，供与 Excel 公式列逐行比对
                "pos_pct": round(pos, 4),
                "weights": {s: round(per["usable"][s], 4) for s in syms},
                "basket_change_pct": round(r_basket * 100, 6),
                "etf_prices": {s: round(p1[s], 4) for s in syms},
                "etf_prev": {s: round(p0[s], 4) for s in syms},
                "usd_cny": round(usd_ref, 4) if usd_ref is not None else None,
                "usd_cny_prev": round(usd_ref_prev, 4) if usd_ref_prev is not None else None,
                "fx_detail": fx_detail,
                "note": "; ".join(note_parts) if note_parts else "",
                "fill_warning": fill_warning,
                "carried_forward": carried_forward,
                "carried_symbols": carried_symbols,
            })

            # 仅当当日官方净值存在(可算误差)才计入误差统计
            if err is not None:
                st = stat.setdefault(per["period"], {"n": 0, "abs_bp": 0.0, "max_abs_bp": 0.0})
                st["n"] += 1
                st["abs_bp"] += abs(err * 10000)
                st["max_abs_bp"] = max(st["max_abs_bp"], abs(err * 10000))

            # prev_nav 仅用非空净值推进；缺失日(如 T+1 未公布的 9-11)保持上一非空值，
            # 使后续可交易日的估值仍能依赖最近一个已公布净值推算。
            # [AI-2026-09-28] prev_nav_date 与 prev_nav 严格同步推进（锚点同源）。
            if nav is not None:
                prev_nav = nav
                prev_nav_date = d

        for k, v in stat.items():
            v["mean_abs_bp"] = round(v["abs_bp"] / v["n"], 2) if v["n"] else None
            v["abs_bp"] = round(v["abs_bp"], 2)
            v["max_abs_bp"] = round(v["max_abs_bp"], 2)

        # 落库 holding_static_val（先清零该基金；仅按"覆盖率达标"落库）。
        # [AI-2026-09-12] 去掉 note=="" 限制：个别标的缺价(真实休市前填 / 或非假期数据漏抓)
        # 时由 _fill 前填到最近交易日，估值仍是合理预测(东哥口径"静态估值即预测")，不应因此变 NULL；
        # note 字段透明标注"假期前填:SYM(日期)"或"⚠️非假期缺价:SYM(日期)"，fill_warning 标记
        # 非假期缺价行供前端标红核实。真正的低覆盖/大面积缺口日仍被 cov_gate 挡掉。
        # [AI-2026-10-08 东哥拍板·清零范围收窄] 原为 `WHERE fund_code=?` **无条件清全表**：
        # 调用方传入的 start 不同（定时任务 2026-01-01 / 前端 detail 2026-07-01），
        # 而 rows 只覆盖 start 之后 ⇒ start 之前的 hsv 被清光且不会被重写回来。
        # 危害：`/holding-recalc-detail` 用的是 start=2026-07-01 —— 一旦该端点恢复可用
        # （本轮 Bug 1 修复），用户每打开一次持仓分析页就会**清掉 ARM 库 1~6 月的 hsv**。
        # 与"全量重算"的语义对齐：清零范围必须 = rows 可覆盖的范围 = start 之后。
        # （完整历史由每日 08:05 `_step_oil_recalc`(start=2026-01-01) 负责全量重写。）
        conn = self._get_conn()
        try:
            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 holding_static_val 清零是谁做的"
            conn.execute(
                "UPDATE unified_fund_history SET holding_static_val=NULL, writer='lofarb:holding_service.recalc_clear' "
                "WHERE fund_code=? AND date>=?",
                (fund_code, start),
            )
            for r in rows:
                if r["coverage"] >= cov_gate:
                    # [AI-2026-09-26] upsert：净值源表无该日期行时插行（仅写 fund_code/date/
                    # holding_static_val，其余列留 NULL 由净值链路后续 upsert 填充）。
                    # 净值链路 save_unified_history 同为 ON CONFLICT upsert，互不覆盖。
                    # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 holding_static_val 是谁写的"
                    conn.execute(
                        "INSERT INTO unified_fund_history (date, fund_code, holding_static_val, writer) "
                        "VALUES (?, ?, ?, 'lofarb:holding_service.recalc') ON CONFLICT(date, fund_code) DO UPDATE "
                        "SET holding_static_val=excluded.holding_static_val, writer=excluded.writer",
                        (r["date"], fund_code, r["holding_static_val"]),
                    )
            conn.commit()
        finally:
            conn.close()

        return {
            "fund_code": fund_code,
            "start": start,
            "count": len(rows),
            "fx_mode": FX_MODE,
            "min_coverage": cov_gate,
            "periods": [
                {
                    "period": p["period"],
                    "report_date": p["report_date"],
                    "span": p.get("span") or [],  # 该期实际复算区间；空=落在 start 之前，未参与
                    "total_weight": p["total"],
                    "usable_weight": p["pos"],
                    "coverage": p["coverage"],
                    "dropped": p["dropped"],
                    "basket": {s: round(w, 4) for s, w in p["usable"].items()},
                    "stat": stat.get(p["period"]),
                }
                for p in periods
            ],
            "rows": list(reversed(rows)),  # 降序：最新在上
            # 降序：待补日 = ①美股价未入库（原有 A 方案语义）+ ②非假期缺价被拦（本轮新增）。
            "pending": sorted(
                list(pending_dates) + blocked_dates,
                key=lambda x: x["date"], reverse=True,
            ),
        }

    def _us_price_pending_dates(
        self, conn, fund_code: str, start: str, us_clock: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """[AI-2026-09-24 方案A] 静态估值为空、但按行挂靠本应有值的交易日（只读，不落库）。

        两类待补，按能否归因拆 reason：
        - `us_price_pending`：日期 > 美股时钟(SPY/QQQ 最新已入库收盘日) 且非美股假期
          ⇒ 美股价尚未入库（原 A 方案语义，与 get_recalc_history 的 us_clock 拦截同口径）。
        - `leg_price_missing`：其余空白日 ⇒ 底层标的当日缺价（非假期），被「腿价齐再算」
          规则拦下未落库（[AI-2026-10-08] 见 get_recalc_history 内注释）。

        这些日在重算时被整行跳过（宁缺毋假，东哥 2026-09-15 / 2026-10-08 拍板），
        因此读库表格会"少一行"，用户第一反应是"程序丢行了"。本方法把它们显式列出，
        供前端以灰色「待补」行占位 —— 既保留"不生成假估值"的红线，又让"哪天缺、为什么缺"一眼可见。

        [AI-2026-10-08 东哥拍板·判据放宽] 原判据要求 `nav IS NOT NULL`（源自"有净值才谈得上
        待补"），但 QDII 的 T+1 净值常整段为空（160723 自 2026-09-30 起 nav 全为 NULL），
        该判据下 pending **恒为空** —— 一旦静态估值因缺价被拦，前端既少一行又无任何提示。
        现改为「库里 hsv 为空即待补」，再按上述两类归因。
        """
        if us_clock is None:
            us_clock_row = conn.execute(
                "SELECT MAX(date) FROM usa_etf_daily_prices "
                "WHERE symbol IN ('SPY','QQQ') AND price IS NOT NULL AND price>0"
            ).fetchone()
            us_clock = us_clock_row[0] if us_clock_row else None
        hsv_null_rows = conn.execute(
            "SELECT date, nav FROM unified_fund_history "
            "WHERE fund_code=? AND date>=? AND holding_static_val IS NULL ORDER BY date",
            (fund_code, start),
        ).fetchall()
        # [AI-2026-10-08] 起点边界过滤：`get_recalc_history(start=D)` 需要 D 的**前一交易日**
        # 收盘价，但 price_rows 只从 start 起取 ⇒ D 当天天然算不出（prev 价取不到）。
        # 这类"起点边界空行"是查询口径的产物、不是数据缺口，不得当作待补（否则前端
        # 每次加载都会多出一行无意义的灰行）。判据：早于该基金首个 hsv 非空日的空行跳过。
        _hsv_min_row = conn.execute(
            "SELECT MIN(date) FROM unified_fund_history "
            "WHERE fund_code=? AND date>=? AND holding_static_val IS NOT NULL",
            (fund_code, start),
        ).fetchone()
        hsv_min = _hsv_min_row[0] if _hsv_min_row else None
        out: List[Dict[str, Any]] = []
        for d, nav in hsv_null_rows:
            if hsv_min and d < hsv_min:
                continue
            if us_clock and d > us_clock and not is_market_holiday("USO", d):
                out.append({
                    "date": d,
                    "official_nav": round(nav, 6) if nav is not None else None,
                    "us_clock": us_clock,
                    "reason": "us_price_pending",
                    "message": f"美股价未入库（美股时钟 {us_clock}），该行待补",
                })
            else:
                out.append({
                    "date": d,
                    "official_nav": round(nav, 6) if nav is not None else None,
                    "us_clock": us_clock,
                    "reason": "leg_price_missing",
                    "message": "底层标的当日缺价（非假期），按「宁缺毋假」未生成估值",
                })
        return out

    def get_local_static_valuation_rows(self, fund_code: str, start: str = "2026-01-01") -> Dict[str, Any]:
        """[AI-2026-09-23 B方案·本地缓存读取] 读本地 unified_fund_history 的 date / nav / holding_static_val。

        与 get_recalc_history（ARM 现算）不同：本方法只读本地已落库的 holding_static_val
        （由 ARM 自算后经 pull_oil_static_from_arm 日更拉回，或手动同步），不在展示时 SSH 代理 ARM。
        返回核心列 date / official_nav / holding_static_val / err_pct / err_bp（err 由 nav 与 holding_static_val 反算）。
        诊断细节（etf_prices / fill_warning / note）不在此列——前端弹窗打开时按需从 ARM 取全量。
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT date, nav, holding_static_val FROM unified_fund_history "
                "WHERE fund_code=? AND date>=? AND holding_static_val IS NOT NULL ORDER BY date",
                (fund_code, start),
            ).fetchall()
            # [AI-2026-09-24 方案A] 待补日：有净值但静态估值为空且已被美股时钟拦截的日子
            pending_dates = self._us_price_pending_dates(conn, fund_code, start)
        finally:
            conn.close()
        out = []
        for d, nav, hsv in rows:
            err = (hsv / nav - 1.0) if (nav is not None and nav != 0) else None
            out.append({
                "date": d,
                "official_nav": round(nav, 6) if nav is not None else None,
                "holding_static_val": round(hsv, 6),
                "err_pct": round(err * 100, 4) if err is not None else None,
                "err_bp": round(err * 10000, 2) if err is not None else None,
            })
        return {
            "fund_code": fund_code,
            "start": start,
            "count": len(out),
            "rows": list(reversed(out)),  # 降序：最新在上
            "pending": list(reversed(pending_dates)),  # 降序：美股价未入库的待补日
            "source": "local",
            "error": None,
        }

    # [AI-2026-09-21] 手喂外盘 ETF 收盘价：写 usa_etf_daily_prices 并重算持仓静态估值。
    # 红线（东哥 2026-09-18 立规）由调用方（main.py 路由）做 hard 校验：trade_date 必须 < 今天
    # （已收盘日），当天/未来盘中价绝不入库。这里只负责写入 + 重算落库。
    def manual_upsert_etf_prices(self, fund_code: str, trade_date: str, prices: list) -> Dict[str, Any]:
        """手喂外盘 ETF 收盘价并触发持仓静态估值重算落库。

        入参 prices: [{symbol, price}, ...]；trade_date 已由路由校验为已收盘日。
        写入 usa_etf_daily_prices（INSERT OR REPLACE，覆盖同日同标的），
        随后调 get_recalc_history 重算并落库 unified_fund_history.holding_static_val，
        返回更新后的 recalc 数据（与 get_recalc_history 同结构）供前端直接替换。
        """
        conn = self._get_conn()
        try:
            written = self._write_etf_price_rows(conn, trade_date, prices)
            conn.commit()
        finally:
            conn.close()

        if not written:
            return {"status": "error", "message": "没有有效价格被写入"}

        # 重算并落库（权重按日期自动路由；period 已废弃，传默认即可）
        recalc = self.get_recalc_history(fund_code, "2026H1", "2026-07-01")
        recalc["written"] = written
        return {"status": "ok", "data": recalc}

    @staticmethod
    def _write_etf_price_rows(conn, trade_date: str, prices: list) -> list:
        """把 [{symbol, price}] 写入 usa_etf_daily_prices（INSERT OR REPLACE，同日同标的覆盖）。

        返回实际写入的 [{symbol, price}]。纯写库，**不含重算**——供「手喂(ARM 重算)」与
        「本地副本(本机只存价)」两处共用，避免两份写库逻辑漂移。
        """
        written = []
        for p in prices:
            sym = (p.get("symbol") or "").strip()
            price = p.get("price")
            if not sym or price is None:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO usa_etf_daily_prices (date, symbol, price, updated_at) "
                "VALUES (?, ?, ?, (datetime('now','localtime')))",
                (trade_date, sym, price),
            )
            written.append({"symbol": sym, "price": price})
        return written

    def local_write_manual_etf_prices(self, trade_date: str, prices: list) -> Dict[str, Any]:
        """[AI-2026-09-24 东哥拍板] 手喂价在【本机】也留一份副本（仅写价格，绝不重算）。

        背景：手喂走 SSH 代理直写 ARM（B 方案），本机 usa_etf_daily_prices 原样不落，
        致本机数据不自洽（如 501018 的 OILUSA 停在 09-22），排查时两头对照费劲。
        故本机分支在 ARM 手喂成功后顺手写一份本地副本 —— 只作数据自洽/排查用途：
        **估值权威仍在 ARM**（本机不计算），静态估值照旧从 ARM 拉回。
        """
        conn = self._get_conn()
        try:
            written = self._write_etf_price_rows(conn, trade_date, prices)
            conn.commit()
        finally:
            conn.close()
        if not written:
            return {"status": "error", "message": "没有有效价格被写入"}
        return {"status": "ok", "written": written}

    # ------------------------------------------------------------------
    # 持仓实时估值（Model B）：季报持仓法 + CL 期货实时价
    # ------------------------------------------------------------------
    def get_realtime_valuation(self, fund_code: str) -> Dict[str, Any]:
        """持仓实时估值（Model B）—— 有效近月±1 三合约对比版。

        公式：est_now = base_nav * (1 + Σ (w_i/100) * (CL_now / CL_point_i - 1))
        自 2026-09-15 起同时计算多个合约（近月 +1，如 9 月 → 2611/2612）的估值：
          - 每个合约各自取本地 futures_freeze_prices 的冻结价作分母（symbol='CL{合约}'）；
            每条腿按 POINT_BY_SYMBOL 取自己定盘时刻的点（缺则该腿回落 1600）；
          - 分子实时抓对应合约新浪 hf_CL{合约}（与冻结采样合约严格一致）；
          - LOF 实时价 / 实时美元人民币为各合约共用输入（与实际基金、汇率相关，与合约无关）。
        返回结构含 contracts:{ '2611':{...}, '2612':{...} }，
        前端可任选一个作对冲基准并对比精度。
        selected_contract 默认取 CL_CONTRACTS[0]（当前近月），前端可切换。
        分母（futures_freeze_prices）需先经 sync_futures_freeze_from_arm 从 ARM 拉到本地；
        本方法只读本地缓存，不实时去 ARM（东哥：每天上午取一次即可）。
        某合约必需三点不齐 → 该合约 status='freeze_incomplete'，其余仍正常；整体 status='partial'。
        """
        today = datetime.now().strftime("%Y-%m-%d")

        # [AI-2026-09-15] 共用输入（与合约无关）提到最前：
        # 即便某合约后续走错误分支（冻结缺失/CL 抓取失败），LOF 现价/汇率也能带出，UI 不空白。
        lof_price, lof_price_source = self._fetch_lof_price(fund_code)
        # [AI-2026-09-16] fx_now 改为「今日人民币中间价」(exchange_rate.usd_cny_mid @ today)，
        # 不再抓取腾讯实时即期价 fxUSDCNY。原因：中间价与即期价天生差约 0.7%(价差非真实变动)，
        # 用即期价会凭空多塞一层假 FX 波动 → realtime_premium 失真。今日中间价盘前已发布，由 DB 读取
        # （见下方 conn 块赋值）。此处先置 None 占位，避免 early-return 引用未定义变量。
        fx_now = None

        periods = self._load_period_weights(fund_code)
        if not periods:
            return _rt_err(fund_code, "no_report_holdings",
                          lof_price=lof_price, lof_price_source=lof_price_source,
                          fx_now=fx_now)

        active = get_active_cl_contracts()  # 每次调用重算（跨月安全）
        brent_months: List[str] = []  # [AI-2026-09-17] Brent 书合约月（同月 CL 跨品种对冲反算用）

        conn = self._get_conn()
        self._ensure_freeze_table(conn)
        # [AI-2026-09-23] 确保 etf_contract_exposure 表存在（ARM 首次运行需种子）：
        # 下方 brent_months 查询直接依赖该表，ARM 库若从未跑过持仓分析会缺表报错。
        # 此处 ensure 仅建表+种子（种子为硬编码 ETF_EXPOSURE_SEED），不改变估值计算逻辑。
        self._ensure_exposure_table(conn)
        try:
            # [AI-2026-09-15] 基准日 = CL 采样日（分子分母同日对齐），但必须是「完整采样日」：
            # 近月合约（active[0]）在该日三时点齐全，且该日 holding_static_val 已落库。
            # 否则采样进行中（如 9-15 只来了 1430 一点、静态估值还是 NULL）会被选为基准，
            # base_nav 缺失 → 整体硬报错 → 前端 active_contracts 拿不到、单选组消失（当日实际 bug）。
            # 无完整采样日时 fallback 旧净值日口径，避免全空回归。
            near_sym = f"CL{active[0]}"
            fr = conn.execute(
                "SELECT f.trade_date FROM futures_freeze_prices f "
                "JOIN unified_fund_history u ON u.date = f.trade_date AND u.fund_code = ? "
                "WHERE f.symbol = ? AND f.trade_date <= ? "
                "GROUP BY f.trade_date "
                "HAVING COUNT(DISTINCT f.point) >= ? AND u.holding_static_val IS NOT NULL "
                "ORDER BY f.trade_date DESC LIMIT 1",
                (fund_code, near_sym, today, len(FREEZE_POINTS))).fetchone()
            freeze_base_date = fr[0] if fr else None

            nav_dates = [r[0] for r in conn.execute(
                "SELECT date FROM unified_fund_history "
                "WHERE fund_code=? AND nav IS NOT NULL AND nav>0 ORDER BY date",
                (fund_code,)).fetchall()]
            nav_base_date = max((d for d in nav_dates if d < today), default=None)

            base_date = freeze_base_date or nav_base_date
            if not base_date:
                return _rt_err(fund_code, "no_base_date", today=today,
                              lof_price=lof_price, lof_price_source=lof_price_source,
                              fx_now=fx_now)

            base_nav_row = conn.execute(
                "SELECT holding_static_val FROM unified_fund_history "
                "WHERE fund_code=? AND date=?", (fund_code, base_date)).fetchone()
            if base_nav_row is None or base_nav_row[0] is None:
                nav_row = conn.execute(
                    "SELECT nav FROM unified_fund_history "
                    "WHERE fund_code=? AND date=?", (fund_code, base_date)).fetchone()
                if nav_row is None or nav_row[0] is None:
                    return _rt_err(fund_code, "no_base_nav", base_date=base_date,
                                  lof_price=lof_price, lof_price_source=lof_price_source,
                                  fx_now=fx_now)
                base_nav = float(nav_row[0])
            else:
                base_nav = float(base_nav_row[0])

            per = self._pick_period(periods, base_date)
            if per is None:
                return _rt_err(fund_code, "no_period", base_date=base_date,
                              lof_price=lof_price, lof_price_source=lof_price_source,
                              fx_now=fx_now)
            basket = per["weights"]  # 已合并 BRNG/BNQA->BRNT，已剔除股票，单位 %

            # FX 时点价：base_date 的 usd_cny_mid（静态估值同源，全 USD 简化）。
            # 分母(三时点冻结价)改为按合约在 _value_one_contract 内各自查询。
            fx_row = conn.execute(
                "SELECT usd_cny_mid FROM exchange_rate WHERE date=?",
                (base_date,)).fetchone()
            fx_point = float(fx_row[0]) if (fx_row and fx_row[0] is not None) else None
            # [AI-2026-09-16] 今日人民币中间价（估值 FX 项用），DB 直接读取，盘前已发布，不抓实时即期。
            mid_row = conn.execute(
                "SELECT usd_cny_mid FROM exchange_rate WHERE date=?",
                (today,)).fetchone()
            fx_now = float(mid_row[0]) if (mid_row and mid_row[0] is not None) else None
            # [AI-2026-09-17] Brent 书合约月（in_book=1 已知单月）：同月 CL 跨品种对冲反算需要其估值，
            # 但不在 WTI active_contracts 中，单独查出追加进计算合约集（如 2701）。
            brent_months = [r[0] for r in conn.execute(
                "SELECT DISTINCT contract_month FROM etf_contract_exposure "
                "WHERE variety='Brent' AND in_book=1 "
                "AND contract_month IS NOT NULL AND contract_month != ''"
            ).fetchall()]
        finally:
            conn.close()

        # 逐合约计算估值（分母/分子各自合约严格一致）
        # [AI-2026-09-17] 计算合约 = WTI active + Brent 书合约月（同月 CL 跨品种对冲反算）
        compute_contracts: List[str] = list(dict.fromkeys(list(active) + brent_months))
        contracts: Dict[str, Any] = {}
        for contract in compute_contracts:
            contracts[contract] = self._value_one_contract(
                fund_code, contract, base_date, base_nav, per, basket,
                fx_point, fx_now, lof_price, lof_price_source, today)

        any_ok = any(c["status"] == "ok" for c in contracts.values())
        all_ok = all(c["status"] == "ok" for c in contracts.values())
        status = "ok" if all_ok else ("partial" if any_ok else "error")
        return {
            "fund_code": fund_code,
            "base_date": base_date,
            "base_nav": round(base_nav, 6),
            "selected_contract": active[0],
            "active_contracts": active,  # 前端渲染选择器用
            "contracts": contracts,
            "status": status,
            "message": None,
        }

    def _value_one_contract(self, fund_code, contract, base_date, base_nav, per, basket,
                           fx_point, fx_now, lof_price, lof_price_source, today) -> Dict[str, Any]:
        """对单个合约（如 '2610' 近月 / '2611' +1 / '2612' +2）计算完整估值。

        分母取本地 futures_freeze_prices 中 symbol='CL{contract}' 的冻结价；
        分子实时抓新浪 hf_CL{contract}。其余（base_nav/篮子/fx_point/lof_price/fx_now）与合约无关，由调用方传入。
        必需三点（FREEZE_POINTS）缺失返回 status='freeze_incomplete'，不兜底；
        可选增强点（EXTRA_POINTS，如 0230）缺失时该腿回落 1600，不算缺失、不报错。
        """
        sym = "CL" + contract
        conn = self._get_conn()
        try:
            freeze: Dict[str, Dict[str, Any]] = {}
            # [2026-09-26] 加载范围 = 必需三点 + 可选增强点（0230）。缺失判定只看 FREEZE_POINTS，
            # 增强点缺失不进 missing、不影响 status（取价时逐腿回落 1600）。
            for pt in FREEZE_POINTS + EXTRA_POINTS:
                # [AI-2026-09-15] 分母三点必须严格取基准日当天，杜绝手工测试行 /
                # 跨日残留污染分母（如 9-15 手工 1430 行顶掉 9-14 真实 1430）。
                # 原写法 trade_date<=today 取各点最新一条，会把非基准日价格混入。
                row = conn.execute(
                    "SELECT price, trade_date FROM futures_freeze_prices "
                    "WHERE symbol=? AND point=? AND trade_date=?",
                    (sym, pt, base_date)).fetchone()
                if row and row[0] is not None:
                    freeze[pt] = {"price": float(row[0]), "trade_date": row[1]}
        finally:
            conn.close()

        missing = [pt for pt in FREEZE_POINTS if pt not in freeze]
        if missing:
            # 即便冻结价缺失，也带出共用输入，避免 ETF 现价/汇率在 UI 空白
            return _rt_err(
                fund_code, "freeze_incomplete", base_date=base_date, contract=contract,
                missing_points=missing,
                lof_price=lof_price, lof_price_source=lof_price_source,
                fx_now=fx_now, fx_point=fx_point, fx_status="unknown_pre_freeze",
                message=f"CL{contract} 三时点冻结价缺失，请先调 sync_futures_freeze_from_arm / "
                        f"/api/fund/sync-freeze 从 ARM 拉取",
            )

        # 分子：实时抓该合约 CL（A 股盘中）
        try:
            cl_now, cl_time = self._fetch_cl_realtime(contract)
        except Exception as e:
            return _rt_err(fund_code, "cl_fetch_failed", base_date=base_date, contract=contract,
                          lof_price=lof_price, lof_price_source=lof_price_source,
                          fx_now=fx_now, fx_point=fx_point, fx_status="unknown_pre_cl",
                          message=str(e)[:200])

        # 计算
        components = []
        contrib_sum = 0.0
        valid_weight_pct = 0.0
        for s, w_pct in basket.items():
            pt = POINT_BY_SYMBOL.get(s, "1600")
            if pt not in freeze:
                # [2026-09-26] EXTRA_POINTS（0230）缺失时的逐腿回落，三种情形都会走到这里：
                #   ① 基准日早于本次改动（历史日从未采过 0230）；② ARM 该点当日未采到；
                #   ③ 该日恰逢美股假日周一，被 is_trading_day 守卫跳过。
                # 回落 1600 ⇒ 行为与改动前完全一致；0230 是增强项，绝不因此报 freeze_incomplete。
                pt = "1600"
            fp = freeze[pt]["price"]
            if fp <= 0:
                components.append({"symbol": s, "weight_pct": round(w_pct, 4),
                                   "point": pt, "status": "bad_freeze"})
                continue
            ratio = cl_now / fp - 1.0
            contrib = (w_pct / 100.0) * ratio  # w_pct 是%，/100 -> 小数权重
            contrib_sum += contrib
            valid_weight_pct += w_pct
            components.append({
                "symbol": s, "name": s, "weight_pct": round(w_pct, 4),
                "point": pt, "freeze_price": round(fp, 4),
                "freeze_date": freeze[pt]["trade_date"],
                "cl_now": round(cl_now, 4), "ratio": round(ratio, 6),
                "contrib": round(contrib, 6), "status": "ok",
            })

        # FX 合并（全 USD 简化，与静态估值口径一致：篮子变动与汇率变动合进同一 POS% 杠杆）
        pos_pct = per["total"]
        r_basket_norm = contrib_sum * 100.0 / pos_pct if pos_pct > 0 else 0.0
        if fx_point is None or fx_point <= 0:
            fx_point_used = fx_point
            fx_status = "fx_point_missing"
            r_fx_rt = 0.0
        elif fx_now is None or fx_now <= 0:
            # [AI-2026-09-16] 今日中间价缺失（DB 尚未刷新该日 usd_cny_mid）→ 不做 FX 调整，
            # 显式标红(fx_status=today_mid_missing) 等补数；绝不退化为实时即期价(会 reintroduce 假 FX)。
            fx_point_used = fx_point
            fx_status = "today_mid_missing"
            r_fx_rt = 0.0
        else:
            fx_point_used = fx_point
            fx_status = "ok"
            r_fx_rt = fx_now / fx_point_used - 1.0
        total_change = (pos_pct / 100.0) * ((1.0 + r_basket_norm) * (1.0 + r_fx_rt) - 1.0)
        est = base_nav * (1.0 + total_change)
        return {
            "fund_code": fund_code,
            "base_date": base_date,
            "base_nav": round(base_nav, 6),
            "contract": contract,
            "cl_now": round(cl_now, 4),
            "cl_time": cl_time,
            "cl_symbol": sym,
            "cl_contract_name": f"CL {contract[2:]}月 (hf_CL{contract})",
            "cl_contract_note": f"新浪 hf_CL{contract} 为 WTI {contract[2:]}月合约，与 ARM 三时点冻结采样合约一致",
            "freeze_trade_date": freeze["1600"]["trade_date"],
            "freeze_points": {
                # [2026-09-26] 输出含可选增强点（0230，前端冻结价矩阵逐行展示采样是否生效）
                pt: {"price": round(freeze[pt]["price"], 4), "trade_date": freeze[pt]["trade_date"]}
                for pt in FREEZE_POINTS + EXTRA_POINTS if pt in freeze
            },
            "realtime_nav": round(est, 6),
            "lof_price": round(lof_price, 4) if lof_price is not None else None,
            "lof_price_source": lof_price_source,
            "realtime_premium": round(lof_price / est - 1, 6)
            if (lof_price is not None and est and est > 0) else None,
            "total_change_pct": round(total_change, 6),
            "basket_change_pct": round(r_basket_norm, 6),
            "fx_change_pct": round(r_fx_rt, 6),
            "fx_now": round(fx_now, 4) if fx_now is not None else None,
            "fx_point": round(fx_point_used, 4) if fx_point_used is not None else None,
            "fx_status": fx_status,
            # [AI-2026-09-28 东哥需求] 显式输出 β 仓位(pos_pct) —— r_basket 归一化的分母。
            # 原来只隐含在 basket_change_pct 里，CSV 导出后在 Excel 无法精确复算；
            # 有腿被剔除(fp<=0)时 pos_pct ≠ valid_weight_sum，必须分开给。
            "pos_pct": round(pos_pct, 4),
            "valid_weight_sum": round(valid_weight_pct / 100.0, 4),
            "coverage": round(valid_weight_pct / per["total"], 4) if per["total"] > 0 else 0.0,
            "components": components,
            "status": "ok",
            "message": None,
        }

    # ------------------------------------------------------------------
    # 对冲穿透（etf_contract_exposure）：底层 ETF 实际持有合约月 + 归一化 CL 对冲分布
    # ------------------------------------------------------------------
    def _ensure_exposure_table(self, conn) -> None:
        """建表 + 首次种子（各基金仅当无数据时写入；此后 DB 为唯一权威，人工按月维护，代码不再覆盖）。
        in_book=0 的行（DBO/OILUSA 结构特殊）仅表1 标注「待核实」，不计入表2 月份敞口聚合。
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS etf_contract_exposure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_code TEXT NOT NULL,
                etf TEXT NOT NULL,
                weight_pct REAL NOT NULL,
                futures_ratio REAL NOT NULL,
                structure TEXT,
                contract_month TEXT NOT NULL,
                inner_ratio REAL NOT NULL,
                variety TEXT NOT NULL,
                mcl_ok INTEGER NOT NULL DEFAULT 1,
                in_book INTEGER NOT NULL DEFAULT 1,
                as_of TEXT NOT NULL,
                source TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # 兼容已有库：补 in_book 列（2026-09-17 新增）
        cols = [c[1] for c in conn.execute(
            "PRAGMA table_info(etf_contract_exposure)").fetchall()]
        if "in_book" not in cols:
            conn.execute(
                "ALTER TABLE etf_contract_exposure ADD COLUMN in_book INTEGER NOT NULL DEFAULT 1")
        # [2026-09-17 修正] 增量补种：同一基金下 (etf, contract_month) 唯一，
        # INSERT OR IGNORE 使「旧库已有 WTI 行、本次新增 Brent 行(如2701)」也能自动补入，
        # 不再因「无数据才播种」而跳过新增月份。人工维护的既有行不受影响（唯一键冲突即忽略）。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_exp_fund_etf_month "
            "ON etf_contract_exposure(fund_code, etf, contract_month)")
        # [2026-11-10] DB 唯一权威护栏：静态种子是 2026-09-17 的合约月快照，
        # 表一旦由 deploy/roll_oil_contracts.py 按月换月维护（存在更晚的 as_of），
        # 就不能再用旧种子补种 —— 否则已停用/已交割的过期合约月会被重新写回，
        # 导致采样器继续抓死价、hedge_plan 推荐空已摘牌合约。
        cur_as_of = conn.execute(
            "SELECT MAX(as_of) FROM etf_contract_exposure").fetchone()[0]
        if cur_as_of and cur_as_of > ETF_EXPOSURE_AS_OF:
            conn.commit()
            return
        for fund, rows in ETF_EXPOSURE_SEED.items():
            conn.executemany(
                "INSERT OR IGNORE INTO etf_contract_exposure "
                "(fund_code, etf, weight_pct, futures_ratio, structure, contract_month, "
                "inner_ratio, variety, mcl_ok, in_book, as_of, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(fund, etf, w, fr, struct, month, ir, variety, mcl_ok, in_book,
                  ETF_EXPOSURE_AS_OF, ETF_EXPOSURE_SOURCE)
                 for (etf, w, fr, struct, month, ir, variety, mcl_ok, in_book)
                 in rows],
            )
        conn.commit()

    def get_hedge_exposure(self, fund_code: str) -> Dict[str, Any]:
        """对冲穿透：底层 ETF 实际持有的期货月份 + 归一化 CL 对冲分布。

        占净值% = 季报权重% × futures_ratio(名义敞口倍数=合同金额÷净资产，非「期货市值占净值比」) × inner_ratio(合约内部比例)。
        WTI 书归一化后给出"该空哪个月"；Brent 书 CME 无 micro、单列（用同月 CL 跨品种近似对冲，见前端表2）。
        hedge_plan：mcl_ok=1 且敞口最大的两个月份（当前 2611+2612）按敞口比例配比。
        该基金无配置时返回空列表，前端隐藏两表（不报错、不兜底造数）。
        """
        conn = self._get_conn()
        try:
            self._ensure_exposure_table(conn)
            rows = conn.execute(
                "SELECT etf, weight_pct, futures_ratio, structure, contract_month, "
                "inner_ratio, variety, mcl_ok, in_book, as_of "
                "FROM etf_contract_exposure WHERE fund_code=? "
                "ORDER BY weight_pct DESC, contract_month",
                (fund_code,)).fetchall()
        finally:
            conn.close()

        if not rows:
            return {
                "fund_code": fund_code, "as_of": None, "etfs": [],
                "wti_months": [], "wti_book_nav_pct": 0.0,
                "brent": {"months": [], "book_nav_pct": 0.0},
                "hedge_plan": None, "pending_nav_pct": 0.0,
                "message": "该基金暂无对冲穿透配置（etf_contract_exposure 无数据）",
            }

        as_of = max(r[9] for r in rows)

        # 表1：按 ETF 分组（保持权重降序）；in_book=0 的 ETF 标 pending（待核实）
        etf_order: List[str] = []
        etf_map: Dict[str, Any] = {}
        for etf, w, fr, struct, month, ir, variety, mcl_ok, in_book, _as in rows:
            if etf not in etf_map:
                etf_order.append(etf)
                etf_map[etf] = {"etf": etf, "weight_pct": round(w, 2),
                                "futures_ratio": fr, "structure": struct,
                                "variety": variety, "contracts": [], "pending": False}
            if not in_book:
                etf_map[etf]["pending"] = True
            etf_map[etf]["contracts"].append(
                {"month": month, "pct": round(ir * 100, 1), "mcl_ok": bool(mcl_ok), "in_book": bool(in_book)})
        etfs = [etf_map[e] for e in etf_order]

        # 表2：按合约月聚合（仅 in_book=1 的已知单月 ETF；待核实 ETF 不计入，避免兜底假数据）
        wti_agg: Dict[str, Dict[str, Any]] = {}
        brent_agg: Dict[str, Dict[str, Any]] = {}
        wti_total = brent_total = pending_total = 0.0
        for etf, w, fr, _struct, month, ir, variety, mcl_ok, in_book, _as in rows:
            if not in_book:
                pending_total += w * fr * ir
                continue
            exp = w * fr * ir  # % of NAV
            if variety == "WTI":
                agg = wti_agg.setdefault(month, {"exp": 0.0, "mcl_ok": bool(mcl_ok), "from": []})
                agg["exp"] += exp
                agg["from"].append(etf)
                wti_total += exp
            else:
                agg = brent_agg.setdefault(month, {"exp": 0.0, "from": []})
                agg["exp"] += exp
                agg["from"].append(etf)
                brent_total += exp

        wti_months = [
            {"month": m, "exp_nav_pct": round(v["exp"], 1),
             "book_pct": round(v["exp"] / wti_total * 100, 1) if wti_total > 0 else 0.0,
             "from": "+".join(sorted(set(v["from"]))), "mcl_ok": v["mcl_ok"]}
            for m, v in sorted(wti_agg.items())
        ]
        brent_months = [
            {"month": m, "exp_nav_pct": round(v["exp"], 1),
             "book_pct": round(v["exp"] / brent_total * 100, 1) if brent_total > 0 else 0.0,
             "from": "+".join(sorted(set(v["from"])))}
            for m, v in sorted(brent_agg.items())
        ]

        # 进阶对冲方案：mcl_ok=1 且敞口最大的两个月份，按敞口比例配比
        hedge_plan = None
        plan = sorted([m for m in wti_months if m["mcl_ok"]],
                      key=lambda m: -m["exp_nav_pct"])[:2]
        if len(plan) == 2:
            s = sum(m["exp_nav_pct"] for m in plan)
            weights = [round(m["exp_nav_pct"] / s * 100) for m in plan]
            weights[-1] = 100 - weights[0]  # 修正取整误差，保证合计 100
            hedge_plan = {
                "months": [m["month"] for m in plan],
                "weights": weights,
                "coverage_book_pct": round(s / wti_total * 100, 1) if wti_total > 0 else 0.0,
                "coverage_nav_pct": round(s, 1),
            }

        return {
            "fund_code": fund_code,
            "as_of": as_of,
            "etfs": etfs,
            "wti_months": wti_months,
            "wti_book_nav_pct": round(wti_total, 1),
            "brent": {"months": brent_months, "book_nav_pct": round(brent_total, 1)},
            "hedge_plan": hedge_plan,
            "pending_nav_pct": round(pending_total, 1),
            "message": None,
        }

    @staticmethod
    def _fetch_cl_realtime(contract: str, max_retry: int = 3) -> Any:
        """实时抓指定远月合约 CL(WTI) 价（新浪 hf_CL{contract}，如 hf_CL2611）。与 cl_freeze_sampler.fetch_cl 同源。

        [2026-09-22 加固] 加 3 次重试 + 短退避：美股盘中 Sina hf_ 期货源偶有瞬时抖动，
        单次超时/空响应不应导致 cl_now=None、估值直接报错。
        ⚠️ 第一性原理（东哥铁律）：任何情况下绝不回退冻结价（92.027 等历史冻结值）顶替实时价——
        真正取不到就显式抛错，由上层 status='cl_fetch_failed' 拒绝估值（宁可无估值，绝不用旧价）。
        """
        import re
        import time
        import urllib.request
        code = "hf_CL" + contract
        last_err = None
        for attempt in range(max_retry):
            try:
                url = "https://hq.sinajs.cn/list=" + code
                req = urllib.request.Request(
                    url, headers={"Referer": "https://finance.sina.com.cn",
                                  "User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
                m = re.search(r'var hq_str_' + code + r'="(.*?)"', raw)
                if not m:
                    raise ValueError(f"新浪未匹配 {code}")
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
                    raise ValueError(f"新浪 {code} 无有效价格")
                t = fields[-1].strip() if fields else ""
                return price, t
            except Exception as e:
                last_err = e
                if attempt < max_retry - 1:
                    time.sleep(0.3 * (attempt + 1))
        raise ValueError(f"新浪 {code} 实时价抓取失败(已重试{max_retry}次): {last_err}")

    @staticmethod
    def _fetch_usdcny_realtime() -> float:
        """实时美元人民币（腾讯 fxUSDCNY，在岸价）。与静态估值 usd_cny_mid 同源（全 USD 简化）。"""
        import re
        import urllib.request
        url = "http://qt.gtimg.cn/q=fxUSDCNY"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", "ignore")
        m = re.search(r'v_fxUSDCNY="(.*?)"', raw)
        if not m:
            raise ValueError("腾讯未匹配 fxUSDCNY")
        fields = m.group(1).split("~")
        if len(fields) < 4:
            raise ValueError("腾讯 fxUSDCNY 字段不足")
        try:
            return float(fields[3])
        except ValueError:
            raise ValueError("腾讯 fxUSDCNY 无有效价格")

    def _fetch_lof_price(self, fund_code: str):
        """LOF 基金实时价（与主看板"现价"同源）。

        - A 股盘中：走 market_data_service.get_realtime_quote（腾讯/新浪，与主看板现价同一入口）；
        - 盘后/休市/非交易日：腾讯接口会把它"最近收盘价"当"当前价"返回，price>0 但实为昨收/收盘，
          必须显式改标（见下方 relabel），否则弹窗会把昨收误显成"腾讯实时"误导用户；
        - 接口彻底失败：回退到 unified_fund_history 最近官方收盘价（src='close'）。
        返回 (price, source)：source ∈ {'realtime:腾讯'/'realtime:新浪'/'昨收'/'收盘'/'close'/None}。取不到返回 (None, None)。
        """
        price = None
        src = None
        if self.market_data_service:
            try:
                q = self.market_data_service.get_realtime_quote(fund_code)
                if q and q.get("price", 0) > 0:
                    price = float(q["price"])
                    src = "realtime:" + str(q.get("source", "mds"))
            except Exception as e:
                logger.debug(f"[{fund_code}] LOF 实时价获取失败(回退收盘): {e}")
        # [AI-2026-09-23] 非 A 股盘中时腾讯返回的"当前价"实为最近收盘价，必须改标，
        # 否则盘前弹窗会把昨收(2.272)误显成"腾讯实时"，误导场内/赎回判断。
        if price is not None and src and src.startswith("realtime") and not self._is_a_share_open():
            from arbcore.utils.market_calendar import is_trading_day
            now = datetime.now(timezone(timedelta(hours=8)))
            # 交易日且已过 15:00 → 今日已收盘；其余(盘前/休市/周末)→ 昨收
            if is_trading_day('A_SHARE', now.date()) and (now.hour * 60 + now.minute) >= 900:
                src = "收盘"
            else:
                src = "昨收"
        if price is None:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT price FROM unified_fund_history "
                    "WHERE fund_code=? AND price IS NOT NULL AND price>0 "
                    "ORDER BY date DESC LIMIT 1",
                    (fund_code,)).fetchone()
                if row:
                    price = float(row[0])
                    src = "close"
            finally:
                conn.close()
        return price, src

    @staticmethod
    def _is_a_share_open() -> bool:
        """当前是否处于 A 股可交易时段（9:30-15:00，含午休，强制 UTC+8）。"""
        try:
            from arbcore.utils.market_calendar import is_a_share_session
            return bool(is_a_share_session())
        except Exception:
            return True  # 降级：拿不到时段判定时不拦截，避免误吞实时价

    @staticmethod
    def _ensure_freeze_table(conn) -> None:
        """确保本地 futures_freeze_prices 表存在（与 ARM cl_freeze_sampler 同结构）。"""
        conn.execute(
            """
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
        )

    def _fallback_fill_missing_etf(self, conn) -> Dict[str, Any]:
        """同步后兜底：本地缺"最新已收盘交易日"收盘价的标的，直连源补抓一次。

        遍历范围 = fund_basket_weights 最新篮子 ∪ fund_report_holdings 持仓篮子的
        distinct symbol 合集（woody 估值 + 季度持仓估值两层实际用到的标的），不再遍历
        usa_etf_daily_prices 全表——这样已调仓/无关注标的（如 160644 调出后的 GOOGL）
        不会一起报红，消除噪音。篮子表为空时退化回全表，保证不丢补抓能力。

        参考日 = 新浪 SPY 最新日（即新浪口径的美股最新已收盘交易日，独立于本地库，
        避免整库同步延迟时"库内最新日"自我参照漏判）。逐标的检查：
        - 该标的所在市场当日休市（is_market_holiday）→ 跳过，不算缺；
        - JP/CH（OILUSA/1671/1699）[AI-2026-09-26] 已有自动源（ARM sampler：SIX/雅虎日本），
          本机重抓直接复用 sampler 抓取函数，基准日用该市场自己的最后交易日；
        - 缺价 → 按 _market_of 选源补抓参考日收盘价，成功即 INSERT OR REPLACE 落库。
        返回 {reference_date, filled:{sym:price}, still_missing:[sym]}，绝不抛异常。
        """
        result: Dict[str, Any] = {"reference_date": None, "filled": {}, "still_missing": []}
        try:
            spy = _sina_us_daily_closes("SPY")
            if not spy:
                result["error"] = "新浪SPY参考日获取失败"
                return result
            ref_date = max(spy)
            result["reference_date"] = ref_date
            # 兜底范围 = union(woody 篮子 fund_basket_weights 最新篮子, 季度持仓篮子
            #  fund_report_holdings)：两层估值实际用到的标的，避免 GOOGL 等已调仓/无关
            #  注标的报红噪音；JP/CH 手动喂标的本就会被下方 mkt 判断跳过。
            # [AI-2026-09-23] 单一真相源：symbol_master（DB 权威）取代原先
            #  fund_basket_weights∪fund_report_holdings 动态 UNION。只取走新浪/腾讯可补的
            #  海外市场 ETF（含 woody 合成标的由下方 skip 规则自然跳过）；OTHER 类（A股成分如
            #  SZ159560 等）不走美股接口、由 A股抓取链路负责，排除避免 still_missing 噪音。
            #  symbol_master 缺失/为空时退化回全表，保证不丢补抓能力。
            sm_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbol_master'"
            ).fetchone()
            if sm_exists:
                symbols = [r[0] for r in conn.execute(
                    "SELECT symbol FROM symbol_master WHERE active=1 AND asset_type='ETF' "
                    "AND market IN ('US','JP','CH','LONDON','HK','SYNTHETIC') "
                    "ORDER BY symbol").fetchall()]
                if not symbols:
                    symbols = [r[0] for r in conn.execute(
                        "SELECT DISTINCT symbol FROM usa_etf_daily_prices "
                        "ORDER BY symbol").fetchall()]
            else:
                symbols = [r[0] for r in conn.execute(
                    "SELECT DISTINCT symbol FROM usa_etf_daily_prices "
                    "ORDER BY symbol").fetchall()]
            tried: list = []
            for sym in symbols:
                # BRNG/BNQA 是 BRNT 的 GBP/EUR 份额别名：USD 价一致，按 BRNT(UK) 处理。
                sym_key = SYMBOL_ALIAS.get(sym, sym)
                # 跳过非美股命名的特殊代码（00700/0857.HK/^GLD-JP/sz159560 等）：
                # 美股源必失败，且不属美股日K管辖（HK/JP 已有映射的除外）
                if sym_key not in SYMBOL_MARKET and (
                        re.search(r"[.^]", sym_key) or sym_key.isdigit()
                        or re.match(r"^[a-z]{2}\d+$", sym_key)):
                    continue
                # znb_DAX 等"指数代理代码"（含下划线且非全小写）非美股/英/港 ETF，
                # 补抓必失败，直接跳过避免徒劳报红
                if "_" in sym_key and not sym_key.islower():
                    continue
                mkt = _market_of(sym_key)
                if mkt in ("JP", "CH"):
                    # [AI-2026-09-26] JP/CH 已有自动源（ARM sampler：SIX/雅虎日本），
                    # 本机重抓复用 sampler 抓取函数；基准日用该市场自己的最后交易日。
                    base = _last_trading_day(sym_key, ref_date)
                    if not base:
                        continue
                    if conn.execute(
                        "SELECT 1 FROM usa_etf_daily_prices WHERE symbol=? AND date=?",
                        (sym, base)).fetchone():
                        continue
                    tried.append(sym)
                    closes = _fetch_jpch_daily_closes(sym_key)
                    price = closes.get(base)
                    if price is not None:
                        conn.execute(
                            "INSERT OR REPLACE INTO usa_etf_daily_prices "
                            "(date, symbol, price, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                            (base, sym, price))
                        result["filled"][sym] = price
                    continue
                if is_market_holiday(sym_key, ref_date):
                    continue
                has = conn.execute(
                    "SELECT 1 FROM usa_etf_daily_prices WHERE symbol=? AND date=?",
                    (sym, ref_date)).fetchone()
                if has:
                    continue
                tried.append(sym)
                try:
                    if mkt == "UK":
                        closes = _tencent_daily_closes("uk" + sym_key)
                    elif mkt == "HK":
                        closes = _tencent_daily_closes("hk" + sym_key)
                    else:
                        closes = _sina_us_daily_closes(sym_key)
                except Exception:
                    closes = {}
                price = closes.get(ref_date)
                if price is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO usa_etf_daily_prices "
                        "(date, symbol, price, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                        (ref_date, sym, price))
                    result["filled"][sym] = price
            conn.commit()
            result["still_missing"] = [s for s in tried if s not in result["filled"]]
        except Exception as e:
            result["error"] = str(e)[:200]
        return result

    def sync_usa_etf_from_arm(self) -> Dict[str, Any]:
        """从 ARM 拉 usa_etf_daily_prices 全表到本地库（用户手动触发）。

        全表镜像：ARM 上 usa-etf-history.timer 每日增量累积（截至昨日北京时间），
        本地只在用户点按钮时把 ARM 整张表拉回，因此"停用 N 天后点一次"会补齐这 N 天
        （以及此前任何缺失）的全部历史，不存在 90 天窗口漏数据的问题。
        用 INSERT OR REPLACE 按 (date, symbol) 覆盖交集，本地更长历史的行不会被删。
        失败就地返回 dict，绝不抛异常。
        """
        import json
        import os
        import subprocess
        import tempfile

        remote_script = (
            "import sqlite3, json\n"
            "c = sqlite3.connect('/home/ubuntu/arbtest/database/arb_master.db')\n"
            "rows = c.execute(\"\"\"SELECT date, symbol, price, netvalue, updated_at "
            "FROM usa_etf_daily_prices ORDER BY symbol, date\"\"\").fetchall()\n"
            "print(json.dumps(rows))\n"
        )
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                f.write(remote_script)
                tmp = f.name
            cmd = 'ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o BatchMode=yes arm "python3 -"'
            proc = subprocess.run(cmd, shell=True, stdin=open(tmp, "r"),
                                  capture_output=True, encoding="utf-8", errors="replace", timeout=60)
            if proc.returncode != 0:
                return {"status": "error",
                        "message": f"SSH 查询 ARM 失败: {(proc.stderr or proc.stdout or '').strip()[:300]}"}
            out = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            rows = json.loads(out)
            if not rows:
                return {"status": "ok", "updated": 0,
                        "message": "ARM 无 usa_etf_daily_prices 数据"}

            conn = self._get_conn()
            try:
                self._ensure_usa_etf_table(conn)
                n = 0
                for r in rows:
                    date, symbol, price, netvalue, updated_at = r
                    conn.execute(
                        "INSERT OR REPLACE INTO usa_etf_daily_prices "
                        "(date, symbol, price, netvalue, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (date, symbol, price, netvalue, updated_at),
                    )
                    n += 1
                conn.commit()
                # 防御：只保留 2026 年起（东哥 2026-09-12 拍板；ARM 表本就只含 2026+，此句防日后 ARM 出现脏老行回流）
                conn.execute("DELETE FROM usa_etf_daily_prices WHERE date < ?", ("2026-01-01",))
                conn.commit()
                # 兜底补抓：ARM 当天源延迟抓不全时，本地直连新浪/腾讯补一次（东哥 2026-09-15 提议）
                fallback = self._fallback_fill_missing_etf(conn)
                # 统计本地覆盖范围（仅取本地也关心的标的 + 总行数），供前端展示
                cur = conn.execute(
                    "SELECT COUNT(*), MIN(date), MAX(date) FROM usa_etf_daily_prices"
                )
                total, first, last = cur.fetchone()
            finally:
                conn.close()
            message = f"从 ARM 同步 {n} 条；本地现有 {total} 条（{first}~{last}）"
            if fallback.get("filled"):
                message += f"；本地补抓 {len(fallback['filled'])} 只（{fallback['reference_date']}）"
            if fallback.get("still_missing"):
                message += f"；⚠️仍缺 {fallback['reference_date']} 收盘: {','.join(fallback['still_missing'])}"
            if fallback.get("error"):
                message += f"；兜底补抓失败({fallback['error']})"
            return {"status": "ok", "updated": n,
                    "total": total,
                    "first": first,
                    "last": last,
                    "fallback": fallback,
                    "message": message}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "SSH 超时（arm 不可达）"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:300]}
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # [AI-2026-09-24 东哥需求] ARM 美股价新鲜度检测 + 本机一键重抓推送。
    # 背景：ARM 采集器固定 07:30 跑（09-24 从 06:00 改），而新浪美股源在该时刻常尚未
    #   更新 T-1 收盘价（实测：06:00 连续 12 次 0 成功、09:34 成功）→ ARM 缺价 →
    #   静态估值不起新行（"宁缺毋假"拦截）。
    # 东哥定的闭环：本机 UI 提示「新浪未抓到」→ 一键重抓 → 自动推 ARM → ARM 重算 → 拉回本地。
    # 本机非 24h 开机（东哥 2026-09-24 明确），故【不做每日自动调度】，只做手动一键 + 检测提示。
    # ------------------------------------------------------------------

    def refetch_usa_etf_from_source(self) -> Dict[str, Any]:
        """[AI-2026-09-24] 本机直连源（新浪/腾讯）重抓「最新已收盘交易日」缺价标的。

        公开入口，包装 _fallback_fill_missing_etf（专供 /api/fund/oil-refetch-prices 调用）。
        本地为价格权威（东哥铁律）：只补本地缺口，绝不从 ARM 反向覆盖本地已有值。
        返回 {reference_date, filled:{sym:price}, still_missing:[...]}，绝不抛异常。

        [AI-2026-10-11 东哥拍板]**定位降级为「人工应急」**：东哥第一性原理 ——
          **ARM 必须自主爬数据，不能依赖本机去纠偏**。ARM 侧正解 = sampler 多个补采时点自愈 +
          缺数报警（而非等本机来救）。本方法保留作**人工应急通道**（仅本机可调；
          ARM 侧 nginx 已对 `/api/fund/oil-refetch-prices` 返回 403，用户根本调不到）。
        """
        conn = self._get_conn()
        try:
            self._ensure_usa_etf_table(conn)
            return self._fallback_fill_missing_etf(conn)
        except Exception as e:
            return {"reference_date": None, "filled": {}, "still_missing": [],
                    "error": str(e)[:200]}
        finally:
            conn.close()

    def get_us_price_freshness(self, fund_codes=("160723", "161129", "501018")) -> Dict[str, Any]:
        """[AI-2026-09-24] 只读检测：ARM 是否已抓到「新浪口径的美股最新已收盘交易日」。

        判据口径与 get_recalc_history 的 us_clock 拦截【严格一致】（口径不一致会给出误导提示）：
        - reference_date = 新浪 SPY 最新日（独立于本地/ARM 库，避免自我参照漏判）；
        - arm_clock = ARM 库 SPY/QQQ 的 MAX(date)，即静态估值实际用的美股时钟；
        - stale ⟺ arm_clock < reference_date（此时 ARM 整行被跳过、不起新行）。

        missing_auto    = 原油三基金季报篮子里参考日缺价的标的（含 JP/CH——
                         [AI-2026-09-26] 三者已由 ARM sampler 自动抓取，不再区分手喂类；
                         基准日仍按市场区分：JP/CH 用该市场自己的最后交易日）。
        绝不抛异常；失败时 error 字段给出原因。
        """
        result: Dict[str, Any] = {
            "reference_date": None, "arm_clock": None, "arm_latest": None,
            "stale": False, "missing_auto": [],
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "error": None,
        }
        # 1) 新浪口径参考日
        try:
            spy = _sina_us_daily_closes("SPY")
        except Exception as e:
            result["error"] = f"新浪 SPY 参考日获取失败: {str(e)[:120]}"
            return result
        if not spy:
            result["error"] = "新浪 SPY 参考日获取失败（空响应）"
            return result
        ref_date = max(spy)
        result["reference_date"] = ref_date

        # 2) 季报篮子标的（最新报告期），统一 (sym, 基准日) 结构。
        # [AI-2026-09-26] JP/CH 已有自动源（ARM sampler：SIX/雅虎日本），不再归手喂类；
        # 但基准日仍按市场区分——JP/CH 用该市场自己的最后一个交易日（假期不同），
        # 其余用美股参考日（市场假期过滤）。
        auto_syms: List[tuple] = []
        try:
            conn = self._get_conn()
            try:
                code_q = ",".join("?" * len(fund_codes))
                row = conn.execute(
                    "SELECT MAX(report_period) FROM fund_report_holdings "
                    "WHERE fund_code IN (%s)" % code_q, tuple(fund_codes)).fetchone()
                latest_period = row[0] if row else None
                if latest_period:
                    syms = [r[0] for r in conn.execute(
                        "SELECT DISTINCT symbol FROM fund_report_holdings "
                        "WHERE fund_code IN (%s) AND report_period=? AND symbol IS NOT NULL"
                        % code_q, tuple(fund_codes) + (latest_period,)).fetchall()]
                else:
                    syms = []
            finally:
                conn.close()
        except Exception as e:
            result["error"] = f"读季报篮子失败: {str(e)[:120]}"
            return result

        # 非美股命名的特殊代码（00700/0857.HK 等）跳过
        for sym in syms:
            sym_key = SYMBOL_ALIAS.get(sym, sym)
            if sym_key not in SYMBOL_MARKET and (
                    re.search(r"[.^]", sym_key) or sym_key.isdigit()
                    or re.match(r"^[a-z]{2}\d+$", sym_key)):
                continue
            if "_" in sym_key and not sym_key.islower():
                continue
            if _market_of(sym_key) in ("JP", "CH"):
                base = _last_trading_day(sym_key, ref_date)
            else:
                base = None if is_market_holiday(sym_key, ref_date) else ref_date
            if base:
                auto_syms.append((sym, base))
        auto_syms = sorted(set(auto_syms))

        # 3) 查 ARM：美股时钟 + 篮子各标的的最大有价日
        import json
        import os
        import subprocess
        import tempfile

        syms_json = json.dumps([s for s, _ in auto_syms])
        remote_script = """import sqlite3, json
syms = json.loads('__SYMS__')
c = sqlite3.connect('/home/ubuntu/arbtest/database/arb_master.db')
clock = c.execute("SELECT MAX(date) FROM usa_etf_daily_prices WHERE symbol IN ('SPY','QQQ')").fetchone()[0]
latest = c.execute("SELECT MAX(date) FROM usa_etf_daily_prices").fetchone()[0]
out = {}
if syms:
    q = ','.join('?' * len(syms))
    for s, d in c.execute("SELECT symbol, MAX(date) FROM usa_etf_daily_prices WHERE symbol IN (%s) GROUP BY symbol" % q, syms):
        out[s] = d
print(json.dumps({'clock': clock, 'latest': latest, 'syms': out}))
""".replace("__SYMS__", syms_json)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                             encoding="utf-8") as f:
                f.write(remote_script)
                tmp = f.name
            cmd = 'ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o BatchMode=yes arm "python3 -"'
            proc = subprocess.run(cmd, shell=True, stdin=open(tmp, "r", encoding="utf-8"),
                                  capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=30)
            if proc.returncode != 0:
                result["error"] = "SSH 查询 ARM 失败: %s" % (
                    (proc.stderr or proc.stdout or "").strip()[:200])
                return result
            out = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            arm = json.loads(out)
        except subprocess.TimeoutExpired:
            result["error"] = "SSH 超时（arm 不可达）"
            return result
        except Exception as e:
            result["error"] = str(e)[:200]
            return result
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

        result["arm_clock"] = arm.get("clock")
        result["arm_latest"] = arm.get("latest")
        arm_syms = arm.get("syms") or {}
        result["stale"] = bool(arm.get("clock")) and arm.get("clock") < ref_date
        result["missing_auto"] = [s for s, base in auto_syms
                                  if base and (not arm_syms.get(s) or arm_syms[s] < base)]
        return result

    def push_usa_etf_to_arm(self) -> Dict[str, Any]:
        """[AI-2026-09-24] 本机 → ARM 推送 usa_etf_daily_prices（增量 INSERT OR REPLACE）。

        [AI-2026-10-11 东哥拍板]**降级为「历史回填 / 人工补推专用」，不再是日常链路**。
          理由（第一性原理）：ARM 是主力部署（面向 `9eastray.com`，本机不保证每天开机），
          **必须自主取数**。ARM 侧通路现已完备：
            · ARM `arb-dashboard.service`（`ARB_MORNING_REFRESH_AT=08:00`）→ `--refresh-morning`
              → `_run_pipeline → step5` **自己从东京 VPS 拉 `usa_prices_*.json`**；
            · ARM `usa-etf-history.timer`（07:30）自采 VPS 未覆盖的 7 只
              （CRUD/BRNT/03175/**SLV**/OILUSA/1671/1699）—— **JP/CH 三只已入 ARM 采集表**
              （旧注释"只能由本机推"已过时，2026-09-26 起 sampler 已自动化）。

        触发场景（仅人工）：新机初始化补齐历史、或 ARM 长期离线后一次性回填。

        安全约束（第一性原理：不制造删数风险）：
        - 只 INSERT OR REPLACE，【绝不 DELETE】：ARM 行数只增不减，本机缺数也不会清空 ARM；
        - 不推送 2026 年以前的行（与本地清理口径一致）；
        - 复用 ssh arm 的 BatchMode 通道，不碰 ARM 部署、不重启 ARM 服务。
        失败就地返回 dict，绝不抛异常。
        """
        import base64
        import json
        import os
        import subprocess
        import tempfile

        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT date, symbol, price, netvalue, updated_at "
                "FROM usa_etf_daily_prices WHERE date >= ? ORDER BY symbol, date",
                ("2026-01-01",)).fetchall()
        except Exception as e:
            return {"status": "error", "message": f"读本地价格表失败: {str(e)[:200]}"}
        finally:
            conn.close()
        if not rows:
            return {"status": "ok", "updated": 0,
                    "message": "本机无 usa_etf_daily_prices 数据（2026+），无需推送"}

        payload = base64.b64encode(
            json.dumps([list(r) for r in rows]).encode("utf-8")).decode("ascii")
        remote_script = """import sqlite3, json, base64
rows = json.loads(base64.b64decode('__PAYLOAD__').decode('utf-8'))
c = sqlite3.connect('/home/ubuntu/arbtest/database/arb_master.db')
c.execute('CREATE TABLE IF NOT EXISTS usa_etf_daily_prices (date TEXT NOT NULL, symbol TEXT NOT NULL, price REAL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, netvalue REAL, PRIMARY KEY (date, symbol))')
c.executemany('INSERT OR REPLACE INTO usa_etf_daily_prices (date, symbol, price, netvalue, updated_at) VALUES (?, ?, ?, ?, ?)', rows)
c.commit()
tot = c.execute('SELECT COUNT(*), MAX(date) FROM usa_etf_daily_prices').fetchone()
print(json.dumps({'updated': len(rows), 'total': tot[0], 'max_date': tot[1]}))
""".replace("__PAYLOAD__", payload)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                             encoding="utf-8") as f:
                f.write(remote_script)
                tmp = f.name
            cmd = 'ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no -o BatchMode=yes arm "python3 -"'
            proc = subprocess.run(cmd, shell=True, stdin=open(tmp, "r", encoding="utf-8"),
                                  capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=180)
            if proc.returncode != 0:
                return {"status": "error",
                        "message": f"SSH 推送 ARM 失败: {(proc.stderr or proc.stdout or '').strip()[:300]}"}
            out = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            r = json.loads(out)
            return {"status": "ok", "updated": r.get("updated", 0),
                    "arm_total": r.get("total"), "arm_max_date": r.get("max_date"),
                    "message": (f"已推 {r.get('updated', 0)} 行到 ARM"
                                f"（ARM 现有 {r.get('total')} 行，最新 {r.get('max_date')}）")}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "SSH 超时（arm 不可达）"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:300]}
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    @staticmethod
    def _ensure_usa_etf_table(conn) -> None:
        """确保本地 usa_etf_daily_prices 表存在（与 ARM 同结构）。"""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usa_etf_daily_prices (
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                netvalue REAL,
                PRIMARY KEY (date, symbol)
            )
            """
        )

    def sync_futures_freeze_from_arm(self) -> Dict[str, Any]:
        """从 ARM 拉 futures_freeze_prices 到本地库（每天上午盘前调一次即可）。

        复用 main.py 的 ssh arm 查询通道（仅拉此小表，不 scp 全库、不碰 ARM 部署）。
        失败就地返回 dict，绝不抛异常。
        """
        import json
        import os
        import subprocess
        import tempfile
        remote_script = (
            "import sqlite3, json\n"
            "c = sqlite3.connect('/home/ubuntu/arbtest/database/arb_master.db')\n"
            "rows = c.execute(\"SELECT trade_date, point, symbol, price, src, fetched_at "
            "FROM futures_freeze_prices ORDER BY trade_date, point, symbol\").fetchall()\n"
            "print(json.dumps(rows))\n"
        )
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                f.write(remote_script)
                tmp = f.name
            cmd = 'ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o BatchMode=yes arm "python3 -"'
            proc = subprocess.run(cmd, shell=True, stdin=open(tmp, "r"),
                                  capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            if proc.returncode != 0:
                return {"status": "error",
                        "message": f"SSH 查询 ARM 失败: {(proc.stderr or proc.stdout or '').strip()[:300]}"}
            out = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            rows = json.loads(out)
            if not rows:
                return {"status": "ok", "updated": 0,
                        "message": "ARM 无 futures_freeze_prices 数据"}

            conn = self._get_conn()
            try:
                self._ensure_freeze_table(conn)
                # ARM 已只保留最新一个 trade_date；本地同步前清空旧数据，保持一致
                conn.execute("DELETE FROM futures_freeze_prices")
                n = 0
                for r in rows:
                    conn.execute(
                        "INSERT INTO futures_freeze_prices "
                        "(trade_date, point, symbol, price, src, fetched_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        tuple(r),
                    )
                    n += 1
                conn.commit()
            finally:
                conn.close()
            return {"status": "ok", "updated": n, "message": f"同步 {n} 条"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "SSH 超时（arm 不可达）"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:300]}
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass


    def pull_oil_static_from_arm(self, fund_codes=("160723", "161129", "501018"),
                                   recent_days: int = 400) -> Dict[str, Any]:
        """[AI-2026-09-23 B方案] 从 ARM 拉取原油三基金的 holding_static_val 回本地库。

        本地 HoldingAnalysis 表格列 + 弹窗仍读本地 holding_static_val；B 方案下该值由 ARM 自算
        （daily_updater 的 _step_oil_recalc），本地不再本地计算，每日从 ARM(自算权威) 拉回即可。
        失败就地返回 dict，绝不抛异常。
        """
        import json
        import subprocess
        remote = (
            'import sqlite3, json\n'
            'con = sqlite3.connect("/home/ubuntu/arbtest/database/arb_master.db")\n'
            # [AI-2026-10-08 东哥拍板] 去掉 AND holding_static_val IS NOT NULL：
            # ARM 端「非假期缺价」被拦的行会由"有值"变 NULL，若只拉非空值，
            # 本机会永久保留那个前填假值，与 ARM 权威源分叉（实证 10-06 160723）。
            # 拉全量（含 NULL），upsert 时写回 NULL 即完成对齐；ARM 未剪枝的老行不受影响。
            'rows = con.execute("SELECT fund_code, date, holding_static_val FROM unified_fund_history WHERE fund_code IN (\'160723\',\'161129\',\'501018\')").fetchall()\n'
            'print(json.dumps(rows))\n'
        )
        try:
            cmd = 'ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no -o BatchMode=yes arm "python3 -"'
            # [AI-2026-09-26] 子进程超时 20s < 前端 axios 30s 上限，确保失败先于客户端超时返回 JSON
            # （超时返回 {"status":"error","message":"SSH 超时..."}），不再让连接被拖到中止 → 杜绝 "Network Error"。
            proc = subprocess.run(cmd, shell=True, input=remote, capture_output=True, encoding="utf-8", errors="replace", timeout=20)
            if proc.returncode != 0:
                return {"status": "error",
                        "message": f"SSH 拉取 ARM 失败: {(proc.stderr or '').strip()[:300]}"}
            out = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            rows = json.loads(out)
            if not rows:
                return {"status": "ok", "updated": 0, "message": "ARM 无 holding_static_val 数据"}
            conn = self._get_conn()
            try:
                n = 0
                for fc, d, v in rows:
                    # [AI-2026-09-26] UPDATE-only 改 upsert：本地无该日行（如 9-25 新估值日，
                    # 本地从未生成占位行）时 UPDATE 匹配 0 行导致拉回丢失；upsert 直接补行。
                    # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 holding_static_val 是谁写的"（ARM拉回）
                    cur = conn.execute(
                        "INSERT INTO unified_fund_history (date, fund_code, holding_static_val, writer) "
                        "VALUES (?, ?, ?, 'lofarb:holding_service.pull_from_arm') ON CONFLICT(date, fund_code) "
                        "DO UPDATE SET holding_static_val=excluded.holding_static_val, writer=excluded.writer",
                        (d, fc, v))
                    n += cur.rowcount if cur.rowcount > 0 else 0
                conn.commit()
            finally:
                conn.close()
            return {"status": "ok", "updated": n,
                    "message": f"已从 ARM 拉取 {len(rows)} 行，本地更新 {n} 行"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "SSH 超时（arm 不可达）"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:300]}

    def sync_oil_to_arm(self) -> Dict[str, Any]:
        """daily_updater 流水线尾部挂接入口：[AI-2026-09-23 B方案]
        ① 季报持仓 本机→ARM（sync_report_holdings_to_arm，季度自动覆盖）；
        ② holding_static_val ARM自算→本地拉回（pull_oil_static_from_arm，日更）。
        本地不再本地计算 holding_static_val。"""
        r1 = self.sync_report_holdings_to_arm()
        r2 = self.pull_oil_static_from_arm()
        return {
            "status": "ok" if (r1.get("status") == "ok" and r2.get("status") == "ok") else "error",
            "report_holdings": r1,
            "holding_static": r2,
        }

    def sync_report_holdings_to_arm(self, fund_codes=("160723", "161129", "501018")) -> Dict[str, Any]:
        """本地解析季报后，把指定基金的 fund_report_holdings 行自动同步到 ARM（东哥 2026-09-23 同意）。

        反向于 sync_futures_freeze_from_arm：本机读 → SSH 推 ARM 库 upsert。
        复用 main.py 的 ssh arm 通道（BatchMode，不碰 ARM 部署）；失败就地返回 dict，绝不抛异常。
        仅同步给定基金（当前原油三基金），不做整表同步，守住"按需同步"边界。
        ARM 端用 INSERT OR REPLACE（按唯一键 fund_code+report_period+sort_order+is_stock），
        季度更新自动覆盖旧报告期，无需手动清理。
        """
        import base64
        import json
        import os
        import subprocess
        import tempfile
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT fund_code, report_period, report_date, symbol, name, name_en, "
                "region, currency, type, operation_mode, manager, weight, market_value, "
                "is_stock, sort_order "
                "FROM fund_report_holdings WHERE fund_code IN (%s)"
                % ",".join("?" * len(fund_codes)),
                tuple(fund_codes)).fetchall()
        finally:
            conn.close()
        if not rows:
            return {"status": "ok", "updated": 0,
                    "message": "本机无这三只基金的季报持仓，无需同步"}

        payload = base64.b64encode(
            json.dumps([list(r) for r in rows]).encode("utf-8")).decode("ascii")
        remote_script = """import sqlite3, json, base64
data = base64.b64decode('%s').decode('utf-8')
rows = json.loads(data)
ddl = 'CREATE TABLE IF NOT EXISTS fund_report_holdings (id INTEGER PRIMARY KEY AUTOINCREMENT, fund_code TEXT NOT NULL, report_period TEXT NOT NULL, report_date TEXT NOT NULL, symbol TEXT, name TEXT NOT NULL, name_en TEXT, region TEXT, currency TEXT, type TEXT, operation_mode TEXT, manager TEXT, weight REAL NOT NULL, market_value REAL, is_stock INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0, UNIQUE(fund_code, report_period, sort_order, is_stock))'
c = sqlite3.connect('/home/ubuntu/arbtest/database/arb_master.db')
c.execute(ddl)
n = 0
for r in rows:
    c.execute('INSERT OR REPLACE INTO fund_report_holdings (fund_code, report_period, report_date, symbol, name, name_en, region, currency, type, operation_mode, manager, weight, market_value, is_stock, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', tuple(r))
    n += 1
c.commit()
print(json.dumps({'updated': n}))
""" % payload
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
                f.write(remote_script)
                tmp = f.name
            cmd = 'ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o BatchMode=yes arm "python3 -"'
            proc = subprocess.run(cmd, shell=True, stdin=open(tmp, "r"),
                                  capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            if proc.returncode != 0:
                return {"status": "error",
                        "message": f"SSH 推送 ARM 失败: {(proc.stderr or proc.stdout or '').strip()[:300]}"}
            out = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            result = json.loads(out)
            return {"status": "ok", "updated": result.get("updated", 0),
                    "message": f"已同步 {result.get('updated', 0)} 行到 ARM"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "SSH 超时（arm 不可达）"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:300]}
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass


def _rt_err(fund_code: str, code: str, **extra: Any) -> Dict[str, Any]:
    """Model B 统一错误返回。"""
    msg_map = {
        "no_report_holdings": "无季报持仓数据",
        "no_base_date": "无可用基准日期（今天之前无净值）",
        "no_base_nav": "基准日无净值",
        "no_period": "基准日无法路由报告期",
        "freeze_incomplete": "CL 三时点冻结价缺失",
        "cl_fetch_failed": "CL 实时价抓取失败",
        "fx_fetch_failed": "美元人民币实时价抓取失败",
    }
    return {
        "fund_code": fund_code,
        "status": "error",
        "code": code,
        "message": msg_map.get(code, code),
        **extra,
    }
