# -*- coding: utf-8 -*-
"""[AI-2026-07-29] 标的->数据源路由。单一真相源=lof_config.yaml 的 symbol_sources。
取代 symbol_source_map.py：路由规则全部在 YAML 声明，未知 symbol 显式报错，不再静默 auto_classify。"""
import os, yaml

_CFG = os.path.join(os.path.dirname(__file__), 'lof_config.yaml')
with open(_CFG, encoding='utf-8') as f:
    _cfg = yaml.safe_load(f) or {}

SYMBOL_SOURCE_MAP = dict(_cfg.get('symbol_sources', {}))
IB_CORE_ARBITRAGE_SYMBOLS = list(_cfg.get('ib_core_symbols', []))

SOURCE_SYMBOL_MAP = {s: [] for s in ['IB', 'IB_CORE_ONLY', 'FUTU', 'TDX', 'QMT_YH', 'QMT_GJ', 'SINA', 'WOODY', 'TENCENT']}
SOURCE_SYMBOL_MAP['IB_CORE_ONLY'] = list(IB_CORE_ARBITRAGE_SYMBOLS)
for _k, _v in SYMBOL_SOURCE_MAP.items():
    if _v == 'IB':
        SOURCE_SYMBOL_MAP['IB' if _k in IB_CORE_ARBITRAGE_SYMBOLS else 'FUTU'].append(_k)
    elif _v in SOURCE_SYMBOL_MAP:
        SOURCE_SYMBOL_MAP[_v].append(_k)
for _s in SOURCE_SYMBOL_MAP:
    SOURCE_SYMBOL_MAP[_s] = sorted(set(SOURCE_SYMBOL_MAP[_s]), key=lambda x: str(x))

US_ETF_MAP = {k: v for k, v in SYMBOL_SOURCE_MAP.items() if v == 'IB'}


def get_symbol_source(symbol, use_ib=True):
    s = symbol.upper().strip()
    if s.startswith(('SH', 'SZ')):  # [AI-2026-07-29] 兼容 A 股 SH/SZ 前缀代码
        s = s[2:]
    base = s.lstrip('^')
    for suf in ('-EU', '-JP', '-HK'):
        if base.endswith(suf):
            base = base[:-len(suf)]
            break
    for cand in (s, s.split('.')[0] if '.' in s else None, base):
        if cand and cand in SYMBOL_SOURCE_MAP:
            src = SYMBOL_SOURCE_MAP[cand]
            if src == 'IB' and (not use_ib and cand in US_ETF_MAP or (base not in IB_CORE_ARBITRAGE_SYMBOLS and cand not in IB_CORE_ARBITRAGE_SYMBOLS)):
                return 'FUTU'
            return src
    # [AI-2026-10-12 东哥拍板] 6位纯数字 = A股上市 LOF/ETF 代码（权威源=DB unified_fund_list，
    # YAML symbol_sources 只是路由加速表，不是准入名单）。未声明时不再抛 KeyError，直接路由
    # TDX —— market_data_service 对 TDX 落 RealtimeMarketManager 回退链（tdx→guojin→galaxy→sina→tencent），
    # historical/manager 对 TDX 走 sina 历史，均为正确 A 股通道。曾因 159513/513650/159660 等
    # 新增基金未同步 YAML 导致主看板每轮刷新刷屏 KeyError（fund_service 1858 捕获）。
    # 非6位数字的未知 symbol（znb_*/HSCHK25 等合成/指数符号）仍显式报错，保留防静默错配设计。
    if base.isdigit() and len(base) == 6:
        return 'TDX'
    raise KeyError(f"symbol '{s}' 未在 lof_config.yaml 的 symbol_sources 声明，无法路由。请先在 YAML 添加该 symbol 的数据源。")


def get_symbols_by_source(source):
    return SOURCE_SYMBOL_MAP.get(source, [])


def get_us_stock_source(use_ib=True):
    return 'IB' if use_ib else 'FUTU'


def get_cn_stock_source(qmt_type=None):
    return 'QMT_YH' if qmt_type == 'YH' else 'QMT_GJ' if qmt_type == 'GJ' else 'TDX'


def add_custom_mapping(symbol, source):
    SYMBOL_SOURCE_MAP[symbol.upper()] = source
    if source in SOURCE_SYMBOL_MAP and symbol not in SOURCE_SYMBOL_MAP[source]:
        SOURCE_SYMBOL_MAP[source].append(symbol)
        SOURCE_SYMBOL_MAP[source].sort()
