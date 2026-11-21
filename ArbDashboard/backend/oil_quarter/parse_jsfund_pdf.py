#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析嘉实基金官网 LOF 季报 PDF，提取基金投资明细与股票投资明细。

用法示例:
    python src/ArbDashboard/backend/oil_quarter/parse_jsfund_pdf.py \
        --pdf D:\\Study\\ARB\\LOFarb\\_tmp\\fund_quarterly_pdfs\\160723\\2026Q2.pdf \
        --code 160723 --period 2026Q2 --date 2026-06-30

通用解析器：覆盖嘉实(160723)/易方达(161129)/南方(501018)三家季报 PDF 的基金投资明细与股票投资明细。
名称→symbol/region/currency 元数据从同目录 holding_symbol_map.json 加载（可 --map 覆盖）。
"""
import argparse
import json
import os
import re
import sys
import sqlite3
import urllib.request
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    import pdfplumber
except ImportError as e:
    raise SystemExit("缺少 pdfplumber，请先安装: pip install pdfplumber") from e


# ============== 通用名称映射 ==============
# 默认从脚本同目录 holding_symbol_map.json 加载，可通过 --map 覆盖。
# 用于把季报披露的基金/股票名称映射到 symbol、region、currency、type 等展示字段。
_NAME_META: Dict[str, Dict[str, Any]] = {}


def _load_symbol_map(map_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """加载名称→元数据映射表。"""
    if map_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        map_path = os.path.join(script_dir, "holding_symbol_map.json")
    if not os.path.exists(map_path):
        return {}
    try:
        with open(map_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("mappings", {})
    except Exception as e:
        print(f"[warning] 加载映射表失败: {e}", file=sys.stderr)
        return {}


def _clean_name(raw: str) -> str:
    """清理 PDF 表格中跨行产生的换行和多余空格。
    特别注意：去掉中文字符之间的空格，但保留英文单词之间的空格。
    """
    if raw is None:
        return ""
    s = str(raw)
    s = s.replace("\n", " ")
    # 去掉中文字符之间的空格（PDF 表格常把汉字间距拉大）
    s = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', s)
    s = re.sub(r"\s+", " ", s)
    s = s.replace("（", "(").replace("）", ")")
    return s.strip()


def _parse_money(value: str) -> Optional[float]:
    """解析带千分位逗号的金额。
    [AI-2026-09-03] 修复：半年报(H1) PDF 中金额单元格被 pdfplumber 在换行处拆断
    （如 '226,654,36 5.29'），仅 strip 末尾空白无法去除单元格内空格，
    导致 float() 失败返回 None、市值列空白。改为先去除所有空白（含单元格内换行/空格）。"""
    if not value:
        return None
    s = str(value)
    s = re.sub(r"\s+", "", s)        # 去单元格内换行/空格导致的数字断裂
    s = s.replace(",", "").replace("-", "")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _parse_percent(value: str) -> Optional[float]:
    """解析百分比为 0-1 之间的小数。"""
    if not value:
        return None
    s = str(value).replace("%", "").replace(",", "").replace("-", "").strip()
    try:
        return float(s) / 100.0 if s else None
    except ValueError:
        return None


def _match_name_meta(clean_name: str) -> Dict[str, Any]:
    """根据清理后的名称匹配 symbol/region/currency 等元数据。
    额外处理 PDF 表格中货币代码被空格拆开的情况，如 "Currency-U SD" -> "Currency-USD"。
    以及单词内断词如 "WisdomTr ee" -> "WisdomTree"（去除所有空格后再匹配一次）。
    """
    if not _NAME_META:
        return {}
    # 归一化：修复 "Trading Currency-U SD" / "Currency-G BP" 这类拆分
    normalized = re.sub(r'(?<=[\-A-Za-z])\s+(?=[A-Za-z]{2,3}\))', '', clean_name)
    candidates = [clean_name, normalized]
    for name_key, meta in _NAME_META.items():
        for cand in candidates:
            if name_key == cand or name_key in cand or cand in name_key:
                return meta.copy()
    # Fallback: 去除所有空格后匹配（处理 PDF 断词如 "WisdomTr ee"）
    stripped_name = clean_name.replace(" ", "")
    for name_key, meta in _NAME_META.items():
        stripped_key = name_key.replace(" ", "")
        if stripped_key in stripped_name or stripped_name in stripped_key:
            return meta.copy()
    return {}


def _is_fund_holding_row(row: List[Any]) -> bool:
    """判断一行是否是基金投资明细数据行。"""
    if len(row) < 6:
        return False
    idx, name, ftype, op_mode, manager, *rest = row
    idx_s = str(idx).strip() if idx else ""
    name_s = _clean_name(name)
    ftype_s = str(ftype).replace("\n", "").strip() if ftype else ""
    if not idx_s.isdigit() or not name_s:
        return False
    if ftype_s not in ("ETF", "ETC", "FOF", "股票"):
        return False
    # 最后两列应为金额和百分比
    money = _parse_money(rest[-2] if len(rest) >= 2 else rest[-1] if rest else None)
    pct = _parse_percent(rest[-1] if rest else None)
    if money is None and pct is None:
        return False
    return True


def _is_stock_holding_row(row: List[Any]) -> bool:
    """判断一行是否是股票投资明细数据行。"""
    if len(row) < 6:
        return False
    idx = str(row[0]).strip() if row[0] else ""
    if not idx.isdigit():
        return False
    # 股票表列数较多，且不含 ETF/ETC 类型
    joined = " ".join(str(c) for c in row if c)
    if "ETF" in joined or "ETC" in joined:
        return False
    if "HK" in joined or "香港" in joined:
        return True
    return False


def _parse_fund_row(row: List[Any]) -> Optional[Dict[str, Any]]:
    """解析基金投资明细的一行。"""
    if not _is_fund_holding_row(row):
        return None
    idx, name, ftype, op_mode, manager, *rest = row
    sort_order = int(str(idx).strip())
    clean_name = _clean_name(name)
    money = _parse_money(rest[-2] if len(rest) >= 2 else rest[-1] if rest else None)
    pct = _parse_percent(rest[-1] if rest else None)
    op_mode_s = _clean_name(op_mode) if op_mode else "交易型开放式"
    manager_s = _clean_name(manager) if manager else ""

    meta = _match_name_meta(clean_name)
    return {
        "sort_order": sort_order,
        "name": clean_name,
        "name_en": clean_name,
        "type": meta.get("type") or str(ftype).strip(),
        "operation_mode": op_mode_s,
        "manager": meta.get("manager") or manager_s,
        "market_value": money,
        "weight": pct,
        "symbol": meta.get("symbol"),
        "region": meta.get("region"),
        "currency": meta.get("currency"),
        "is_stock": 0,
    }


def _parse_stock_row(row: List[Any]) -> Optional[Dict[str, Any]]:
    """解析股票投资明细的一行。
    仅识别期末持仓表（含公允价值+占净值比），忽略买入/卖出明细表。
    """
    if not _is_stock_holding_row(row):
        return None
    # 表头: ['序号','公司名称(英文)','公司名称(中文)','证券代码','所在证券市场','','所属国家(地区)','','数量(股)','公允价值(人民币元)','占基金资产净值比例(%)']
    # 数据行长度可能因跨页合并而不一致
    idx = int(str(row[0]).strip())
    name_en = _clean_name(row[1]) if len(row) > 1 else ""
    name_cn = _clean_name(row[2]) if len(row) > 2 else ""
    symbol_raw = str(row[3]).replace("\n", "").replace(" ", "").strip() if len(row) > 3 else ""
    symbol = symbol_raw + ".HK" if symbol_raw and symbol_raw.isdigit() else symbol_raw
    region = "HK"
    money = _parse_money(row[-2] if len(row) >= 2 else None)
    pct = _parse_percent(row[-1] if len(row) >= 1 else None)
    # 没有 weight 的不是期末持仓（如期间买入/卖出表）
    if pct is None or money is None:
        return None
    name = name_cn or name_en

    meta = _match_name_meta(name_en)
    return {
        "sort_order": idx,
        "name": name,
        "name_en": name_en,
        "type": "股票",
        "operation_mode": None,
        "manager": None,
        "market_value": money,
        "weight": pct,
        "symbol": meta.get("symbol") or symbol,
        "region": meta.get("region") or region,
        "currency": meta.get("currency") or "HKD",
        "is_stock": 1,
    }


def parse_pdf(pdf_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """解析 PDF，返回 {'fund': [...], 'stock': [...]}。"""
    fund_holdings: List[Dict[str, Any]] = []
    stock_holdings: List[Dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            for table in tables:
                if not table:
                    continue
                header = table[0] if table else []
                header_str = " ".join(str(c).replace("\n", "") for c in header if c)

                # 跳过「累计买入/卖出金额」这类非期末持仓表
                if any(k in header_str for k in ["累计买入金额", "累计卖出金额", "买入金额", "卖出金额"]):
                    continue

                if "基金名称" in header_str and ("基金类型" in header_str or "基金类" in header_str):
                    # 基金投资明细表（有表头）
                    for row in table[1:]:
                        parsed = _parse_fund_row(row)
                        if parsed:
                            fund_holdings.append(parsed)
                elif "公司名称" in header_str and "证券代码" in header_str:
                    # 股票投资明细表（有表头）
                    for row in table[1:]:
                        parsed = _parse_stock_row(row)
                        if parsed:
                            stock_holdings.append(parsed)
                else:
                    # 无表头表格：尝试解析为基金持仓或股票持仓
                    for row in table:
                        p = _parse_fund_row(row)
                        if p:
                            fund_holdings.append(p)
                        else:
                            p2 = _parse_stock_row(row)
                            if p2:
                                stock_holdings.append(p2)

    # 去重并按序号排序
    def _key(h):
        return (h["sort_order"], h["name"], h.get("is_stock", 0))

    seen = set()
    fund_unique = []
    for h in sorted(fund_holdings, key=lambda x: x["sort_order"]):
        k = _key(h)
        if k not in seen:
            seen.add(k)
            fund_unique.append(h)

    stock_unique = []
    for h in sorted(stock_holdings, key=lambda x: x["sort_order"]):
        k = _key(h)
        if k not in seen:
            seen.add(k)
            stock_unique.append(h)

    return {"fund": fund_unique, "stock": stock_unique}


def download_pdf(url: str, out_dir: str) -> str:
    """下载 PDF 到本地。"""
    os.makedirs(out_dir, exist_ok=True)
    filename = os.path.basename(url) or "report.pdf"
    path = os.path.join(out_dir, filename)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        with open(path, "wb") as fh:
            fh.write(resp.read())
    return path


def save_to_db(db_path: str, fund_code: str, report_period: str, report_date: str,
               holdings: List[Dict[str, Any]]) -> None:
    """将解析结果写入 fund_report_holdings 表。"""
    from arbcore.database.managers.base import ensure_wal_once
    ensure_wal_once(db_path)
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        # 先删除旧数据
        conn.execute(
            "DELETE FROM fund_report_holdings WHERE fund_code=? AND report_period=?",
            (fund_code, report_period),
        )
        for h in holdings:
            conn.execute(
                """
                INSERT INTO fund_report_holdings (
                    fund_code, report_period, report_date, symbol, name, name_en,
                    region, currency, type, operation_mode, manager, weight,
                    market_value, is_stock, sort_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fund_code, report_period, report_date,
                    h.get("symbol"), h.get("name"), h.get("name_en"),
                    h.get("region"), h.get("currency"), h.get("type"),
                    h.get("operation_mode"), h.get("manager"), h.get("weight"),
                    h.get("market_value"), h.get("is_stock", 0), h.get("sort_order", 0),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="解析基金 LOF 季报 PDF 持仓（通用版）")
    parser.add_argument("--pdf", required=True, help="PDF 本地路径或 URL")
    parser.add_argument("--code", required=True, help="基金代码，如 160723")
    parser.add_argument("--period", required=True, help="报告期，如 2026Q2 / 2026H1")
    parser.add_argument("--date", required=True, help="报告截止日，如 2026-06-30")
    parser.add_argument("--db", help="数据库路径（默认自动解析）")
    parser.add_argument("--map", help="名称→symbol 映射表 JSON 路径（默认 本目录 holding_symbol_map.json）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写入数据库")
    parser.add_argument("--download-dir", default=os.path.join(os.path.dirname(__file__), "..", "_tmp", "jsfund_pdf"),
                        help="URL 下载临时目录")
    args = parser.parse_args()

    # 加载名称映射表（全局，供解析函数使用）
    global _NAME_META
    _NAME_META = _load_symbol_map(args.map)

    pdf_path = args.pdf
    if pdf_path.startswith("http://") or pdf_path.startswith("https://"):
        print(f"下载 PDF: {pdf_path}")
        pdf_path = download_pdf(pdf_path, args.download_dir)
        print(f"已保存到: {pdf_path}")

    if not os.path.exists(pdf_path):
        print(f"PDF 不存在: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"解析 PDF: {pdf_path}")
    result = parse_pdf(pdf_path)
    all_holdings = result["fund"] + result["stock"]

    print(f"\n基金投资明细: {len(result['fund'])} 条")
    for h in result["fund"]:
        print(f"  {h['sort_order']:2d}. {h['name']:<50} {h.get('symbol') or '-':<8} {h.get('region') or '-':<4} {h.get('currency') or '-':<4} {h['type']:<4} weight={h['weight']*100:.2f}%")

    if result["stock"]:
        print(f"\n股票投资明细: {len(result['stock'])} 条")
        for h in result["stock"]:
            print(f"  {h['sort_order']:2d}. {h['name']:<30} {h.get('symbol') or '-':<12} {h.get('region') or '-':<4} weight={h['weight']*100:.2f}%")

    if args.dry_run:
        print("\n(dry-run, 未写入数据库)")
        return

    db_path = args.db
    if not db_path:
        # 自动定位主库
        try:
            from arbcore.database.db_manager import _resolve_db_path
            db_path = _resolve_db_path()
        except Exception as e:
            print(f"自动解析数据库路径失败: {e}", file=sys.stderr)
            sys.exit(1)

    save_to_db(db_path, args.code, args.period, args.date, all_holdings)
    print(f"\n已写入数据库: {db_path} (fund_report_holdings {args.period} 共 {len(all_holdings)} 条)")


if __name__ == "__main__":
    main()
