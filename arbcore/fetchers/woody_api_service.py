# -*- coding: utf-8 -*-
# woody_api_service.py - 统一的 Woody API 数据获取、备份与因子解析服务

import os
import json
import time  # 🌟 新增：用于重试机制的延时
from datetime import datetime
import pandas as pd
import logging

from .woody_telegram_client import FetchPalmmicroData

logger = logging.getLogger(__name__)

# [AI-2026-10-12 东哥拍板] QDII日本 5 只 hedge/calibration 硬编码冻结值（方案 A · 撤 woody 依赖）。
# 值来源：arb_master.db fund_daily_factors 库内最后一次 woody 真值
# （4 只 N225 = 2026-10-09 行；513800/TOPIX = 2025-09-29 行）。
# 冻结目的：woody 上游 payload 日期停在 2026-10-09 但随时可能恢复推新值，
# 硬编码保证对冲手数口径（hedge × round(calib×multiplier)）永久恒定、全读方同源。
# ⚠️ 任何脚本/AI 禁止擅自更新此表——口径由东哥拍板，改动须东哥确认。
_QDII_JP_FROZEN_HEDGE = {
    "513000": (136508.840113, 126953.221305),  # 易方达日经225ETF
    "159866": (169828.546392, 169828.546392),  # 华安日经225ETF
    "513520": (131916.79887, 127959.294904),   # 工银日经225ETF
    "513880": (150125.755581, 136614.437579),  # 华安日经225ETF(513880)
    "513800": (9780.958888, 9291.910944),      # 南方日本东证指数ETF(513800)
}


class WoodyAPIService:
    @staticmethod
    def fetch_and_process(db, codes: list, backup_dir: str, source_id: str):
        """
        核心共享逻辑：拉取Woody API、保存数据湖、生成CSV/JSON双备份、提纯入库
        :param db: DatabaseManager 实例
        :param codes: 基金代码列表 (如 ['162411', '513350'])
        :param backup_dir: 备份文件存放的物理绝对目录
        :param source_id: 来源标识 (用于数据湖和防刷标记，如 'woody_lof' 或 'woody_etf')
        """
        today_str = datetime.now().strftime('%Y-%m-%d')
        sync_key = f"{source_id}_batch"
        
        # 1. 防刷检查
        if db.is_access_synced_today(today_str, sync_key):
            logger.info(f"✅ 今日已成功拉取过 {source_id}，防刷机制启动，跳过网络请求...")
            raw_content = db.get_raw_api_data(today_str, source_id)
            if not raw_content:
                return None
            try:
                return json.loads(raw_content)
            except Exception:
                return None

        # 2. 拼装请求并调用 Telegram 接口
        symbols = []
        for c in codes:
            c_str = str(c).strip()
            if not c_str: continue
            prefix = 'sh' if c_str.startswith('5') else 'sz'
            symbols.append(f"{prefix}{c_str}")
        
        symbols_str = ",".join(set(symbols))
        logger.info(f"📡 正在向 Woody API 发起批量请求: {symbols_str}")
        
        # 🌟 优化：引入带指数退避的重试机制 (最多尝试 3 次)
        max_retries = 3
        result = None
        for attempt in range(1, max_retries + 1):
            try:
                result = FetchPalmmicroData(symbols_str)
                if result and 'text' in result:
                    break  # 获取成功，跳出重试循环
                else:
                    logger.warning(f"⚠️ 第 {attempt} 次请求成功，但返回数据格式异常或为空。")
            except Exception as e:
                logger.warning(f"⚠️ 第 {attempt} 次请求 Woody API 失败: {e}")
            
            if attempt < max_retries:
                sleep_time = 2 ** attempt  # 指数退避: 第1次失败等2秒，第2次等4秒
                logger.info(f"⏳ 等待 {sleep_time} 秒后进行第 {attempt + 1} 次重试...")
                time.sleep(sleep_time)

        if not result or 'text' not in result:
            logger.error("❌ Woody API 历经多次重试后依然返回为空或请求失败。")
            return None

        api_data = result['text']
        if isinstance(api_data, str):
            try: api_data = json.loads(api_data)
            except: pass
                
        # 3. 校验 API 返回数据质量：必须是 dict 且包含基金代码键
        if not isinstance(api_data, dict) or not api_data:
            logger.error(f"❌ [woody_lof] API 返回数据无效（非字典或为空），不入库。原始内容: {str(api_data)[:200]}")
            return None

        raw_json_str = json.dumps(api_data, ensure_ascii=False, indent=2)
        
        # 4. 存入原始数据湖
        db.save_raw_api_data(date=today_str, source=source_id, raw_content=raw_json_str)

        # 4. 智能生成 JSON / 动态宽表 CSV 备份
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        
        try:
            # JSON
            json_path = os.path.join(backup_dir, f"Data_{source_id}_{timestamp}.json")
            with open(json_path, 'w', encoding='utf-8') as f:
                f.write(raw_json_str)
                
            # 动态 CSV (修复原有写死字段导致新ETF丢失的问题)
            if isinstance(api_data, dict):
                csv_data = []
                dynamic_cols = set(['symbol', 'type', 'CNY', 'position', 'date', 'netvalue', 'CNYholdings', 'calibration', 'hedge', 'symbol_hedge'])
                
                for sym, f_data in api_data.items():
                    if not isinstance(f_data, dict): continue
                    row = {'symbol': sym}
                    for field in ['type', 'CNY', 'position', 'date', 'netvalue', 'CNYholdings', 'calibration', 'hedge']:
                        row[field] = f_data.get(field, '')
                    
                    sh_data = f_data.get('symbol_hedge', '')
                    if isinstance(sh_data, dict):
                        for e_name, e_data in sh_data.items():
                            clean_name = e_name
                            if ('-JP' in clean_name or '-EU' in clean_name or '-HK' in clean_name) and not clean_name.startswith('^'): 
                                clean_name = f"^{clean_name}"
                            p_col, r_col = f"{clean_name}_price", f"{clean_name}_ratio"
                            dynamic_cols.update([p_col, r_col])
                            row[p_col] = e_data.get('price', '')
                            row[r_col] = e_data.get('ratio', '')
                    else:
                        row['symbol_hedge'] = str(sh_data)
                    csv_data.append(row)
                    
                if csv_data:
                    std_cols = ['symbol', 'type', 'CNY', 'position', 'date', 'netvalue', 'CNYholdings', 'calibration', 'hedge', 'symbol_hedge']
                    final_cols = std_cols + sorted(list(dynamic_cols - set(std_cols)))
                    csv_path = os.path.join(backup_dir, f"Data_{source_id}_{timestamp}.csv")
                    pd.DataFrame(csv_data, columns=final_cols).to_csv(csv_path, index=False, encoding='utf-8-sig')
        except Exception as e:
            logger.error(f"⚠️ 生成备份文件失败: {e}")

        # 5. 验证数据有效性（防止错误信息被标记为成功）
        if not isinstance(api_data, dict) or len(api_data) == 0:
            logger.error(f"❌ [{source_id}] API 返回数据无效（非字典或为空），不标记防刷。原始内容: {str(api_data)[:200]}")
            return None

        # 6. 调用提纯入库逻辑
        WoodyAPIService.process(db, api_data, source_id)

        # 7. 数据验证通过后才标记防刷（修复 bug：之前错误信息也会被标记）
        db.mark_access_synced(today_str, sync_key)
        logger.info(f"✅ [{source_id}] 因子提纯入库与双备份完毕！")
        return api_data

    @staticmethod
    def process(db, api_data: dict, source_id: str = 'woody_lof'):
        """
        核心数据提纯入库解析逻辑，支持解析最新的估值日数据 (est_date, est_price)
        """
        today_str = datetime.now().strftime('%Y-%m-%d')
        if not isinstance(api_data, dict):
            return None

        # [AI-2026-07-21] 预加载基金类别映射，用于 CNYest 路由到正确汇率列
        _fund_cats = {}
        try:
            _conn = db._get_conn()
            for _fc, _cat in _conn.execute("SELECT fund_code, category FROM unified_fund_list").fetchall():
                _fund_cats[_fc] = _cat
            _conn.close()
        except Exception:
            pass

        for sym, f_data in api_data.items():
            if not isinstance(f_data, dict): continue

            # 🌟 新增：如果是基础标的（如 GLD, USO, ^GSPC, ^NDX），则提取其校准值存入 futures_daily
            if sym in ['GLD', 'USO', '^GSPC', '^NDX']:
                future_mapping = {'GLD': 'GC', 'USO': 'CL', '^GSPC': 'ES', '^NDX': 'NQ'}
                db_sym = future_mapping.get(sym)
                api_calib = f_data.get('calibration')
                api_date = f_data.get('est_date', f_data.get('date', today_str))
                if api_calib:
                    try:
                        calib_val = float(api_calib)
                        if calib_val > 0:
                            db.upsert_futures_daily(date=api_date, symbol=db_sym, calibration=calib_val)
                            logging.info(f"✅ 从API同步全局校准值: {db_sym} = {calib_val} (日期: {api_date})")
                    except Exception as e:
                        logging.error(f"❌ 解析全局校准值 {sym} 失败: {e}")

            fund_code = sym.replace('sh', '').replace('sz', '').replace('SH', '').replace('SZ', '')
            
            raw_date = f_data.get('date', '')
            b_date = pd.to_datetime(str(raw_date).strip()).strftime('%Y-%m-%d') if raw_date else today_str
            
            # --- 解析最新的估值日 (est_date) ---
            est_date_raw = f_data.get('est_date', '')
            e_date = pd.to_datetime(str(est_date_raw).strip()).strftime('%Y-%m-%d') if est_date_raw else None
            
            pos = f_data.get('position')
            pos = float(pos)/100.0 if pos and float(pos) > 2 else (float(pos) if pos else 1.0)
            # 兼容「键存在但值为 None」的网页降级数据（API 路径恒为数值，不受影响）
            cal = float(f_data['calibration']) if f_data.get('calibration') is not None else None
            hed = float(f_data['hedge']) if f_data.get('hedge') is not None else None
            nav_val = float(f_data['netvalue']) if f_data.get('netvalue') else None

            # [AI-2026-10-12 东哥拍板] QDII日本 5 只 hedge/calibration 写前硬编码冻结（方案 A · 撤 woody 依赖）：
            # 无论 woody payload 停更还是恢复推新值，这 5 只写入 fund_daily_factors 的
            # hedge/calibration 一律用 _QDII_JP_FROZEN_HEDGE 冻结值，保证对冲手数
            # （hedge × round(calib×multiplier)）与全读方（get_base_data/static_valuation/
            # _resolve_hedge_for_order/fund_service 显示回填）口径恒定同源。
            # position/nav/CNYest 不冻结，照常随 payload 更新。
            _frozen = _QDII_JP_FROZEN_HEDGE.get(fund_code)
            if _frozen is not None:
                hed, cal = _frozen

            # [AI-2026-08-20] 写入基准日数据
            # [AI-2026-10-15 东哥定调 · 修「hedge 日期错位」] calibration/hedge/nav 三者都是
            #   **基准日当天的观测值**，只属于 b_date（= woody payload 的 `date`）：
            #     hedge(D) = XOP净值(D) × 人民币中间价(D) ÷ [ nav(D) − nav(D−1)×(1−pos) ]
            #   即 hedge(D) 由 **D 日已公布的官方净值**推出，D 日净值没公布就不存在 hedge(D)。
            #   实证（vps_sync 四份文件逐日交叉，2026-10-15 核）：
            #     woody_2026-10-12  date=10-08  est=10-09  hedge=1378.29949
            #     woody_2026-10-13  date=10-09  est=10-12  hedge=1377.678951
            #     woody_2026-10-14  date=10-12  est=10-13  hedge=1378.776419
            #     woody_2026-10-15  date=10-13  est=10-14  hedge=1380.677713
            #   四行与 fund_daily_factors 里 date=10-08/10-09/10-12/10-13 的值逐行吻合，
            #   亦与 woody 网页 10-09→1378 / 10-12→1379 / 10-13→1381 对齐 ⇒ 口径确认。
            db.upsert_fund_factor(date=b_date, fund_code=fund_code, calibration=cal, hedge=hed, position=pos, nav=nav_val)

            # [AI-2026-08-20 立 → AI-2026-10-15 修订] est_date 行**只保留 position**（慢变量、
            #   非「当日观测」），calibration/hedge/nav 一律写 None。
            #   旧行为是把同一份三元组也写进 est_date ⇒ 「10-13 的 hedge 出现在 10-14 行」，
            #   导出 CSV 与历史页对 hedge 整体前移一天（2026-10-15 东哥实测 162411 复现）。
            #   缺失即留空，绝不把基准日的值伪装成 est_date 当天的值（SUPREME 铁律「不伪装」）。
            #   安全性（改前逐一核过读方，取值不变）：
            #     · dynamic_valuation.get_base_data / fund_service 展示回填 /
            #       main._resolve_hedge_for_order 全是 `hedge IS NOT NULL ORDER BY date DESC LIMIT 1`
            #       ⇒ 去掉 est_date 副本后自动回落到 b_date 行，取到的数一样；
            #     · static_valuation 的 hedge 取自 base_row（基准行）而非当日行，不受影响；
            #     · fund_service t1_data 的 `COALESCE(h.nav, f.nav)` 所取出的 nav 字段无人消费。
            if e_date and e_date != b_date:
                db.upsert_fund_factor(date=e_date, fund_code=fund_code, calibration=None, hedge=None, position=pos, nav=None)
                logger.debug(f"  🔧 [{fund_code}] 回填 {e_date} 仓位（hedge/校准/净值归基准日 {b_date}，不搬）")
            
            # 🌟 Woody API 返回了真实仓位时，同步更新 unified_fund_list.pos_ratio
            raw_pos = f_data.get('position')
            if raw_pos is not None:
                try:
                    clean_pos = float(raw_pos)/100.0 if float(raw_pos) > 2 else float(raw_pos)
                    db.update_fund_pos_ratio(fund_code, clean_pos)
                except (ValueError, TypeError) as e:
                    logger.warning(f"⚠️ 解析 position 失败 {fund_code}: raw={raw_pos}, err={e}")
            
            # [AI-2026-07-21] 保存 Woody 提供的估值日汇率 (CNYest) 到汇率表
            # 修复：写入正确的汇率列——JPY基金→jpy_cny_mid, HKD基金→hkd_cny_mid, 其余→usd_cny_mid
            # 旧bug：所有CNYest都写入usd_cny_mid，JPY基金的日元汇率覆盖了美元汇率
            if e_date and f_data.get('CNYest'):
                try:
                    cny_est = float(f_data['CNYest'])
                    fc = _fund_cats.get(fund_code, '')
                    if fc == 'QDII日本':
                        db.upsert_exchange_rate(e_date, jpy_cny_mid=cny_est)
                    elif fc == 'QDII亚洲':
                        db.upsert_exchange_rate(e_date, hkd_cny_mid=cny_est)
                    else:
                        db.upsert_exchange_rate(e_date, usd_cny_mid=cny_est)
                except Exception:
                    pass
            
            # [AI-2026-09-22] 修复：symbol_hedge 的 str 形态被静默丢弃 → 17 只基金篮子从未入库
            #   woody 该字段有两种形态：
            #     dict → 多标的篮子，带 ratio/price/est_price（实测 14 只，如 161116 的 GLD+^GLD-EU）
            #     str  → 单标的跟踪型基金的简写（如 SZ162411 → "XOP"，即净值 100% 跟 XOP）
            #   旧代码只认 dict，str 形态直接落到 else 外被跳过 → 这 17 只基金在
            #   fund_basket_weights 里永远无行（162411/161127/161125/161130/162415/161128/
            #   161126/164906/159502/159518/513350/513000/159866/513520/513880/501300/159561）。
            #   权重取 100.0：与 dict 形态的单标的基金一致（实测 164701 的 GLD weight=100.0）。
            #   ⚠️ 仅当 key 是 6 位纯数字基金代码时才按篮子处理：GLD→hf_GC、SPY→^GSPC、
            #      QQQ→^NDX、SGOL/AAAU/IAU/UGL→hf_GC、ZSL→hf_SI、SCO→hf_CL 等 str 是
            #      「该 ETF 的对冲物」语义，不是估值篮子，一并写入会污染篮子表。
            sh_data = f_data.get('symbol_hedge')
            if isinstance(sh_data, str) and sh_data.strip() and fund_code.isdigit() and len(fund_code) == 6:
                sh_data = {sh_data.strip(): {'ratio': 100.0}}
            if isinstance(sh_data, dict):
                # [AI-2026-07-29] 修复权重换代残留bug：woody 篮子每日可能换代（标的组合变化），
                # 旧逻辑只 INSERT OR REPLACE 新代符号，换代后消失的旧 symbol 行残留 → 权重和>100%。
                # 先按新代符号集删除该 (b_date, fund) 下的旧残留行，再逐个 upsert。
                # 详见 docs_unfinished/权重换代残留bug修复说明_2026-07-29.md
                _new_gen_syms = []
                for _s in sh_data.keys():
                    if ('-JP' in _s or '-EU' in _s or '-HK' in _s) and not _s.startswith('^'): _s = f"^{_s}"
                    _new_gen_syms.append(_s)
                try:
                    db.prune_fund_basket_weights(date=b_date, fund_code=fund_code, valid_symbols=_new_gen_syms)
                except Exception as _pe:
                    logger.warning(f"⚠️ 权重换代清理失败 {fund_code}@{b_date}: {_pe}")

                for etf_sym, etf_info in sh_data.items():
                    clean_etf = etf_sym
                    if ('-JP' in clean_etf or '-EU' in clean_etf or '-HK' in clean_etf) and not clean_etf.startswith('^'): clean_etf = f"^{clean_etf}"
                    price = float(etf_info.get('price', 0))
                    est_price = float(etf_info.get('est_price', 0)) if etf_info.get('est_price') else 0.0
                    ratio = float(etf_info.get('ratio', 0))
                    
                    # [AI-2026-06-29] 修复：移除 clean_etf.startswith('^') 限制，
                    # 使非^符号（GLD/SLV/USO等）的 price/est_price 也能写入 usa_etf_daily_prices
                    # 这些是真实的 ETF 市场价格（区别于顶层 netvalue 是 NAV）
                    if price > 0: 
                        db.upsert_usa_etf_price(date=b_date, symbol=clean_etf, price=price)
                    
                    if est_price > 0 and e_date:
                        db.upsert_usa_etf_price(date=e_date, symbol=clean_etf, price=est_price)
                        
                    if ratio != 0: db.upsert_fund_basket_weight(date=b_date, fund_code=fund_code, underlying_symbol=clean_etf, weight=ratio)
                    
        return api_data
