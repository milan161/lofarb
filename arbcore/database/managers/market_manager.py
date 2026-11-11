from .base import BaseManager
import sqlite3
import pandas as pd
from datetime import datetime
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class MarketManager(BaseManager):
    # [AI-2026-07-08] 新增 usd_cny_spot 在岸价支持
    # [AI-2026-07-09] 新增 jpy_cny_mid 日元中间价支持（QDII日本估值）
    # [AI-2026-08-02] 新增 jpy_cny_spot 列支持（实时估值在岸价历史，与 woody 实时口径一致）
    def upsert_exchange_rate(self, date: str, usd_cny_mid: float = None, hkd_cny_mid: float = None, usd_cnh: float = None, usd_cny_spot: float = None, jpy_cny_mid: float = None, jpy_cny_spot: float = None, eur_cny_mid: float = None, eur_cny_spot: float = None):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            # [AI-2026-07-03] 新增 usd_cnh 列支持
            # [AI-2026-07-08] 新增 usd_cny_spot 列支持
            # [AI-2026-07-09] 新增 jpy_cny_mid 列支持
            # [AI-2026-08-02] 新增 jpy_cny_spot 列支持
            # [AI-2026-10-07] 新增 eur_cny_mid / eur_cny_spot 列支持（QDII欧洲德/法ETF估值）
            cursor.execute("SELECT usd_cny_mid, hkd_cny_mid, usd_cnh, usd_cny_spot, jpy_cny_mid, jpy_cny_spot, eur_cny_mid, eur_cny_spot FROM exchange_rate WHERE date = ?", (date,))
            row = cursor.fetchone()

            # [A根因修复 2026-09-18] 若目标行不存在(新建)且本次所有汇率值均为空，
            # 不建"全 NULL"空行——避免 9:15 前中间价未发布时污染 exchange_rate 表
            # （正常有值写入路径不受影响：任一值非 None 即正常建/更新行）
            if row is None and all(v is None for v in (usd_cny_mid, hkd_cny_mid, usd_cnh, usd_cny_spot, jpy_cny_mid, jpy_cny_spot, eur_cny_mid, eur_cny_spot)):
                conn.close()
                return

            exist_usd = row[0] if row else None
            exist_hkd = row[1] if row else None
            exist_cnh = row[2] if row else None
            exist_spot = row[3] if row else None
            exist_jpy = row[4] if row else None
            exist_jpy_spot = row[5] if row else None
            exist_eur = row[6] if row else None
            exist_eur_spot = row[7] if row else None

            new_usd = usd_cny_mid if usd_cny_mid is not None else exist_usd
            new_hkd = hkd_cny_mid if hkd_cny_mid is not None else exist_hkd
            new_cnh = usd_cnh if usd_cnh is not None else exist_cnh
            new_spot = usd_cny_spot if usd_cny_spot is not None else exist_spot
            new_jpy = jpy_cny_mid if jpy_cny_mid is not None else exist_jpy
            new_jpy_spot = jpy_cny_spot if jpy_cny_spot is not None else exist_jpy_spot
            new_eur = eur_cny_mid if eur_cny_mid is not None else exist_eur
            new_eur_spot = eur_cny_spot if eur_cny_spot is not None else exist_eur_spot

            query = "INSERT OR REPLACE INTO exchange_rate (date, usd_cny_mid, hkd_cny_mid, usd_cnh, usd_cny_spot, jpy_cny_mid, jpy_cny_spot, eur_cny_mid, eur_cny_spot, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, (datetime('now', 'localtime')))"
            conn.execute(query, (date, new_usd, new_hkd, new_cnh, new_spot, new_jpy, new_jpy_spot, new_eur, new_eur_spot))
            conn.commit()
            conn.close()

    def upsert_futures_daily(self, date: str, symbol: str, settle_price: float = None, calibration: float = None, close_price: float = None, volume: int = None):
        with self.lock:
            conn = self._get_conn()
            conn.execute("INSERT OR IGNORE INTO futures_daily (date, symbol) VALUES (?, ?)", (date, symbol))
            if settle_price is not None:
                conn.execute("UPDATE futures_daily SET settle_price = ?, updated_at = (datetime('now', 'localtime')) WHERE date = ? AND symbol = ?", (settle_price, date, symbol))
            if calibration is not None:
                conn.execute("UPDATE futures_daily SET calibration = ?, updated_at = (datetime('now', 'localtime')) WHERE date = ? AND symbol = ?", (calibration, date, symbol))
            if close_price is not None:
                conn.execute("UPDATE futures_daily SET close_price = ?, updated_at = (datetime('now', 'localtime')) WHERE date = ? AND symbol = ?", (close_price, date, symbol))
            if volume is not None:
                conn.execute("UPDATE futures_daily SET volume = ?, updated_at = (datetime('now', 'localtime')) WHERE date = ? AND symbol = ?", (volume, date, symbol))
            conn.commit()
            conn.close()
            
    def upsert_usa_etf_price(self, date: str, symbol: str, price: float, netvalue: float = None):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("UPDATE usa_etf_daily_prices SET price = ?, netvalue = COALESCE(?, netvalue), updated_at = (datetime('now', 'localtime')) WHERE date = ? AND symbol = ?", (price, netvalue, date, symbol))
            if cursor.rowcount == 0:
                query = "INSERT INTO usa_etf_daily_prices (date, symbol, price, netvalue) VALUES (?, ?, ?, ?)"
                cursor.execute(query, (date, symbol, price, netvalue))
            conn.commit()
            conn.close()

    # 🔴 [AI-2026-11-11 东哥拍板] 单事务批量写：替代 step5 的逐行 upsert。
    # 旧路径每份 VPS 文件 18.9 万行 × 每行 _get_conn/UPDATE/INSERT/commit/close = 38 万次
    # 独立事务，ARM 1 OCPU 单次 step5 耗 62 分钟。改后全部行在**一个连接、一个事务**里完成。
    def bulk_upsert_usa_etf_prices(self, rows):
        """rows: [(date, symbol, price), ...]，依赖 PRIMARY KEY (date, symbol)。

        语义与 upsert_usa_etf_price(..., netvalue=None) 完全一致：
        冲突只更新 price + updated_at，**绝不触碰 netvalue**（净值由另一条 VPS 链路写入）。
        返回写入行数。
        """
        if not rows:
            return 0
        with self.lock:
            conn = self._get_conn()
            try:
                conn.executemany(
                    "INSERT INTO usa_etf_daily_prices (date, symbol, price) VALUES (?, ?, ?) "
                    "ON CONFLICT(date, symbol) DO UPDATE SET "
                    " price = excluded.price, "
                    " updated_at = (datetime('now', 'localtime'))",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return len(rows)

    def bulk_upsert_index_history(self, rows, source: str = 'yahoo_vps'):
        """rows: [(symbol, date, close), ...]，与 upsert_index_history 同语句，单事务。"""
        if not rows:
            return 0
        with self.lock:
            conn = self._get_conn()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO index_history (symbol, date, close, source) VALUES (?, ?, ?, ?)",
                    [(s, d, c, source) for s, d, c in rows],
                )
                conn.commit()
            finally:
                conn.close()
        return len(rows)

    # [AI-2026-07-20] VPS Yahoo 同步用：写入指数收盘价到 index_history
    def upsert_index_history(self, symbol: str, date: str, close: float, source: str = 'yahoo_vps'):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO index_history (symbol, date, close, source) VALUES (?, ?, ?, ?)",
                (symbol, date, close, source)
            )
            conn.commit()
            conn.close()

    # [AI-2026-07-20] 只更新 netvalue（从 VPS Yahoo 同步），不覆盖 price
    def upsert_usa_etf_netvalue(self, date: str, symbol: str, netvalue: float):
        with self.lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("UPDATE usa_etf_daily_prices SET netvalue = ?, updated_at = (datetime('now', 'localtime')) WHERE date = ? AND symbol = ?", (netvalue, date, symbol))
            if cursor.rowcount == 0:
                cursor.execute("INSERT INTO usa_etf_daily_prices (date, symbol, netvalue) VALUES (?, ?, ?)", (date, symbol, netvalue))
            conn.commit()
            conn.close()

    def get_latest_usa_etf_date(self, symbol: str) -> str:
        conn = self._get_conn()
        query = "SELECT MAX(date) FROM usa_etf_daily_prices WHERE symbol = ?"
        cursor = conn.execute(query, (symbol,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result and result[0] else None

    def upsert_hkd_exchange_rate(self, date: str, hkd_cny_mid: float):
        self.upsert_exchange_rate(date, hkd_cny_mid=hkd_cny_mid)

    def get_latest_futures_price(self, symbol: str) -> Optional[float]:
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT settle_price FROM futures_daily 
                WHERE symbol = ? 
                ORDER BY date DESC LIMIT 1
            ''', (symbol,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result and result[0] is not None else None
        except Exception as e:
            logger.error(f"Failed to get futures price: {e}")
            return None

    def batch_save_futures_data(self, data_list: List[Dict[str, Any]]):
        try:
            for data in data_list:
                date_str = data.get('date', datetime.now().strftime('%Y-%m-%d'))
                sym = data.get('symbol')
                price = data.get('price', data.get('settle_price'))
                self.upsert_futures_daily(date=date_str, symbol=sym, settle_price=price)
            logger.info(f"Batch saved futures data: {len(data_list)} items")
        except Exception as e:
            logger.error(f"Failed to batch save futures data: {e}")
