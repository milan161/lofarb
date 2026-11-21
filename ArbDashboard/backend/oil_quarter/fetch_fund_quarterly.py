#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原油穿透三只 QDII-LOF 季报持仓更新一体化脚本。

功能：
  1. 自动检测东财 JJGG 公告列表 → 发现最新季报
  2. 构造 PDF URL → 尝试脚本下载（失败则提示手动存取路径）
  3. 调用 parse_jsfund_pdf 解析基金/股票投资明细
  4. 写入 fund_report_holdings（先删后插，保证幂等）
  5. 可选同步到 ARM

数据来源：
  东财公告列表 API: https://api.fund.eastmoney.com/f10/JJGG
  PDF 直链: https://pdf.dfcfw.com/pdf/H2_{AN_ID}_1.pdf

用法示例：
    # 自动检测三只基金最新未入库季报并处理
    python src/ArbDashboard/backend/oil_quarter/fetch_fund_quarterly.py --update-all

    # 单只基金指定 AN ID
    python src/ArbDashboard/backend/oil_quarter/fetch_fund_quarterly.py --code 160723 --an-id AN202610261830928805

    # 使用已下载到本地的 PDF
    python src/ArbDashboard/backend/oil_quarter/fetch_fund_quarterly.py --code 501018 --pdf _tmp/fund_quarterly_pdfs/501018/2026Q3.pdf --period 2026Q3

    # dry-run 预览不入库
    python src/ArbDashboard/backend/oil_quarter/fetch_fund_quarterly.py --code 160723 --an-id AN202610261830928805 --dry-run

    # 跳过 ARM 同步（本地调试）
    python src/ArbDashboard/backend/oil_quarter/fetch_fund_quarterly.py --update-all --no-arm-sync

    # 列出各基金已有报告期
    python src/ArbDashboard/backend/oil_quarter/fetch_fund_quarterly.py --list-periods
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
import ssl
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# ─── stdout UTF-8（Windows 终端默认 GBK） ────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ─── 模块与项目路径 ──────────────────────────────────────────────────────────
# 本模块位于 src/ArbDashboard/backend/oil_quarter/，是原油穿透"持仓侧"季报数据管道，
# 与 holding_valuation/（价格侧）同为 backend 下的正式管道目录，区别于仓库根的 scripts/（临时工具）。
MODULE_DIR = Path(__file__).resolve().parent


def _find_project_root(start: Path) -> Path:
    """向上找到同时含 src/ 与 _tmp/ 的目录（即仓库根 LOFarb/）。
    找不到时按已知层级回退：oil_quarter->backend->ArbDashboard->src->LOFarb（parents[3]）。"""
    for p in [start, *start.parents]:
        if (p / "src").is_dir() and (p / "_tmp").is_dir():
            return p
    return start.parents[3]


PROJECT_ROOT = _find_project_root(MODULE_DIR)
TMP_DIR = PROJECT_ROOT / "_tmp"                      # 下载 PDF 等运行时临时区
CONFIG_PATH = MODULE_DIR / "fund_quarterly_config.json"   # 配置随模块走
DB_PATH_ENV = os.environ.get(
    "ARB_MASTER_DB", str(PROJECT_ROOT.parent / "_shared" / "arb_master.db")
)

# ─── 报告期 → 期末日映射 ────────────────────────────────────────────────────
PERIOD_END_DATES = {
    "Q1": "-03-31",
    "Q2": "-06-30",
    "Q3": "-09-30",
    "Q4": "-12-31",
    "H1": "-06-30",
    "H2": "-12-31",
}

# ─── HTTP 工具 ────────────────────────────────────────────────────────────────
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")


def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read()


def build_pdf_url(an_id: str) -> str:
    return f"https://pdf.dfcfw.com/pdf/H2_{an_id}_1.pdf"


# ─── 东财 JJGG 公告检测 ─────────────────────────────────────────────────────
def detect_announcements(fund_code: str) -> List[Dict[str, str]]:
    """调用东财 JJGG API，返回季报公告列表 [{title, an_id, date}, ...]。
    响应 body 是 UTF-8 JSONP，需要去掉 callback 包装。
    """
    url = (f"https://api.fund.eastmoney.com/f10/JJGG?"
           f"fundcode={fund_code}&type=3&pageIndex=1&pageSize=30")
    raw = http_get(url, headers={"Referer": "https://fundf10.eastmoney.com/"})
    text = raw.decode("utf-8", errors="replace")

    # JSONP 剥离: callback({Data:[...]});
    m = re.search(r'\((\{.*\})\)', text, re.DOTALL)
    if m:
        text = m.group(1)
    # 也可能是纯 JSON
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # 尝试从 body 提取
        obj = json.loads(text[text.index('{'):text.rindex('}') + 1])

    items = obj.get("Data") or []
    results = []
    for item in items:
        title = item.get("TITLE", "")
        an_id = item.get("ID", "")
        pub_date = item.get("DATE", "")[:10] if item.get("DATE") else ""
        results.append({"title": title, "an_id": an_id, "date": pub_date})
    return results


def parse_period_from_title(title: str) -> Optional[str]:
    """从公告标题提取报告期。
    '嘉实原油证券投资基金(QDII-LOF)2026年第3季度报告' → '2026Q3'
    同时过滤半年报/年报（只要 Q1~Q4）。
    """
    # 匹配: 2026年第二季度报告 / 2026年第3季度报告 / 2026年一季度报告
    m = re.search(r'(20\d{2})\s*年\s*第?\s*([一二三四1-4])\s*季度\s*报', title)
    if not m:
        # 半年报/年报 → 不入库（持仓数据与 Q2/Q4 重复）
        if re.search(r'(半年度|年度)\s*报', title):
            return None
        return None
    year = m.group(1)
    q_map = {"一": "Q1", "二": "Q2", "三": "Q3", "四": "Q4",
             "1": "Q1", "2": "Q2", "3": "Q3", "4": "Q4"}
    q = q_map.get(m.group(2))
    return f"{year}{q}" if q else None


def period_to_report_date(period: str) -> Optional[str]:
    """报告期 → 期末日 YYYY-MM-DD（非发布日期）。"""
    if len(period) < 6:
        return None
    year = period[:4]
    suffix = period[4:]  # Q1/Q2/Q3/Q4/H1/H2
    date_str = PERIOD_END_DATES.get(suffix)
    return year + date_str if date_str else None


# ─── 数据库工具 ──────────────────────────────────────────────────────────────
def get_reported_periods_from_db(fund_code: str, db_path: str = DB_PATH_ENV) -> List[str]:
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.execute(
            "SELECT DISTINCT report_period FROM fund_report_holdings "
            "WHERE fund_code=? AND report_period GLOB '[0-9]*Q[1-4]' "
            "ORDER BY report_period DESC",
            (fund_code,)
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ─── 解析模块加载 ────────────────────────────────────────────────────────────
def load_parse_module():
    """导入同目录 parse_jsfund_pdf 并初始化 symbol map。"""
    sys.path.insert(0, str(MODULE_DIR))
    try:
        import parse_jsfund_pdf as pp
        pp._NAME_META = pp._load_symbol_map()
        if not pp._NAME_META:
            print("  ⚠ holding_symbol_map.json 为空或缺失，symbol 将为 null！")
        return pp
    finally:
        pass  # 保持模块缓存


def assert_no_null_symbols(holdings: List[Dict], fund_code: str, period: str) -> List[str]:
    """入库前检查是否有 symbol=None 的条目，返回未映射的名称列表。
    空列表 = 全部 symbol 已映射。
    """
    unmapped = []
    for h in holdings:
        if h.get("is_stock") == 1:
            continue  # 股票 symbol 从代码提取，不走 map
        if not h.get("symbol"):
            unmapped.append(h["name"])
    return unmapped


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def auto_resolve_from_db(name: str, name_en: str, db_path: str) -> Optional[Dict[str, Any]]:
    """半自动补映射：对未映射的基金腿，复用 fund_report_holdings 历史里**已人工确认过**的
    name→symbol 配对，自动填 symbol/region/currency/type。

    保守规则（宁可停也绝不猜错）：
      1) 规范化(去空格+小写)后与历史 name/name_en【完全相等】优先；无完全命中再退【双向子串】；
      2) 同一批候选若指向多个不同 symbol ⇒ 歧义，放弃自动补；
      3) 命中 symbol 必须在 usa_etf_daily_prices 有价格覆盖，否则判为真·新品种，放弃。
    命中返回 {symbol, region, currency, type}，否则 None。
    """
    from collections import Counter
    t, t_en = _norm_name(name), _norm_name(name_en)
    if not t and not t_en:
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            hist = conn.execute(
                "SELECT name, name_en, symbol, region, currency, type "
                "FROM fund_report_holdings WHERE is_stock=0 AND symbol IS NOT NULL").fetchall()
            priced = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM usa_etf_daily_prices")}
        finally:
            conn.close()
    except Exception:
        return None

    exact: List[tuple] = []
    substr: List[tuple] = []
    for (hn, hne, sym, region, currency, _type) in hist:
        if not sym:
            continue
        meta = (sym, region, currency, _type)
        for cand in (hn, hne):
            cn = _norm_name(cand)
            if not cn:
                continue
            if cn == t or (t_en and cn == t_en):
                exact.append(meta)
                break
            # 子串兜底仅对足够长的名称启用，避免 "SPDR"/"ETF" 等短通用词误配
            if len(cn) >= 12 and len(t) >= 12 and (cn in t or t in cn):
                substr.append(meta)
                break
            if t_en and len(cn) >= 12 and len(t_en) >= 12 and (cn in t_en or t_en in cn):
                substr.append(meta)
                break

    def pick(cands):
        if not cands or len({c[0] for c in cands}) != 1:   # 空 或 歧义 → 放弃
            return None
        return Counter(cands).most_common(1)[0][0]

    chosen = pick(exact) or pick(substr)
    if not chosen:
        return None
    sym, region, currency, _type = chosen
    if sym not in priced:                                   # 无价格覆盖 → 真新品种
        return None
    return {"symbol": sym, "region": region, "currency": currency, "type": _type}


def persist_to_symbol_map(entries: List[Dict[str, Any]]) -> int:
    """把自动补出的映射按披露名写回 holding_symbol_map.json 的 mappings，
    使其成为正式条目（下季度直接命中）。返回新增条数。"""
    map_path = MODULE_DIR / "holding_symbol_map.json"
    try:
        with open(map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mappings = data.setdefault("mappings", {})
        added = 0
        for e in entries:
            key = e.get("name")
            if key and key not in mappings:
                mappings[key] = {"symbol": e["symbol"], "region": e.get("region"),
                                 "currency": e.get("currency"), "type": e.get("type")}
                added += 1
        if added:
            with open(map_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return added
    except Exception as ex:
        print(f"  ⚠ 写回 holding_symbol_map.json 失败(忽略): {ex}")
        return 0


# ─── PDF 下载 ────────────────────────────────────────────────────────────────
def try_download_pdf(an_id: str, target_path: Path) -> bool:
    """尝试 curl 下载 PDF，成功返回 True。东财 PDF 有 JS 反爬，大概率失败。"""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_url = build_pdf_url(an_id)
    tmp = target_path.with_suffix(".tmp")
    try:
        result = subprocess.run(
            ["curl.exe", "-sL", "-o", str(tmp), "--max-time", "30",
             "-H", f"User-Agent: {UA}",
             "-H", "Referer: https://fund.eastmoney.com/",
             pdf_url],
            capture_output=True, timeout=35
        )
        if result.returncode == 0 and tmp.exists() and tmp.stat().st_size > 10240:
            # 验证不是 HTML 反爬页
            header = tmp.read_bytes()[:5]
            if header == b'%PDF-':
                tmp.rename(target_path)
                return True
    except Exception:
        pass
    return False


# ─── ARM 同步 ────────────────────────────────────────────────────────────────
def sync_to_arm(fund_codes: List[str], db_path: str = DB_PATH_ENV) -> None:
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        code_q = ",".join("?" * len(fund_codes))
        rows = conn.execute(
            "SELECT fund_code, report_period, report_date, symbol, name, name_en, "
            "region, currency, type, operation_mode, manager, weight, market_value, "
            "is_stock, sort_order "
            "FROM fund_report_holdings WHERE fund_code IN (%s)" % code_q,
            tuple(fund_codes)).fetchall()
    finally:
        conn.close()
    if not rows:
        return

    payload = base64.b64encode(
        json.dumps([list(r) for r in rows]).encode("utf-8")).decode("ascii")
    remote_script = f"""\
import sqlite3,json,base64
data=base64.b64decode('{payload}').decode('utf-8')
rows=json.loads(data)
ddl='''CREATE TABLE IF NOT EXISTS fund_report_holdings \
(id INTEGER PRIMARY KEY AUTOINCREMENT, fund_code TEXT NOT NULL, \
report_period TEXT NOT NULL, report_date TEXT NOT NULL, symbol TEXT, \
name TEXT NOT NULL, name_en TEXT, region TEXT, currency TEXT, type TEXT, \
operation_mode TEXT, manager TEXT, weight REAL NOT NULL, market_value REAL, \
is_stock INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0, \
UNIQUE(fund_code, report_period, sort_order, is_stock))'''
c=sqlite3.connect('/home/ubuntu/arbtest/database/arb_master.db')
c.execute(ddl)
for r in rows:
    c.execute('INSERT OR REPLACE INTO fund_report_holdings '\
        '(fund_code,report_period,report_date,symbol,name,name_en,'\
        'region,currency,type,operation_mode,manager,weight,'\
        'market_value,is_stock,sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',tuple(r))
c.commit()
print('ARM updated: '+str(len(rows))+' rows')
"""

    tmp_file = TMP_DIR / "_arm_sync_tmp.py"
    tmp_file.write_text(remote_script, encoding="utf-8")
    try:
        proc = subprocess.run(
            f'ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o BatchMode=yes '
            f'arm "python3 -"',
            shell=True, stdin=open(tmp_file, "r", encoding="utf-8"),
            capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        if proc.returncode == 0:
            print(f"  ✅ ARM 同步完成: {proc.stdout.strip()}")
        else:
            print(f"  ⚠ ARM SSH 失败: {(proc.stderr or proc.stdout or '').strip()[:200]}")
    except Exception as e:
        print(f"  ⚠ ARM 同步异常: {e}")
    finally:
        tmp_file.unlink(missing_ok=True)


# ─── 主处理流程 ─────────────────────────────────────────────────────────────
def process_fund(
    config: dict, fund_code: str,
    pdf_path: str | None = None,
    an_id: str | None = None,
    period: str | None = None,
    db_path: str = DB_PATH_ENV,
    dry_run: bool = False,
    no_arm_sync: bool = False,
) -> Dict[str, Any]:
    """对单只基金执行完整流程。返回 {status, message, period, count}"""
    fund_cfg = next((f for f in config["funds"] if f["fund_code"] == fund_code), None)
    if not fund_cfg:
        return {"status": "error", "message": f"基金 {fund_code} 不在配置中"}

    fund_name = fund_cfg.get("fund_name", fund_code)
    download_dir = TMP_DIR / "fund_quarterly_pdfs" / fund_code

    # ── 1. 确定 PDF 来源 ──
    pdf_file: Optional[Path] = None

    if pdf_path:
        pdf_file = Path(pdf_path)
        if not pdf_file.exists():
            return {"status": "error", "message": f"PDF 文件不存在: {pdf_path}"}
        print(f"  📄 本地 PDF: {pdf_file}")
    elif an_id:
        # 尝试自动下载
        target = download_dir / f"{period or 'latest'}.pdf"
        if target.exists():
            pdf_file = target
            print(f"  📄 使用已缓存 PDF: {target}")
        elif try_download_pdf(an_id, target):
            pdf_file = target
            print(f"  ✅ PDF 已下载: {target} ({target.stat().st_size // 1024} KB)")
        else:
            return {
                "status": "need_pdf",
                "an_id": an_id,
                "pdf_url": build_pdf_url(an_id),
                "save_to": str(target),
                "message": (
                    f"东财 PDF 反爬无法直连下载。\n"
                    f"  请用浏览器/内置抓取打开: {build_pdf_url(an_id)}\n"
                    f"  保存到: {target}"
                ),
            }
    else:
        # 自动检测模式：查最新未入库季报
        return _auto_detect_and_report(config, fund_code, db_path)

    # ── 2. 确定报告期 ──
    if not period:
        period = _infer_period_from_pdf(pdf_file, fund_code)
    if not period:
        return {"status": "error", "message": "无法确定报告期，请加 --period 参数"}

    # 检查是否已在库
    already = get_reported_periods_from_db(fund_code, db_path)
    if period in already:
        return {"status": "ok", "message": f"{fund_code} {period} 已在库，跳过",
                "period": period, "skipped": True}

    report_date = period_to_report_date(period)
    if not report_date:
        return {"status": "error", "message": f"无法从 {period} 推算期末日"}

    # ── 3. 解析 PDF ──
    pp = load_parse_module()
    try:
        result = pp.parse_pdf(str(pdf_file))
    except Exception as e:
        return {"status": "error", "message": f"PDF 解析失败: {e}"}

    holdings = result.get("fund", []) + result.get("stock", [])
    if not holdings:
        return {"status": "warn", "message": "解析结果为空，检查 PDF 格式"}

    # ── 4. 校验 symbol + 半自动补映射 ──
    auto_resolved: List[Dict[str, Any]] = []
    still_unmapped: List[str] = []
    for h in holdings:
        if h.get("is_stock") == 1 or h.get("symbol"):
            continue
        meta = auto_resolve_from_db(h.get("name"), h.get("name_en"), db_path)
        if meta:
            h.update(meta)
            h["_auto_resolved"] = True
            auto_resolved.append({"name": h["name"], **meta})
        else:
            still_unmapped.append(h["name"])

    if auto_resolved:
        print(f"\n  🔧 半自动补映射 {len(auto_resolved)} 条（复用历史配对 + 价格覆盖校验）:")
        for e in auto_resolved:
            print(f"      {e['symbol']:<8} ← {e['name'][:45]}")

    if still_unmapped:
        print(f"\n  ⚠ 以下名称无映射且历史/价源也匹配不到（真·新品种，需人工/AI 补映射并确认价源）:")
        for name in still_unmapped:
            print(f"      - {name}")
        print(f"  → 请更新 holding_symbol_map.json 后重试，否则下游估值会剔除这些腿。\n")
        if not dry_run:
            return {"status": "error",
                    "message": f"{len(still_unmapped)} 条 symbol=null（无法半自动补全），中止入库。",
                    "unmapped": still_unmapped,
                    "auto_resolved": auto_resolved}

    # 自动补出的映射写回 map 文件，下季度直接命中
    if auto_resolved and not dry_run:
        n = persist_to_symbol_map(auto_resolved)
        if n:
            print(f"  📝 已把 {n} 条自动映射写回 holding_symbol_map.json")

    # ── 5. 打印 & 入库 ──
    fund_only = [h for h in holdings if not h.get("is_stock")]
    total_weight = sum(h["weight"] for h in fund_only if h["weight"])
    print(f"\n{'='*60}")
    print(f"  [{fund_code}] {fund_name} | {period} | report_date={report_date}")
    print(f"  持仓 {len(fund_only)} 只ETF/ETC，权重合计 {total_weight*100:.2f}%")
    print(f"{'='*60}")
    for h in holdings:
        sym = h.get("symbol") or "???"
        w = (h["weight"] or 0) * 100
        tag = " [股]" if h.get("is_stock") else ""
        print(f"    {h['sort_order']:2d}. {sym:<8} {w:6.2f}%{tag}  {h['name'][:45]}")

    if dry_run:
        print("\n  🔒 dry-run，未写入数据库")
        return {"status": "ok", "period": period, "count": len(holdings),
                "dry_run": True, "total_weight_pct": round(total_weight * 100, 2),
                "auto_resolved": auto_resolved}

    # 先删后插（幂等）
    save_db(db_path, fund_code, period, report_date, holdings)
    print(f"\n  ✅ 已写入 {db_path}")
    print(f"     fund_report_holdings: {fund_code}/{period} × {len(holdings)} rows")

    # ARM 同步
    if not no_arm_sync:
        sync_to_arm([fund_code], db_path)

    return {"status": "ok", "period": period, "count": len(holdings),
            "total_weight_pct": round(total_weight * 100, 2),
            "auto_resolved": auto_resolved}


def _infer_period_from_pdf(pdf_file: Path, fund_code: str) -> Optional[str]:
    """尝试从 PDF 文件名推断期号（如 2026Q3.pdf），否则解析首页标题。"""
    # 文件名模式
    m = re.search(r'(20\d{2}[QH][1-4])', pdf_file.stem)
    if m:
        return m.group(1)
    # 从 PDF 内容首页提取
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_file)) as pdf:
            first_text = (pdf.pages[0].extract_text() or "")[:500]
        m2 = re.search(r'(20\d{2}).*?第?\s*([一二三四1-4])\s*季度', first_text)
        if m2:
            q_map = {"一": "Q1", "二": "Q2", "三": "Q3", "四": "Q4",
                     "1": "Q1", "2": "Q2", "3": "Q3", "4": "Q4"}
            return m2.group(1) + q_map.get(m2.group(2), "")
    except Exception:
        pass
    return None


def _auto_detect_and_report(config: dict, fund_code: str, db_path: str) -> Dict[str, Any]:
    """自动检测：查东财 JJGG → 找最新未入库季报 → 返回需要用户做的操作。"""
    try:
        announcements = detect_announcements(fund_code)
    except Exception as e:
        return {"status": "error", "message": f"JJGG API 失败: {e}"}

    already = set(get_reported_periods_from_db(fund_code, db_path))
    pending = []
    for ann in announcements:
        period = parse_period_from_title(ann["title"])
        if period and period not in already:
            pending.append({"period": period, "an_id": ann["an_id"],
                            "pub_date": ann["date"], "title": ann["title"][:60]})

    if not pending:
        latest = sorted(already)[-1] if already else "无"
        return {"status": "up_to_date", "message": f"{fund_code} 已是最新（{latest}）"}

    # 只取最新一期
    target = pending[0]  # JJGG 按日期倒序，第一个就是最新
    print(f"  🔍 检测到 {fund_code} {target['period']} 季报（发布日 {target['pub_date']}）")
    print(f"     AN_ID={target['an_id']}")
    return {"status": "need_pdf", "period": target["period"],
            "an_id": target["an_id"],
            "pdf_url": build_pdf_url(target["an_id"]),
            "message": (
                f"  下一步：取 PDF 后运行\n"
                f"    python src/ArbDashboard/backend/oil_quarter/fetch_fund_quarterly.py "
                f"--code {fund_code} --an-id {target['an_id']} --period {target['period']}\n"
                f"  或手动下载 PDF 存到 _tmp/fund_quarterly_pdfs/{fund_code}/{target['period']}.pdf\n"
                f"  PDF URL: {build_pdf_url(target['an_id'])}"
            )}


def save_db(db_path: str, fund_code: str, report_period: str, report_date: str,
            holdings: List[Dict[str, Any]]) -> None:
    """写入 fund_report_holdings（先删同 key 旧数据再插，保证幂等）。"""
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.execute(
            "DELETE FROM fund_report_holdings WHERE fund_code=? AND report_period=?",
            (fund_code, report_period),
        )
        for h in holdings:
            conn.execute(
                "INSERT INTO fund_report_holdings "
                "(fund_code, report_period, report_date, symbol, name, name_en, "
                "region, currency, type, operation_mode, manager, weight, "
                "market_value, is_stock, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fund_code, report_period, report_date,
                 h.get("symbol"), h.get("name"), h.get("name_en"),
                 h.get("region"), h.get("currency"), h.get("type"),
                 h.get("operation_mode"), h.get("manager"), h.get("weight"),
                 h.get("market_value"), h.get("is_stock", 0), h.get("sort_order", 0)),
            )
        conn.commit()
    finally:
        conn.close()


# ─── 加载配置 ────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="原油 LOF 季报 → 解析 → 入库")
    ap.add_argument("--code", help="基金代码（如 160723）")
    ap.add_argument("--update-all", action="store_true",
                    help="自动检测全部基金最新未入库季报")
    ap.add_argument("--list-periods", action="store_true",
                    help="列出各基金已有报告期")
    ap.add_argument("--pdf", help="本地 PDF 文件路径")
    ap.add_argument("--an-id", help="东财公告 AN ID")
    ap.add_argument("--period", help="报告期（如 2026Q3）")
    ap.add_argument("--dry-run", action="store_true", help="预览不入库")
    ap.add_argument("--no-arm-sync", action="store_true", help="跳过 ARM 同步")
    ap.add_argument("--db", default=DB_PATH_ENV, help="数据库路径")
    args = ap.parse_args()
    _db = args.db

    if not Path(_db).exists():
        sys.exit(f"❌ 数据库不存在: {_db}")

    config = load_config()

    # --list-periods
    if args.list_periods:
        codes = [args.code] if args.code else [f["fund_code"] for f in config["funds"]]
        for code in codes:
            periods = get_reported_periods_from_db(code, _db)
            cfg = next((f for f in config["funds"] if f["fund_code"] == code), {})
            print(f"  {code} {cfg.get('fund_name','')}: {', '.join(periods) or '暂无'}")
        return

    # --update-all 自动检测
    if args.update_all:
        print("═" * 60)
        print("  自动检测三只原油基金最新季报")
        print("═" * 60)
        for fund_cfg in config["funds"]:
            code = fund_cfg["fund_code"]
            print(f"\n{'─'*50}")
            res = _auto_detect_and_report(config, code, _db)
            status = res["status"]
            icon = {"up_to_date": "✅", "need_pdf": "📥", "error": "❌"}.get(status, "ℹ️")
            print(f"  {icon} {code}: {res.get('message', status)}")
        return

    # 单只处理
    if args.code:
        res = process_fund(
            config, args.code,
            pdf_path=args.pdf, an_id=args.an_id, period=args.period,
            db_path=_db, dry_run=args.dry_run, no_arm_sync=args.no_arm_sync,
        )
        print(f"\n结果: {json.dumps(res, ensure_ascii=False, indent=2)}")
        sys.exit(0 if res["status"] in ("ok", "up_to_date") else 1)

    ap.print_help()


if __name__ == "__main__":
    main()
