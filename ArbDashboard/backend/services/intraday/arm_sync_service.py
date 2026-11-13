#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[2026-08-27] 从 ARM 同步当日分时数据到本地数据库。

背景（东哥口径）：
- 分时价格数据全部由 ARM / AMD 采集，回传到本地；本地程序不采分时。
- 本地库在下午才开机时，当日上午分时缺失 → 分时图上午空白。
- 因此：页面启动 / 当日首次分时请求时，通过 ssh 在 ARM 端直连主库拉取当日关注基金分时（不再 SCP 整库）。

仅同步关注基金（与 H5 一致）：162411 / 164701 / 161116 / 513350 / 159518。
其它基金不在此同步范围（东哥 2026-08-27：其它基金不需要；2026-11-13 扩 2 只 XOP 跟踪 ETF）。

设计原则（第一性原理，非打补丁）：
- 通过 ssh 在 ARM 端直接 SELECT 当日关注基金分时并以 CSV 流式拉回（不再 SCP 整库）。
  整库约 87MB 在慢链路会超 30s 而静默失败 → 本地永远缺当日数据（2026-11-12 已复现）。
  改为 ssh 取数仅当日 5 只几十 KB，秒回，彻底摆脱"超时才失败"。
- 以 ARM 为准：拉到数据才 DELETE 本地当日关注基金行再整批 INSERT；ARM 不可达/无数据绝不空清本地。
- 幂等：先删后插，本地与 ARM 当日完全一致。
- 刷新策略：TTL 幂等（成功后再过 _SYNC_TTL_SEC 秒才允许下一次），失败退避 _RETRY_BACKOFF_SEC。
  [2026-11-13 修] 原「当天只同步一次」导致下午打开页面曲线永远停在上午最后一次同步点
  （11:52 同步完 → 之后全部 skipped，实测 162411 停在 11:30）。
- 所有异常就地捕获返回 dict，绝不抛给调用方（不阻塞 API / 启动）。
"""

import csv
import io
import sqlite3
import subprocess
import threading
import time
from datetime import datetime

from arbcore.database.db_manager import _resolve_db_path

# 主库唯一：与 backend/main.py 同款，走 arbcore 统一解析
LOCAL_DB = _resolve_db_path()
# ARM 端分时主库绝对路径（ssh 内联命令直连，避免 SCP 整库超时）
ARM_REMOTE_DB = '/home/ubuntu/arbtest/database/arb_master.db'

# 关注基金：仅这 5 只做分时同步（与 H5 intradayCodes 一致；2026-11-13 扩 513350/159518 两只 XOP 跟踪 ETF）
ARM_SYNC_FUNDS = ['162411', '164701', '161116', '513350', '159518']

# 与 fund_intraday_quotes 表列顺序严格一致（SELECT 与本地 INSERT 共用）
_INTRADAY_COLS = [
    'fund_code', 'date', 'time', 'price', 'rt_val', 'premium',
    'open_premium', 'close_premium', 'lof_bid1', 'lof_ask1',
    'etf_bid1', 'etf_ask1',
]
# 数值列索引（CSV 文本化后需转回 float，None/空串 → None）
_NUMERIC_IDX = {3, 4, 5, 6, 7, 8, 9, 10, 11}
# ssh 取数超时：仅拉当日5只几十 KB，秒回，留足余量（远小于原 SCP 仍会失败的量级）
_SSH_TIMEOUT = 25

_sync_lock = threading.Lock()
# [2026-11-13] 由「当天一次」改为 TTL 刷新：分时是每分钟 1 条，TTL 也取 60s；
# 失败退避 5 分钟，避免 ssh 不通时每次点开页面都卡在超时上。
_last_ok_ts = 0.0          # 上次成功同步的 monotonic 时间戳（0 = 从未成功）
_fail_until_ts = 0.0       # 失败退避截止（monotonic）
_SYNC_TTL_SEC = 60
_RETRY_BACKOFF_SEC = 300


def _fetch_from_arm(fund_codes, date):
    """通过 ssh 在 ARM 端直连主库 SELECT 当日关注基金分时，CSV 流式拉回本地。

    不 SCP 整库：原方案 SCP 整个 arb_master.db（约 87MB）在慢链路会超 30s 而
    静默失败，导致本地永远缺当日分时（2026-11-12 已复现）。改为 ssh 取数仅
    当日 5 只几十 KB，秒回，彻底摆脱"超时才失败"。
    返回 list[list]（12 列，数值列仍为字符串），失败时抛 RuntimeError。"""
    codes_arg = ','.join(fund_codes)
    ph = ','.join('?' * len(fund_codes))
    cols = ','.join(_INTRADAY_COLS)
    # 外层单引号保护整段 -c 脚本，脚本内部一律双引号字符串，远程 shell 不破坏
    remote_py = (
        "python3 -c '"
        "import sys,sqlite3,csv;"
        'codes="' + codes_arg + '".split(",");'
        'd="' + date + '";'
        'c=sqlite3.connect("' + ARM_REMOTE_DB + '");'
        'ph=",".join("?"*len(codes));'
        'q="SELECT ' + cols + ' FROM fund_intraday_quotes WHERE date=? AND fund_code IN ("+ph+") ORDER BY rowid";'
        'w=csv.writer(sys.stdout);'
        '[w.writerow(["" if v is None else v for v in r]) for r in c.execute(q,[d]+codes)]'
        "'"
    )
    try:
        proc = subprocess.run(
            ['ssh', 'arm', remote_py],
            capture_output=True, text=True, timeout=_SSH_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'ssh 取数超时（>{_SSH_TIMEOUT}s）')
    if proc.returncode != 0:
        raise RuntimeError(f'ARM 取数失败: {proc.stderr.strip()[:300]}')
    rows = []
    for line in csv.reader(io.StringIO(proc.stdout)):
        if len(line) < len(_INTRADAY_COLS):
            continue
        row = []
        for i, val in enumerate(line):
            row.append(None if val == '' else float(val) if i in _NUMERIC_IDX else val)
        rows.append(row)
    return rows


def sync_arm_intraday_to_local(fund_codes=None, date=None):
    """执行一次同步：ssh 在 ARM 端直连主库拉取当日关注基金分时 → 合并到本地。
    返回 dict(status, updated, message)。失败不抛异常。"""
    fund_codes = fund_codes or ARM_SYNC_FUNDS
    date = date or datetime.now().strftime('%Y-%m-%d')
    try:
        rows = _fetch_from_arm(fund_codes, date)
        # ARM 无当日数据时不删本地（避免误清空），直接返回
        if not rows:
            return {'status': 'ok', 'updated': 0, 'message': 'ARM 无当日数据，跳过'}

        # 以 ARM 为准：先删本地当日关注基金行，再整批写入，保证本地与 ARM 完全一致
        # （仅当 ARM 已拉到数据才执行删除，ARM 不可达时绝不清空本地）
        conn = sqlite3.connect(LOCAL_DB)
        cur = conn.cursor()
        placeholders = ','.join('?' * len(fund_codes))
        cur.execute(
            f'DELETE FROM fund_intraday_quotes WHERE date=? AND fund_code IN ({placeholders})',
            (date, *fund_codes)
        )
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for row in rows:
            cur.execute('''
                INSERT INTO fund_intraday_quotes
                (fund_code, date, time, price, rt_val, premium, open_premium, close_premium,
                 lof_bid1, lof_ask1, etf_bid1, etf_ask1, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (*row, now))
        updated = len(rows)
        conn.commit()
        conn.close()
        return {'status': 'ok', 'updated': updated, 'message': f'同步 {updated} 条'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def ensure_synced_today():
    """TTL 幂等同步：距上次**成功**同步 < _SYNC_TTL_SEC 秒则跳过，否则重新同步。

    2026-11-13 改：原实现在首次成功后把 _synced_date 置为今天，此后整日 skipped，
    导致下午打开页面时曲线停在上午最后一次同步点（实测 162411 停在 11:30）。
    分时是每分钟 1 条，因此 TTL 也取 60s —— 页面每次打开都可能拿到最新分钟。

    失败不置位成功时间戳，改为退避 _RETRY_BACKOFF_SEC，避免 ssh 不通时反复卡超时。
    """
    global _last_ok_ts, _fail_until_ts
    now = time.monotonic()
    if _last_ok_ts and now - _last_ok_ts < _SYNC_TTL_SEC:
        return {'status': 'skipped', 'message': f'{_SYNC_TTL_SEC}s 内已同步'}
    if now < _fail_until_ts:
        return {'status': 'skipped', 'message': '上次同步失败，退避中'}
    with _sync_lock:
        now = time.monotonic()
        if _last_ok_ts and now - _last_ok_ts < _SYNC_TTL_SEC:
            return {'status': 'skipped', 'message': f'{_SYNC_TTL_SEC}s 内已同步'}
        if now < _fail_until_ts:
            return {'status': 'skipped', 'message': '上次同步失败，退避中'}
        res = sync_arm_intraday_to_local()
        if res.get('status') == 'ok':
            _last_ok_ts = time.monotonic()
        else:
            _fail_until_ts = time.monotonic() + _RETRY_BACKOFF_SEC
        return res


def trigger_sync_async(fund_codes=None):
    """后台线程触发同步，不阻塞调用方。供启动钩子 / 路由内 fire-and-forget 使用。"""
    def _run():
        sync_arm_intraday_to_local(fund_codes)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
