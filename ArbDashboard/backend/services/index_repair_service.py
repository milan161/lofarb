"""
指数数据修复服务 (Index Repair Service)

提供两种方式补采 index_history 数据：
1. repair_with_tdx() - 需要通达信客户端已打开
2. repair_with_sina() - 纯 HTTP API，无需通达信（数据质量可能不如通达信）

两种方式都会在补采后自动重算所有受影响的 static_val。
"""
import os
import sys
import json
import logging
import subprocess
import requests
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

# [AI-2026-08-21] 新浪请求节流：15秒间隔防封IP
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    from arbcore.utils.sina_throttle import throttle_sina_request as _throttle_sina
except Exception:
    def _throttle_sina(): pass

logger = logging.getLogger(__name__)

# ============================================================
# 路径配置
# ============================================================
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
# [AI-2026-08-16] 活库移出仓库根（物理隔离防泄漏）
PROJECT_ROOT = os.path.normpath(os.path.join(BACKEND_DIR, '..', '..', '..'))
# [AI-2026-10-06] 搬入 src/ 后层数全变，改走 arbcore 统一解析（env ARB_MASTER_DB 优先 → _shared → database/ 上溯）
try:
    from arbcore.database.db_manager import _resolve_db_path as _resolve_master_db
    DB_PATH = _resolve_master_db()
except Exception:
    DB_PATH = os.path.join(os.path.dirname(PROJECT_ROOT), 'database', 'arb_master.db')

# 同主码表：和 backfill_tdx_index.py 保持一致
A_SHARE_SZ_PREFIX = {'399300','399001','399997','399989','399330','399441','399707',
                     '399803','399807','399809','399987','399998','399417','399011',
                     '399303','399968','399986','399363','399975','399322','399988',
                     '399990','399993','399005','399006'}
A_SHARE_SH_PREFIX = {'000905','000869','000852'}

# 港股指数
HK_INDICES = {'HSI','HSCEI','HSCCI','HSTECH','HSCI','HSSI','HSMI','HSSCNE'}

# 日本指数
JP_INDICES = {'N225', 'NKY', 'NIKKEI'}


# ============================================================
# TDX 方式
# ============================================================

def repair_with_tdx(days_back: int = 30) -> dict:
    """
    通过通达信 tqcenter 补采指数数据。
    需要通达信客户端已打开。
    
    Args:
        days_back: 补采最近多少天的数据（默认 30 天）
    
    Returns:
        {"status": "ok"|"error", "message": "...", "new_records": int}
    """
    # [AI-2026-07-01] 路径：BACKEND_DIR=backend/services/ -> scripts/ 需上一层
    script_path = os.path.normpath(os.path.join(
        BACKEND_DIR, '..', 'scripts', 'backfill_tdx_index.py'))
    
    if not os.path.exists(script_path):
        return {"status": "error", "message": f"backfill_tdx_index.py 不存在: {script_path}"}
    
    start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    
    try:
        result = subprocess.run(
            [sys.executable, script_path, '--start', start_date],
            capture_output=True, text=True, timeout=300,
            cwd=PROJECT_ROOT
        )
        output = result.stdout + result.stderr
        
        if result.returncode != 0:
            return {"status": "error", "message": f"脚本异常退出 (code={result.returncode})", "output": output[-2000:]}
        
        # 解析输出中的新增条数
        new_records = 0
        for line in output.split('\n'):
            if '完成！新增' in line:
                import re
                m = re.search(r'新增\s*(\d+)', line)
                if m:
                    new_records = int(m.group(1))
        
        # 补采成功后重算 static_val
        recalc_result = _recalc_all_static_val()
        
        return {
            "status": "ok",
            "message": f"TDX 补采完成，新增 {new_records} 条记录",
            "new_records": new_records,
            "recalc": recalc_result,
            "output": output[-1000:]
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "补采超时（>5分钟）"}
    except Exception as e:
        return {"status": "error", "message": f"补采失败: {e}"}


# ============================================================
# Sina/腾讯 API 方式（无需通达信）
# ============================================================

def _get_all_related_indices() -> List[tuple]:
    """
    从 unified_fund_list 获取所有需要补采的指数代码。
    Returns: [(symbol, category_type), ...]
        category_type: 'a_share_sz' | 'a_share_sh' | 'hk' | 'skip'
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT related_index FROM unified_fund_list 
        WHERE related_index IS NOT NULL AND related_index != '-' AND related_index != ''
    """)
    raw = [r[0] for r in c.fetchall()]
    conn.close()
    
    result = []
    seen = set()
    for code in raw:
        clean = code.strip().upper()
        if clean in seen:
            continue
        seen.add(clean)
        
        # 去 .CSI 后缀
        if clean.endswith('.CSI'):
            clean = clean[:-4]
        
        # 美股 → 跳过
        if clean in ('GLD','USO','XOP','QQQ','XLY','XBI','INDA','SOXX',
                     'AGG','VNQ','RSPH','SPY','KWEB','XLE'):
            result.append((code, 'skip'))
            continue
            
        # 6位纯数字指数
        if len(clean) == 6 and clean.isdigit():
            if clean[0] in ('3', '9'):  # 399xxx(深) 或 930xxx(中证, 新浪有) 走 SZ
                result.append((code, 'a_share_sz'))
            else:  # 0/1开头 - 沪市
                result.append((code, 'a_share_sh'))
            continue
        # A股深圳指数
        if clean in A_SHARE_SZ_PREFIX or (clean.startswith('399') and len(clean) == 6):
            result.append((code, 'a_share_sz'))
        # A股上海指数
        elif clean in A_SHARE_SH_PREFIX or (clean.startswith('000') and len(clean) == 6) or (clean.startswith('001') and len(clean) == 6):
            result.append((code, 'a_share_sh'))
        # 港股指数
        elif clean in HK_INDICES:
            result.append((code, 'hk'))
        # 带 .HI 后缀的港股指数
        elif clean.endswith('.HI'):
            result.append((code, 'hk'))
        # 美股指数
        elif clean in ('.INX', '.NDX'):
            result.append((code, 'us_index'))
        # 日本指数(N225等) — 通过新浪全球指数接口获取
        elif clean in JP_INDICES:
            result.append((code, 'jp_index'))
        else:
            result.append((code, 'skip'))
    
    return result


def _fetch_sina_a_share(symbol: str, market: str, days: int = 30) -> list:
    """
    从新浪财经获取 A 股指数日线数据。
    
    Args:
        symbol: 指数代码，如 '399300'
        market: 'sz' 或 'sh'
        days: 获取最近多少天
    
    Returns: [(date_str, close_price), ...] 按日期升序
    """
    url = (f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={market}{symbol}&scale=240&ma=no&datalen={days}")
    try:
        resp = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.sina.com.cn/'
        }, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not data or not isinstance(data, list):
            return []
        result = []
        for d in data:
            day = d.get('day', '')
            close = d.get('close', '')
            if day and close:
                try:
                    result.append((day, float(close)))
                except ValueError:
                    pass
        return sorted(result, key=lambda x: x[0])
    except Exception as e:
        logger.warning(f"[Sina] 获取 {market}{symbol} 失败: {e}")
        return []


def _fetch_qq_hk_index(symbol: str, days: int = 30) -> list:
    """
    从腾讯财经获取港股指数日线数据。
    
    Args:
        symbol: 港股指数代码，如 'HSI'
        days: 获取最近多少天
    
    Returns: [(date_str, close_price), ...] 按日期升序
    """
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"_var=kline_dayqfq&param=hk{symbol},day,,,{days},qfq")
    try:
        resp = requests.get(url, timeout=15)
        text = resp.text
        if 'kline_dayqfq=' not in text:
            return []
        json_str = text.split('kline_dayqfq=')[-1]
        data = json.loads(json_str)
        day_data = data.get('data', {}).get(f'hk{symbol}', {}).get('day', [])
        if not day_data:
            return []
        result = []
        for item in day_data:
            if len(item) >= 5:
                date_str = item[0]
                close = item[4]
                try:
                    result.append((date_str, float(close)))
                except (ValueError, IndexError):
                    pass
        return sorted(result, key=lambda x: x[0])
    except Exception as e:
        logger.warning(f"[QQ] 获取 hk{symbol} 失败({e})，试东财兜底")
        try:
            em_secid = f"116.{symbol}" if (len(symbol) == 5 and symbol.isdigit()) else f"100.{symbol}"
            em_url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={em_secid}"
                      f"&ut=fa5fd1947f08e6d3cb8d1e3a8a3c7b2e&fields1=f1f1f2&fields2=f51f52f53f54f55f56"
                      f"&klt=101&fqt=0&end=20500101&lmt={days}")
            em = requests.get(em_url, timeout=15).json()
            kl = (em.get("data") or {}).get("klines") or []
            res = []
            for row in kl:
                p = row.split(",")
                if len(p) >= 3 and p[0] and p[2]:
                    res.append((p[0], float(p[2])))
            if res:
                return sorted(res, key=lambda x: x[0])
        except Exception as e2:
            logger.error(f"东财港股 hk{symbol} 也失败: {e2}")
        return []


def _fetch_qq_a_share(symbol: str, market: str, days: int = 30) -> list:
    """
    从腾讯财经获取 A 股指数日线数据（备用，Sina 失败时使用）。
    """
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"_var=kline_dayqfq&param={market}{symbol},day,,,{days},qfq")
    try:
        resp = requests.get(url, timeout=15)
        text = resp.text
        if 'kline_dayqfq=' not in text:
            return []
        json_str = text.split('kline_dayqfq=')[-1]
        data = json.loads(json_str)
        day_data = data.get('data', {}).get(f'{market}{symbol}', {}).get('day', [])
        if not day_data:
            return []
        result = []
        for item in day_data:
            if len(item) >= 5:
                date_str = item[0]
                close = item[4]
                try:
                    result.append((date_str, float(close)))
                except (ValueError, IndexError):
                    pass
        return sorted(result, key=lambda x: x[0])
    except Exception as e:
        logger.warning(f"[QQ] 获取 {market}{symbol} 失败({e})，试新浪兜底")
        try:
            s_url = (f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                     f"CN_MarketData.getKLineData?symbol={market}{symbol}&scale=240&ma=no&datalen={days}")
            sd = requests.get(s_url, headers={"User-Agent": "Mozilla/5.0"},
                              timeout=15, proxies={"http": None, "https": None}).json()
            if isinstance(sd, list):
                res = []
                for x in sd:
                    if x.get('day') and x.get('close'):
                        res.append((x['day'], float(x['close'])))
                if res:
                    return sorted(res, key=lambda x: x[0])
        except Exception as e2:
            logger.error(f"新浪兜底 {market}{symbol} 也失败: {e2}")
        return []


def _fetch_sina_global_index(symbol: str, days: int = 30) -> list:
    """
    [AI-2026-07-20] 从新浪全球指数接口获取海外指数日线数据（日经225等）。

    尝试以下途径获取历史数据：
    1. 新浪 US_MinKService 日线 API（可能支持 N225）
    2. 新浪实时接口 int_nikkei（仅最新收盘价，备用源）

    Args:
        symbol: 指数代码，如 'N225'
        days: 获取最近多少天（仅对支持历史数据的 API 有效）

    Returns: [(date_str, close_price), ...] 按日期升序
    """
    result = []

    # 1. 尝试新浪 US_MinKService 日线 API（美股日线接口，部分海外指数也可用）
    try:
        # [AI-2026-10-12] 按指数符号选择候选：N225 用 nikkei 系，TOPIX 用 topix 系
        if 'TOPIX' in symbol.upper() or symbol.upper() == 'TPX':
            alt_symbols = ['topix', 'tpx', '^topix', 'int_topix']
        else:
            # N225 在新浪 US 接口中可能的 symbol 格式
            alt_symbols = ['n225', 'nikkei', '^n225']
        for alt_sym in alt_symbols:
            url = f"https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK?symbol={alt_sym}"
            resp = requests.get(url, headers={
                'Referer': 'https://finance.sina.com.cn/',
                'User-Agent': 'Mozilla/5.0'
            }, timeout=10)
            if resp.status_code == 200 and resp.text and resp.text != 'null':
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    for d in data:
                        date_str = d.get('d', '')
                        close = d.get('c', 0)
                        if date_str and close:
                            try:
                                result.append((date_str, float(close)))
                            except (ValueError, TypeError):
                                pass
                    if result:
                        logger.info(f"[SINA-GLOBAL] {symbol} 通过 US_MinKService({alt_sym}) 获取到 {len(result)} 条历史数据")
                        return sorted(result, key=lambda x: x[0])
    except Exception as e:
        logger.debug(f"[SINA-GLOBAL] US_MinKService 获取 {symbol} 失败: {e}")

    # 2. 备用源：使用新浪实时接口 int_nikkei 获取最新收盘价
    try:
        _throttle_sina()
        sina_code = 'int_nikkei'
        url = f"http://hq.sinajs.cn/list={sina_code}"
        resp = requests.get(url, headers={
            'Referer': 'https://finance.sina.com.cn/',
            'User-Agent': 'Mozilla/5.0'
        }, timeout=5)
        resp.encoding = 'gbk'
        if '="' in resp.text:
            parts = resp.text.split('"')[1].split(',')
            if len(parts) >= 4:
                price = float(parts[1])
                if price > 0:
                    # 实时接口返回的是当前价格，我们记录为今天的日期
                    from datetime import date
                    today_str = date.today().strftime('%Y-%m-%d')
                    result = [(today_str, price)]
                    logger.info(f"[SINA-GLOBAL] {symbol} 从 int_nikkei 获取到实时价格: {price}")
                    return result
    except Exception as e:
        logger.warning(f"[SINA-GLOBAL] int_nikkei 实时获取 {symbol} 失败: {e}")

    return []


def _get_existing_dates(symbol: str) -> set:
    """获取数据库中已有的日期"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT date FROM index_history WHERE symbol=?", (symbol,))
    dates = {r[0] for r in c.fetchall()}
    conn.close()
    return dates


def _upsert_index_history(symbol: str, date: str, close: float, source: str = 'sina'):
    """写入一条 index_history"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO index_history (symbol, date, close, source) VALUES (?, ?, ?, ?)",
        (symbol, date, close, source)
    )
    conn.commit()
    conn.close()


def _recalc_all_static_val() -> dict:
    """
    重新计算全部基金的 static_val（复用 step11 逻辑的简化版）。
    通过直接调用 repair 端点逻辑对每只基金重算。
    此处直接复用 daily_updater 的 step11 方法。
    """
    # 导入 daily_updater 并执行 step11
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, 'ArbDashboard', 'backend'))
        from scheduler.daily_updater import DailyUpdater
        
        du = DailyUpdater()
        # 只跑 step11（跟踪指数公式），不跑完整 pipeline
        du.step11_simple_static_valuation()
        return {"status": "ok", "detail": "static_val 已全部重算"}
    except Exception as e:
        logger.error(f"重算 static_val 失败: {e}")
        return {"status": "error", "message": f"重算 static_val 失败: {e}"}


def repair_with_sina(days_back: int = 30) -> dict:
    """
    使用 Sina/腾讯 API 补采指数数据（无需通达信）。
    
    流程：
    1. 遍历所有 related_index
    2. A股指数 → Sina API（优先）→ QQ API（备用源）
    3. 港股指数 → QQ API
    4. 写入 index_history
    5. 重算 static_val
    """
    indices = _get_all_related_indices()
    
    total_new = 0
    total_skip = 0
    total_fail = 0
    details = []
    
    for raw_code, category in indices:
        clean = raw_code.strip().upper()
        if clean.endswith('.CSI'):
            clean = clean[:-4]
        if clean.endswith('.HI'):
            clean = clean[:-3]
        
        if category == 'skip':
            details.append(f"  {raw_code} → 跳过（无需补采）")
            total_skip += 1
            continue
        
        existing = _get_existing_dates(clean)
        rows = []
        
        if category == 'a_share_sz':
            rows = _fetch_sina_a_share(clean, 'sz', days_back)
            if not rows:
                rows = _fetch_qq_a_share(clean, 'sz', days_back)
        elif category == 'a_share_sh':
            rows = _fetch_sina_a_share(clean, 'sh', days_back)
            if not rows:
                rows = _fetch_qq_a_share(clean, 'sh', days_back)
        elif category == 'hk':
            rows = _fetch_qq_hk_index(clean, days_back)
            if not rows:
                # 也试试不带 hk 前缀
                rows = _fetch_qq_hk_index(clean, days_back)
        elif category == 'jp_index':
            # 日本指数(N225等)通过新浪全球指数接口补采
            rows = _fetch_sina_global_index(clean, days_back)
        
        if not rows:
            details.append(f"  {raw_code} → 无数据")
            total_fail += 1
            continue
        
        inserted = 0
        skipped = 0
        for date_str, close in rows:
            if date_str in existing:
                skipped += 1
                continue
            _upsert_index_history(clean, date_str, close, 'sina')
            inserted += 1
        
        total_new += inserted
        total_skip += skipped
        if inserted > 0:
            details.append(f"  {raw_code} → 新增 {inserted} 天, 跳过 {skipped} 天")
        else:
            details.append(f"  {raw_code} → 跳过 {skipped} 天（已存在）")
    
    summary = f"Sina 补采完成：新增 {total_new} 条, 跳过 {total_skip} 条, 失败 {total_fail} 个"
    logger.info(summary)
    
    # 重算 static_val
    recalc_result = _recalc_all_static_val()
    
    return {
        "status": "ok",
        "message": summary,
        "new_records": total_new,
        "skipped": total_skip,
        "failed": total_fail,
        "details": details,
        "recalc": recalc_result
    }


def repair_fund_index_history(fund_code: str, days_back: int = 30) -> dict:
    """
    [AI-2026-08-05] 单基金指数历史补采（对应前端「核对静态估值」按钮）。

    只补该基金 related_index 的指数数据写入 index_history，不触及其他基金；
    需用时按需触发，不进每日流水线（每日自动补采已被东哥否决，见 AGENTS.md TOP 2 相关）。
    与 repair_with_sina(全量) 互补：按钮走这条，做到"单基金、按需"。
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT related_index FROM unified_fund_list WHERE fund_code=?", (fund_code,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return {"status": "skip", "fund_code": fund_code, "message": "无 related_index，跳过指数补采"}
    raw = str(row[0]).strip()
    clean = raw.upper()
    if clean.endswith('.CSI'):
        clean = clean[:-4]
    if clean.endswith('.HI'):
        clean = clean[:-3]

    # 分类（与 _get_all_related_indices 同逻辑）
    cat = None
    if clean in ('GLD', 'USO', 'XOP', 'QQQ', 'XLY', 'XBI', 'INDA', 'SOXX',
                 'AGG', 'VNQ', 'RSPH', 'SPY', 'KWEB', 'XLE'):
        cat = 'skip'
    elif len(clean) == 6 and clean.isdigit():
        cat = 'a_share_sz' if clean[0] in ('3', '9') else 'a_share_sh'
    elif clean in A_SHARE_SZ_PREFIX or (clean.startswith('399') and len(clean) == 6):
        cat = 'a_share_sz'
    elif clean in A_SHARE_SH_PREFIX or clean.startswith('000') or clean.startswith('001'):
        cat = 'a_share_sh'
    elif clean in HK_INDICES:
        cat = 'hk'
    elif clean.startswith('N225') or 'NK' in clean or 'TOPIX' in clean or clean == 'TPX':
        cat = 'jp_index'

    if cat is None or cat == 'skip':
        return {"status": "skip", "fund_code": fund_code, "symbol": raw,
                "message": f"指数 {raw} 无需/无法经 sina/qq 补采 (cat={cat})"}

    existing = _get_existing_dates(clean)
    if cat == 'a_share_sz':
        rows = _fetch_sina_a_share(clean, 'sz', days_back) or _fetch_qq_a_share(clean, 'sz', days_back)
    elif cat == 'a_share_sh':
        rows = _fetch_sina_a_share(clean, 'sh', days_back) or _fetch_qq_a_share(clean, 'sh', days_back)
    elif cat == 'hk':
        rows = _fetch_qq_hk_index(clean, days_back)
    elif cat == 'jp_index':
        rows = _fetch_sina_global_index(clean, days_back)
    else:
        rows = []

    if not rows:
        return {"status": "fail", "fund_code": fund_code, "symbol": clean,
                "message": f"指数 {clean} 无数据（sina/qq 均未返回）"}

    inserted = 0
    for date_str, close in rows:
        if date_str in existing:
            continue
        _upsert_index_history(clean, date_str, close, 'sina')
        inserted += 1
    return {"status": "ok", "fund_code": fund_code, "symbol": clean, "new_records": inserted}
