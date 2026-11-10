# -*- coding: utf-8 -*-
# daily_updater.py - 每日数据大一统更新器
import os
import sys
# 自动引导路径：确保能找到根目录下的 arbcore
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))
# [AI-2026-11-10 修 VPS 告警断链] main.py:591-596 以
#   [python, ".../backend/scheduler/daily_updater.py"] 起子进程 ⇒ sys.path[0] = backend/scheduler，
#   backend/ 不在路径中 ⇒ 下方 `from services.alert_service import ...` 与 `..services` 相对导入
#   **双双失败** ⇒ _send_alert=None ⇒ VPS 硬故障只写日志、电报一条不发（2026-10-20~11-07
#   VPS 停机 19 天期间 ARM journald 里 674 条告警全部带「（告警通道不可用）」）。
# 补 backend/ 进 sys.path：包方式导入时该路径已在，幂等不重复；不改动告警降级逻辑本身。
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
import json
import yaml
import logging
from datetime import date, datetime, timedelta
import pandas as pd
import re
import time
import random

# 引入项目基座
from arbcore.base_app import BaseApp, setup_logging, PROJECT_LOGS_DIR
from arbcore.fetchers.historical import HistoricalDataManager
from arbcore.fetchers.woody_web_crawler import WoodyWebCrawler
from arbcore.fetchers.woody_api_service import WoodyAPIService
from arbcore.calculators.static_valuation import StaticValuationCalculator
# [AI-2026-10-07 东哥抓错] 欧元基金清单（德/法 QDII）。step11 静态估值对这批基金必须用 eur_cny_mid，
# 不能用美元（口径与 common.py resolve_fx / dynamic_valuation.py 对称）。
from arbcore.analysis.common import EUR_FUND_CODES
# [AI-2026-09-28 东哥点名] A股交易日历（含中国法定假日，chinese_calendar + _HOLIDAYS_2026 兜底）。
# 原 step4 内两处「上一交易日」只跳周末 ⇒ 9-25(中秋) 被当成交易日，预期净值日长期落后、
# 每次跑都白抓一次东财（当日日志 501018 出现 498 次即此）；收盘价齐全判定同病。
from arbcore.utils.market_calendar import is_trading_day

# [AI-2026-10-11 东哥拍板] 告警通道（Telegram）：VPS 硬故障立即报警、不做兜底。
# 宽松导入：本机/ARM 均可用；导入失败则降级为「只记日志」，绝不影响流水线。
try:
    from services.alert_service import send_alert as _send_alert
except Exception:
    try:
        from ..services.alert_service import send_alert as _send_alert
    except Exception:
        _send_alert = None

# [AI-2026-10-11 东哥要求] 各 VPS 数据源的**预期产出时刻**（北京时间；对齐 deploy_vps.py 的 crontab）。
# 用途：`_try_sync_all_from_vps` 判为 NO_TODAY 时，据此提示"是正常时滞还是已超时"。
VPS_EXPECTED_TIME = {
    'shares':      '02:00',
    'prices':      '03:00',
    'nav':         '03:00',
    'usa_prices':  '07:00',
    'etf_nav':     '07:30',
    'woody':       '09:20',
    'fx':          '09:20',
    'futures':     '09:20',
    'index':       '16:30',
    'hssi':        '15:30',   # 已停用（HSSI 迁 ETFarb），仅防御性保留
}
# [AI-2026-10-11] 本机（Python3.11）**缺 exchange_calendars / chinese_calendar**，
# 故为「VPS 今日是否产出」的判定硬编码休市表 —— 与 VPS 各脚本内表**同源**，零依赖。
_VPS_US_HOLIDAYS_2026 = {
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25',
    '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
}
_VPS_JPX_HOLIDAYS_2026 = {
    '2026-01-01', '2026-01-02', '2026-01-12', '2026-02-11', '2026-02-23',
    '2026-03-20', '2026-04-29', '2026-05-04', '2026-05-05', '2026-05-06',
    '2026-07-20', '2026-07-21', '2026-07-22', '2026-07-23', '2026-08-11',
    '2026-09-21', '2026-09-22', '2026-09-23', '2026-10-12', '2026-11-03',
    '2026-11-23', '2026-11-24', '2026-12-23', '2026-12-31',
}
_VPS_EU_HOLIDAYS_2026 = {
    '2026-01-01', '2026-04-03', '2026-04-06', '2026-05-01', '2026-05-14',
    '2026-12-25', '2026-12-26',
}
# [AI-2026-08-16] 修复: account_private 为本地密钥文件(被 gitignore, 可能不存在),
# WOODY/VPS 凭据缺失时必须容错(与项目其他模块一致), 否则后端 import 阶段直接崩溃起不来
try:
    from arbcore.config.account_private import (
        WOODY_USERNAME, WOODY_PASSWORD,
        VPS_HOST, VPS_PORT, VPS_USER, VPS_PASSWORD, VPS_DATA_DIR, VPS_KEY_PATH, VPS_KEY_PASSWORD,
    )
except ImportError:
    WOODY_USERNAME = WOODY_PASSWORD = None
    VPS_HOST, VPS_PORT, VPS_USER, VPS_PASSWORD, VPS_DATA_DIR, VPS_KEY_PATH, VPS_KEY_PASSWORD = None, 22, None, None, None, None, None

# [AI-2026-10-13 东哥拍板] VPS_KEY_PATH / VPS_KEY_PASSWORD 支持环境变量覆盖：
# 本机 account_private.py 写的是 Windows 开发机路径（本机正确）；部署到 ARM 后该路径不存在，
# 导致 ARM→东京回退到密码认证。部署时由 sync_arm.sh 通过 systemd EnvironmentFile drop-in
# 注入 VPS_KEY_PATH=/home/ubuntu/.ssh/id_ed25519，此处优先读环境变量，使 ARM 用真实 key 路径、
# 且不改动凭据文件（凭据文件不随 deploy 推送）。本地不设 env → 用 account_private 原值。
VPS_KEY_PATH = os.environ.get('VPS_KEY_PATH', VPS_KEY_PATH)
VPS_KEY_PASSWORD = os.environ.get('VPS_KEY_PASSWORD', VPS_KEY_PASSWORD)

# [AI-2026-08-02] 展示副本模式（ARM）：只自采「A股行情 + 富途美股夜盘」，
# 其余静态数据一律从东京 VPS 只读，不在展示副本上重复造一套爬虫。
DASHBOARD_MODE = os.environ.get('ARB_DASHBOARD_MODE', '0') == '1'

# [AI-2026-08-03] 无日K线基金跳过名单：腾讯/新浪均无法提供日K线的基金（如债券/封闭LOF），
# 首次探测到即写入 arbcore/config/no_kline_funds.json，后续启动直接跳过，不再刷 WARNING。
NO_KLINE_DENYLIST_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'arbcore', 'config', 'no_kline_funds.json'))

def _load_no_kline_denylist():
    try:
        if os.path.exists(NO_KLINE_DENYLIST_PATH):
            with open(NO_KLINE_DENYLIST_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_no_kline_denylist(d):
    try:
        os.makedirs(os.path.dirname(NO_KLINE_DENYLIST_PATH), exist_ok=True)
        with open(NO_KLINE_DENYLIST_PATH, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _is_arm_runtime() -> bool:
    """本进程是否跑在 ARM(H5) 部署机上 —— 与 backend/main.py 的 _is_arm 同口径（按库路径区分）。

    ARM 库在 /home/ubuntu/（POSIX 路径），本机在 Windows（如 D:/Study/ARB/_shared/）。
    用于区分「同一套日终代码」在本机与 ARM 上的分支行为。
    """
    try:
        from arbcore.database.db_manager import _resolve_db_path
        p = _resolve_db_path()
    except Exception:
        return False
    return "/home/ubuntu" in p or p.startswith("/home")


class DailyUpdater(BaseApp):
    def __init__(self):
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        logs_dir = PROJECT_LOGS_DIR
        super().__init__("daily_updater", app_dir=scripts_dir, log_dir=logs_dir)
        # [AI-2026-07-08] 移除：BaseApp 现已在 arbcore/config/ 优先查找，无需重复重定向
        self.woody_crawler = WoodyWebCrawler()
        self.hist_manager = HistoricalDataManager(db_manager=self.db)
        self._woody_logged_in = False  # 延迟登录标记
        # [AI-2026-10-11] VPS 同步状态表：key=data_type →
        #   'HAS_TODAY' 有今日文件 / 'NO_TODAY' 连上但无今日文件（可能正常时滞，不报警）
        #   'SSH_FAIL' 连接或同步异常（硬故障，报警） / 'NO_DIR' 远端目录不存在（硬故障，报警）
        #   'NOT_CONFIGURED' 本机未配置 VPS 凭据
        # 由 _try_sync_all_from_vps 写入；其返回值保持 list 不变 ⇒ 10 处调用点零改动。
        self._vps_sync_status = {}
        # 降低第三方库日志噪音
        logging.getLogger('arbcore.fetchers.historical').setLevel(logging.WARNING)
    
    def _login_woody_if_needed(self):
        """延迟登录：只在真正需要时才登录 Woody 网站"""
        if self._woody_logged_in:
            return True
        
        username = WOODY_USERNAME
        password = WOODY_PASSWORD
        if username and password and username != "your_email@example.com":
            self.logger.info("🔐 [按需登录] 尝试登录 Woody 网站...")
            success = self.woody_crawler.login(username, password)
            if success:
                self.logger.info("✅ Woody 登录成功")
                self._woody_logged_in = True
                return True
            else:
                self.logger.warning("⚠️ Woody 登录失败，区域ETF数据可能无法获取")
                return False
        else:
            self.logger.warning("⚠️ 未配置 Woody 账号密码，区域ETF数据将无法获取")
            return False

    def _try_sync_all_from_vps(self, data_type='woody'):
        """
        [架构升级] 从云端增量同步所有缺失的历史数据 (支持断网补全)
        """
        # [AI-2026-08-02] 降权：允许「仅密钥」认证（展示副本 arbsync + 密钥），
        # 不再强制要求 VPS_PASSWORD。需满足 主机+用户+至少一种凭据。
        if not VPS_HOST or not VPS_USER or (not VPS_PASSWORD and not VPS_KEY_PATH):
            self._vps_sync_status[data_type] = 'NOT_CONFIGURED'
            return []
        
        # 数据目录迁移到 ArbDashboard/data/（与脚本目录解耦）
        local_sync_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "ArbDashboard", "data", "vps_sync"))
        os.makedirs(local_sync_dir, exist_ok=True)

        self.logger.info(f"[VPS] 正在扫描云端所有缺失的 {data_type} 历史数据...")
        synced_data_list = []
        try:
            import paramiko
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            # 优先使用私钥认证，失败则降级到密码
            try:
                if VPS_KEY_PATH and os.path.exists(VPS_KEY_PATH):
                    pkey = paramiko.Ed25519Key.from_private_key_file(VPS_KEY_PATH, password=VPS_KEY_PASSWORD)
                    ssh.connect(VPS_HOST, port=VPS_PORT, username=VPS_USER, pkey=pkey, timeout=10)
                    self.logger.info(f"[VPS] SSH 私钥认证成功")
                else:
                    raise Exception("no key file")
            except Exception as key_err:
                self.logger.info(f"[VPS] 私钥认证失败 ({key_err})，降级到密码认证")
                ssh.connect(VPS_HOST, port=VPS_PORT, username=VPS_USER, password=VPS_PASSWORD, timeout=10,
                            look_for_keys=False, allow_agent=False)
            
            sftp = ssh.open_sftp()
            # 1. 列表远程目录下的所有文件
            try:
                files = sftp.listdir(VPS_DATA_DIR)
            except IOError:
                self.logger.warning(f"[VPS] 远程目录不存在: {VPS_DATA_DIR}")
                self._vps_sync_status[data_type] = 'NO_DIR'
                self._alert_vps_failure(data_type, f"远端数据目录不存在: {VPS_DATA_DIR}")
                sftp.close(); ssh.close()
                return []

            # 2. 筛选对应类型的文件 (如 woody_2026-05-31.json)
            target_files = [f for f in files if f.startswith(f"{data_type}_") and f.endswith(".json")]
            
            for remote_file in sorted(target_files):
                # 从文件名提取日期 (woody_2026-05-31.json -> 2026-05-31)
                # [AI-2026-07-20] 修复：不能用 split('_')[1]（etf_nav_*.json 前缀有下划线会解析为'nav'）
                # [AI-2026-10-12] 修复：index_<SYM>_<date>.json（如 index_CAC_2026-10-08.json）被旧逻辑
                #   解析成 file_date="CAC_2026-10-08"，step5b 内 strptime(file_date) 崩溃、
                #   CAC/DAX/HSCEI 指数数据被丢弃。改用正则从文件名提取 YYYY-MM-DD 日期段。
                _m = re.search(r'(\d{4}-\d{2}-\d{2})', remote_file)
                if not _m:
                    self.logger.warning(f"[VPS] 无法从文件名提取日期，跳过: {remote_file}")
                    continue
                file_date = _m.group(1)
                # 同步标记键：保留完整文件名 stem（含符号），避免同日多指数文件互相覆盖标记
                #   index_CAC_2026-10-08.json → "CAC_2026-10-08"（与旧标记键一致，无迁移问题）
                #   woody_2026-05-31.json → "2026-05-31"（与旧行为一致）
                sync_marker_key = remote_file[len(data_type)+1:].replace('.json', '')

                local_path = os.path.join(local_sync_dir, remote_file)

                # [AI-2026-10-13] 修复「今日已同步→后续重跑误报无数据」：
                # 跳过「重新下载 / 重新打标」，但仍要把本地已同步文件读进 synced_data_list；
                # 否则当天首次同步成功后，每次重跑都返回空列表 → has_today 误判 False
                # → step4P 的 `if vps_prices:` 为假 → 调 _vps_missing_alert 狂发 BOT。
                # 仅当本地缓存文件也在时才计入（缓存被删则按未同步处理，保持旧行为）。
                sync_key = f"{data_type}_vps_sync"
                if self.db.is_access_synced_today(sync_marker_key, sync_key):
                    if os.path.exists(local_path):
                        try:
                            with open(local_path, 'r', encoding='utf-8') as f:
                                content = json.load(f)
                            synced_data_list.append({'date': file_date, 'content': content})
                        except Exception as e:
                            self.logger.warning(f"[VPS] 已同步文件本地读取失败(不计入): {remote_file}: {e}")
                    continue

                # 3. 增量同步：如果本地不存在，则下载
                if not os.path.exists(local_path):
                    remote_path = f"{VPS_DATA_DIR}/{remote_file}"
                    self.logger.info(f"[VPS] 正在补全历史数据: {remote_file}")
                    sftp.get(remote_path, local_path)

                # 4. 加载数据 (无论是刚下载的还是本地已有的)
                try:
                    with open(local_path, 'r', encoding='utf-8') as f:
                        content = json.load(f)
                    synced_data_list.append({'date': file_date, 'content': content})
                    # [AI-2026-07-03] 标记VPS文件已读取解析，下次跳过检查能找到此标记，避免重复加载
                    self.db.mark_access_synced(sync_marker_key, f"{data_type}_vps_sync")
                except Exception as e:
                    self.logger.error(f"[VPS] 解析本地同步文件失败 {remote_file}: {e}")

            sftp.close()
            ssh.close()
            
            if synced_data_list:
                self.logger.info(f"[VPS] {data_type} 数据同步完成，共获取 {len(synced_data_list)} 天记录")
            # [AI-2026-10-11] 状态化：区分「有今日文件」与「无今日文件」——
            # 后者可能是正常时滞（如本机 08:00 跑流水线时 VPS 16:30 的产物还没出），**不报警**。
            today = datetime.now().strftime('%Y-%m-%d')
            has_today = any(it.get('date') == today for it in synced_data_list)
            self._vps_sync_status[data_type] = 'HAS_TODAY' if has_today else 'NO_TODAY'
            if not has_today:
                # [AI-2026-10-11] 仅记录；是否报警由各 step 的 _vps_missing_alert 判定
                #（含「交易日 + 预期时刻 + 30min 宽限」三重判断），避免非产出日误报。
                self.logger.info(f"[VPS] {data_type} 无今日({today})文件")
            return synced_data_list

        except Exception as e:
            self.logger.warning(f"[VPS] 同步失败: {e}")
            self._vps_sync_status[data_type] = 'SSH_FAIL'
            self._alert_vps_failure(data_type, f"SSH/SFTP 同步失败: {e}")
        return []

    def _alert_vps_failure(self, data_type: str, detail: str):
        """[AI-2026-10-11 东哥拍板] VPS 硬故障立即告警（SSH 连不上 / 远端目录不存在）。

        ⚠️ 「VPS 当日文件尚未落盘」不算故障（正常时滞），不在此报警 —— 由调用方按
        「期望落盘时刻 + 宽限」另行判断。告警失败绝不影响主流程。
        """
        if _send_alert is None:
            self.logger.error(f"🚨 [VPS-ALERT] {data_type}: {detail}（告警通道不可用）")
            return
        try:
            _send_alert(f"东京 VPS 同步失败 · {data_type}\n{detail}",
                        level="error", key=f"vps_fail_{data_type}")
        except Exception as e:
            self.logger.error(f"🚨 [VPS-ALERT] {data_type}: {detail}（告警发送异常: {e}）")

    def _vps_gap_alert(self, data_type: str, covered: int, expected: int):
        """[AI-2026-10-11 东哥铁律] VPS 只覆盖了一部分 → 报警，**缺口字段留空、不本地兜底**。"""
        msg = f"{data_type} VPS 覆盖不全：{covered}/{expected}"
        self.logger.warning(f"⚠️ [VPS-GAP] {msg} —— 按铁律不本地兜底，缺口字段留空")
        if _send_alert is not None:
            try:
                _send_alert(f"东京 VPS 数据覆盖不全 · {msg}", level="warn", key=f"vps_gap_{data_type}")
            except Exception as e:
                self.logger.error(f"🚨 [VPS-GAP] 告警发送异常: {e}")

    def _vps_session_day(self, data_type: str, d) -> bool:
        """d 是否为该源的「数据归属交易日」——复刻东京 VPS 各脚本内置的市场日历。

        🔴 [2026-11-09 事项3] 单一口径：`_vps_source_due_today`（告警判据）与
        `_vps_expected_trade_date`（覆盖率判据）都走这里，杜绝两处日历漂移。
        """
        ds = d.strftime('%Y-%m-%d')
        try:
            if data_type in ('shares', 'prices', 'nav'):
                # A股：market_calendar（chinese_calendar 可用时覆盖调休/法定假期）
                return is_trading_day('A_SHARE', d)
            if data_type in ('usa_prices', 'etf_nav'):
                return d.weekday() < 5 and ds not in _VPS_US_HOLIDAYS_2026
            if data_type == 'index':
                jp = d.weekday() < 5 and ds not in _VPS_JPX_HOLIDAYS_2026
                eu = d.weekday() < 5 and ds not in _VPS_EU_HOLIDAYS_2026
                return jp or eu
            if data_type == 'hssi':
                return is_trading_day('XHKG', d)
            # woody / fx / futures：VPS 脚本 cron 为 1-5 且无假日表 ⇒ 工作日必产
            return d.weekday() < 5
        except Exception as e:
            self.logger.warning(f"[VPS] {data_type} 交易日判据异常: {e}（保守按应产处理）")
            return True

    def _vps_expected_trade_date(self, data_type: str) -> str:
        """本次采集必须核对的「数据归属交易日」= 今天之前最近一个该源应产出的交易日。

        🔴 [2026-11-09 事项3 根因修复 · 东哥拍板]：
          · VPS 的 prices_/nav_/shares_ 每份文件都是**多日滚动窗口**（prices 单文件即
            5 交易日 × 52 只 = 260 行；nav 21 天 × 52 = 1040 行），usa_prices 更是
            48 标的全历史。旧判据把「所有历史文件里出现过的代码集合」当成「今日已覆盖」
            ⇒ VPS 整月停机后，任意一份旧文件仍凑得出 52 只 ⇒ 打印「✅ 已全覆盖」并
            静默 return：既不写新数据、也不报警，故障彻底隐形（10-15 起三个星期）。
          · 正解遵循 docs/002_1 §九「定型只能问来源」：覆盖率必须绑定**目标交易日**，
            只认来源行 `row.date == 目标交易日` 的记录；旧文件里再多的旧日期都不算数。
        """
        today = datetime.now().date()
        for back in range(1, 15):
            d = today - timedelta(days=back)
            if self._vps_session_day(data_type, d):
                return d.strftime('%Y-%m-%d')
        return (today - timedelta(days=1)).strftime('%Y-%m-%d')

    def _vps_coverage_window(self, data_type: str, target_date: str) -> tuple:
        """来源行日期必须落在 (floor, target_date] 才算「新鲜」，否则只是历史文件。

        滞后天数按源区分（自然日），只用来容纳真实的公布/休市时差，**不是前填**：
          · prices 2  : T+1 03:00 必产 T 日收盘，留 2 天容周末；
          · nav    4  : QDII欧美/黄金原油官方净值 T+2 公布（daily_updater:1259）；
          · usa_prices 4 / shares 4 : 美股假期 + 周末 + 交易所份额公布时差。
        ⚠️ 窗口上限固定为 target_date：早于目标日的旧行、晚于目标日的未来行都不算覆盖。
        """
        lag = {'prices': 2, 'nav': 4, 'usa_prices': 4, 'shares': 4}.get(data_type, 2)
        t = date.fromisoformat(target_date)
        return (t - timedelta(days=lag)).isoformat(), target_date

    def _vps_row_is_fresh(self, row_date: str, window: tuple) -> bool:
        floor, ceiling = window
        return bool(row_date) and floor <= row_date <= ceiling

    def _vps_coverage_ok(self, data_type: str, covered: set, expected: set,
                         target_date: str) -> bool:
        """按「目标交易日 + 来源」判覆盖率；缺新鲜数据一律按断供处理，绝不本地兜底。"""
        if not covered:
            self.logger.warning(
                f"⚠️ [VPS-STALE] {data_type} 在目标交易日 {target_date} 附近无任何新鲜数据"
                f"（历史文件不算）⇒ 判定为断供：报警 + 缺口留空，绝不本地兜底")
            self._vps_missing_alert(data_type)
            return False
        if covered.issuperset(expected):
            return True
        self._vps_gap_alert(data_type, len(covered & expected), len(expected))
        return False

    def _vps_source_due_today(self, data_type: str) -> bool:
        """[AI-2026-10-11 修误报] 该源「今天是否应有产出」。

        采集类（T+1 凌晨采 T 日数据）看「昨天」；指数/港股类（当日 16:30）看「今天」；
        woody/fx/futures 看工作日。判据统一走 `_vps_session_day`（与覆盖率同源）。
        非产出日 → 无数据属正常，**绝不报警**（周日休市被误判为故障并狂发 BOT 的根因）。
        """
        today = datetime.now().date()
        if data_type in ('index', 'hssi'):
            return self._vps_session_day(data_type, today)
        if data_type in ('woody', 'fx', 'futures'):
            return today.weekday() < 5
        return self._vps_session_day(data_type, today - timedelta(days=1))

    def _vps_missing_alert(self, data_type: str):
        """[AI-2026-10-11 东哥铁律 + 修误报] VPS 无今日数据时：

        只有「**应为产出日** 且 **已过预期时刻 + 30min 宽限**」才发 BOT 报警；
        非产出日（市场休市）或未到点 → **只记 info，绝不发 BOT**（东哥：不到时间的消息没意义）。
        两种情形都**不本地兜底**（字段留空）。
        """
        now = datetime.now()
        now_hm = now.strftime('%H:%M')
        # ① 今日非该源产出日（市场休市）→ 正常
        if not self._vps_source_due_today(data_type):
            self.logger.info(f"[VPS] {data_type} 今日为非产出日（市场休市），无数据属正常，不报警")
            return
        # ② 未到预期产出时刻（+30min 宽限）→ info，不发 BOT
        exp = VPS_EXPECTED_TIME.get(data_type)
        if exp:
            hh, mm = int(exp[:2]), int(exp[3:])
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=30)
            if now < due:
                self.logger.info(f"[VPS] {data_type} 未到预期产出时刻({exp}+30min)，现在 {now_hm}，本步留空")
                return
        # ③ 应为产出日 + 已过点(+宽限) → 先核实本地是否真缺今日文件，避免误报
        # [AI-2026-10-13] 兜底：即便上游标记/本次同步列表为空，只要本地 {data_type}_{today}.json 已存在，
        # 说明数据已落盘，绝非「无数据」，直接 return 不报警（Fix A 已让正常重跑看到今日文件，此为双保险）。
        local_sync_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       "..", "..", "..", "ArbDashboard", "data", "vps_sync"))
        today_str = now.strftime('%Y-%m-%d')
        today_file = os.path.join(local_sync_dir, f"{data_type}_{today_str}.json")
        if os.path.exists(today_file):
            self.logger.info(f"[VPS] {data_type} 本地今日文件已存在({today_file})，非缺失，不报警")
            return
        # 真异常，报警
        msg = f"{data_type} 今日无数据" + (f"（预期 {exp}，现已 {now_hm}）" if exp else "")
        self.logger.warning(f"🚨 [VPS-MISS] {msg} —— 按铁律不本地兜底，字段留空")
        if _send_alert is not None:
            try:
                _send_alert(f"东京 VPS 今日无数据 · {msg}", level="error", key=f"vps_miss_{data_type}")
            except Exception as e:
                self.logger.error(f"🚨 [VPS-MISS] 告警发送异常: {e}")

    def _try_fetch_from_vps(self, data_type='woody'):
        """保持兼容性的包装方法，仅返回当天的内容"""
        all_data = self._try_sync_all_from_vps(data_type)
        today_str = datetime.now().strftime('%Y-%m-%d')
        for item in all_data:
            if item['date'] == today_str:
                return item['content']
        return None

    def step1_and_2_fetch_woody_api(self):
        """
        步骤一 & 二：获取 Woody 数据并解析入库
        实施"安全第一"防御机制：VPS(增量追溯) -> API -> Crawler -> Stop on Failure
        """
        self.logger.info("=== 步骤一：获取 Woody 数据，步骤二：解析入库 (增量追溯模式) ===")
        today_str = datetime.now().strftime('%Y-%m-%d')
        sync_key = "woody_lof_batch"
        
        # 🛡️ 总闸检查：今日已处理过（任一通道成功）→ 跳过整步
        if self.db.is_access_synced_today(today_str, sync_key):
            self.logger.info(f"✅ 今日 Woody 因子已处理完毕（防刷标记 {sync_key} 已存在），跳过 VPS 同步与 API 请求。")
            return True

        # 🛡️ [AI-2026-10-12 东哥定调 · 修 Q1「双闸」bug] 「今日数据是否已定型」必须问**来源**，
        #   而不是问「库里有没有今日任何原始数据」。旧判据（raw_api_data 存在且是合法 JSON）的致命缺陷：
        #   凌晨流水线跑在 VPS 09:20 产出之前 ⇒ 回落 Level 1「直连 woody API」并写下当日 raw 行 ⇒
        #   09:20 的 `--refresh-morning` 虽然清了 woody_lof_batch 防刷标记，仍被这道 DB 闸短路
        #   ⇒ **东京 VPS 09:20 的权威文件永远拉不到**（10-12 实证，细节见 memory/2026-10-12.md §一）。
        #   新判据：只有「已由 VPS 通道入库」（woody_vps_ingested）才算定型；非 VPS 来源的当日数据
        #   **不阻止**后续 VPS 覆盖（save_raw_api_data 是 INSERT OR REPLACE，process() 会重写各因子表）。
        if self.db.is_access_synced_today(today_str, 'woody_vps_ingested'):
            self.logger.info(f"✅ 今日({today_str}) Woody 因子已由 VPS 通道入库，跳过。")
            self.db.mark_access_synced(today_str, sync_key)
            return True

        # 🛡️ 保留旧安全网：库里有今日原始数据但内容无效（如 "未授权IP" 错误串）⇒ 清除后重取
        try:
            conn = self.db._get_conn()
            cursor = conn.execute(
                "SELECT COUNT(*), raw_content FROM raw_api_data WHERE date = ? AND source = 'woody_lof'",
                (today_str,)
            )
            row = cursor.fetchone()
            count = row[0] if row else 0
            raw_content = row[1] if row else ""
            conn.close()
            if count > 0 and not (raw_content and raw_content.strip().startswith('{')):
                self.logger.warning(f"⚠️ 今日({today_str}) Woody 原始数据在库中但内容无效（非JSON），清除后重新获取...")
                conn2 = self.db._get_conn()
                conn2.execute("DELETE FROM raw_api_data WHERE date = ? AND source = 'woody_lof'", (today_str,))
                conn2.commit()
                conn2.close()
            elif count > 0:
                self.logger.info(
                    f"ℹ️ 今日({today_str}) 库内有 Woody 原始数据但非 VPS 来源（可能为凌晨直连 API 抢跑），"
                    f"继续走 VPS 同步，让权威文件覆盖它。")
        except Exception as e:
            self.logger.warning(f"检查 Woody 数据库时出错: {e}，继续连接VPS获取")

        # [AI-2026-10-11 东哥定调] woody 抓取范围改以 **DB 权威**：读 unified_fund_list.data_source='woody'。
        #   该列由 lof_config.yaml 的 data_source 字段同步而来（sync_unified_fund_list），
        #   DB 为唯一真源，YAML 降格为退化兜底（与 step5 的 symbol_master 同源手法，见本文件 :1188）。
        #   仅当 DB 列缺失/读取失败时才回退 yaml，保证不丢抓取能力。
        codes = []
        db_rows = None
        try:
            _c = self.db._get_conn()
            try:
                db_rows = _c.execute(
                    "SELECT fund_code FROM unified_fund_list "
                    "WHERE data_source = 'woody' AND fund_code != '161226' ORDER BY fund_code"
                ).fetchall()
            finally:
                _c.close()
        except Exception as e:
            self.logger.warning(f"⚠️ [Woody] unified_fund_list 读取失败，退化用 YAML: {e}")
            db_rows = None
        if db_rows is not None:
            codes = [str(r[0]) for r in db_rows if r[0]]
        else:
            # 退化：老 YAML 逻辑
            codes = [
                str(fund.get('code', ''))
                for fund in self.config.get('funds', [])
                if str(fund.get('code', '')) != '161226' and fund.get('data_source') == 'woody'
            ]
        # 数据目录迁移到 ArbDashboard/data/（与脚本目录解耦）
        backup_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "ArbDashboard", "data", "woodyAPI"))
        
        # Level 0: VPS Siphon (支持多日历史自动补全)
        vps_history_data = self._try_sync_all_from_vps('woody')
        vps_today_success = False
        
        if vps_history_data:
            self.logger.info(f"🔄 [VPS] 发现 {len(vps_history_data)} 份历史因子数据，正在逐一入库解析...")
            for item in vps_history_data:
                file_date = item['date']
                content = item['content']

                # 🛡️ [AI-2026-10-12 修 Q1 双闸] 判据改为「该日是否**已由 VPS 入库**」（woody_vps_ingested）。
                #   旧判据用 fund_daily_factors 是否有行 ⇒ 会把凌晨 Level 1 直连 API 抢跑写下的当日数据
                #   当成「已解析」而跳过 VPS 文件 ⇒ 权威数据进不来。现在非 VPS 来源的当日行会被 VPS 覆盖重解析。
                #   （注：woody_vps_sync 只表示「文件已下载/已读取」，故不能拿它当入库判据。）
                if self.db.is_access_synced_today(file_date, 'woody_vps_ingested'):
                    self.logger.info(f"   ⏭️ [VPS] 日期 {file_date} 已由 VPS 入库，跳过")
                    if file_date == today_str:
                        vps_today_success = True
                    continue

                try:
                    # 提取真实内容 (Woody API 包装在 text 字段里)
                    api_content = content.get('text') if isinstance(content, dict) else content
                    if api_content:
                        # 1. 存入本地原始数据湖 (Data Lake)（INSERT OR REPLACE，可覆盖非 VPS 来源的当日行）
                        raw_json_str = json.dumps(api_content, ensure_ascii=False, indent=2)
                        self.db.save_raw_api_data(date=file_date, source='woody_lof', raw_content=raw_json_str)
                        
                        # 2. 调用核心解析引擎提取因子
                        processed_data = WoodyAPIService.process(self.db, api_content, source_id='woody_lof')
                        if processed_data:
                            self.logger.info(f"   ✅ [VPS] 日期 {file_date} 的因子解析成功")
                            # [性能优化] 标记该日期已处理，下次不再重复解析
                            self.db.mark_access_synced(file_date, sync_key)
                            self.db.mark_access_synced(file_date, 'woody_vps_sync')
                            self.db.mark_access_synced(file_date, 'woody_vps_ingested')  # ← 来源标记（权威通道）
                            if file_date == today_str:
                                vps_today_success = True
                except Exception as e:
                    self.logger.error(f"   ❌ [VPS] 解析日期 {file_date} 数据时出错: {e}")
        
        # 如果 VPS 已经搞定了今天的数据，直接打标并收工
        if vps_today_success:
            self.db.mark_access_synced(today_str, sync_key)
            self.logger.info(f"✅ [VPS] 今日数据已通过云端同步完成，已标记 {sync_key} 成功。")
            return True

        # [AI-2026-08-02] 展示副本（ARM）：woody 因子只读东京 VPS，不走 API 直采 / 网页爬虫，
        # 也不触发"拿不到今日数据即熔断"。理由：ARM 是暴露在公网的纯展示副本，
        # 东京主程序才是 woody 的唯一真源；东京当日产物尚未落盘时（东京固定 09:20 落盘），
        # 展示副本沿用 T-1 因子是可接受的，熔断只会让整条流水线白停。
        if DASHBOARD_MODE:
            if vps_history_data:
                self.logger.info(f"🖥️ [DASHBOARD_MODE] woody 因子已从东京同步 {len(vps_history_data)} 天，跳过 API/爬虫兜底与熔断")
            else:
                self.logger.warning("🖥️ [DASHBOARD_MODE] 东京暂无新增 woody 因子（可能当日尚未落盘），沿用库内既有因子，不熔断")
            return True

        # Level 1: 实时 API (确保拿回最新的，或者作为 VPS 失败后的备用源)
        try:
            self.logger.info("🛡️ [Level 1] 尝试通过实时 API 刷新今日因子...")
            success = WoodyAPIService.fetch_and_process(self.db, codes, backup_dir, source_id='woody_lof')
            if success:
                self.logger.info("✅ [Level 1] API 今日实时数据同步成功")
                return True
        except Exception as e:
            self.logger.warning(f"⚠️ [Level 1] API 尝试失败: {e}")

        # Level 2: Web Crawler（无 woody API key 的降级通道，群友免 key 爬网页拿因子）
        # 东哥拍板保留：群里多数人无付费 API key，这是他们的命根子。
        # 2026-08-02 实测：B/C 页【免登录】即可拿 仓位/校准/价格/权重；仅 step6 区域价历史需登录。
        try:
            self.logger.info("🛡️ [Level 2] 触发网页爬虫补位（无 woody API key 降级通道）...")
            self._login_woody_if_needed()  # 网站账号登录：B/C 免登录无害；仅 step6/A页需登录时才有用

            backup = self.woody_crawler.get_woody_backup_data(self.config)
            if not backup:
                self.logger.error("❌ [Level 2] 爬虫未从 C 页返回任何基金因子数据")
            else:
                merged = {}
                for fund_key, fd in backup.items():
                    bare_code = fund_key[2:] if fund_key[:2].upper() in ('SZ', 'SH') else fund_key
                    # (b) 合并 B 页权重：C 页无权重列，权重只在 holdingscn（旧比例%）
                    weights = self.woody_crawler.get_woody_holdings_data(bare_code) or []
                    wmap = {}
                    for w in weights:
                        s = str(w['symbol'])
                        # B 页区域变种不带 ^ 前缀（如 GLD-EU），与 C 页 ^GLD-EU 对齐
                        if ('-JP' in s or '-EU' in s or '-HK' in s) and not s.startswith('^'):
                            s = '^' + s
                        try:
                            wmap[s] = float(w['weight'])
                        except (ValueError, TypeError):
                            pass
                    sh = fd.get('symbol_hedge', {}) or {}
                    # 填权重
                    for etf, info in sh.items():
                        try:
                            info['ratio'] = float(wmap.get(etf, info.get('ratio', 0) or 0))
                        except (ValueError, TypeError):
                            info['ratio'] = 0.0
                    # 若 B 页有权重但 C 页没价格（罕见），以 B 页建壳，确保权重入库
                    for etf, w in wmap.items():
                        if etf not in sh:
                            try:
                                sh[etf] = {'price': 0.0, 'ratio': float(w)}
                            except (ValueError, TypeError):
                                pass
                    fd['symbol_hedge'] = sh
                    merged[fund_key] = fd

                # 复用 process() 一次写 6 张表（与 API 路径完全对齐）
                WoodyAPIService.process(self.db, merged, 'woody_lof')
                # 落原始数据湖 + 防刷标，避免次日重复爬
                raw_json = json.dumps(merged, ensure_ascii=False, indent=2)
                self.db.save_raw_api_data(date=today_str, source='woody_lof', raw_content=raw_json)
                self.db.mark_access_synced(today_str, sync_key)
                self.logger.info(f"✅ [Level 2] 网页爬虫补位成功，已写入 {len(merged)} 只基金的因子（仓位/校准/价格/权重）")
                return True
        except Exception as e:
            self.logger.error(f"❌ [Level 2] 网页爬虫补位失败: {e}", exc_info=True)

        # 🛑 安全熔断：拒绝使用 T-1 历史数据
        error_msg = "🚨 [致命错误] 无法获取今日最新的 Woody 因子数据！为防止估值失真导致误判，系统已启动安全熔断，停止后续流水线。"
        self.logger.error("-" * 60)
        self.logger.error(error_msg)
        self.logger.error("👉 建议检查项：1. VPN 是否已彻底关闭？ 2. 网络是否连通？ 3. Woody 网站是否正常？")
        self.logger.error("-" * 60)
        
        # 直接抛出异常，强制停止程序运行
        raise RuntimeError("Woody 因子获取失败，流水线安全中止。")

    def step2_5_sync_yaml_with_latest_factors(self):
        """步骤2.5：[AI-2026-08-16] 废弃反写 lof_config.yaml，仅保留观测日志。

        篮子权重/仓位已以数据库(fund_basket_weights / fund_daily_factors)为唯一权威，
        估值引擎(static/dynamic)直接读 DB，不再依赖 yaml 篮子镜像。
        原反写机制每天把 DB 最新篮子写回 yaml，但 yaml 会被 git checkout/commit/迁移
        反复冲掉，形成双重真相源（如 160216 的 XLE vs XOP 错配 → 误差 +30%）。
        故本步骤改为只打印 DB 最新因子，不再写任何 yaml 字段。
        """
        self.logger.info("=== 步骤2.5：观测 DB 最新因子（篮子权重/仓位，不反写 yaml）===")
        try:
            conn = self.db._get_conn()
            for fund in self.config.get('funds', []):
                code = str(fund.get('code', ''))
                if not code:
                    continue

                # 观测最新仓位
                pos_df = pd.read_sql(
                    "SELECT position FROM fund_daily_factors WHERE fund_code=? ORDER BY date DESC LIMIT 1",
                    conn, params=(code,)
                )
                db_pos = None
                if not pos_df.empty and pd.notna(pos_df.iloc[0]['position']):
                    db_pos = float(pos_df.iloc[0]['position'])

                # 观测最新篮子权重
                weight_df = pd.read_sql(
                    "SELECT underlying_symbol, weight FROM fund_basket_weights "
                    "WHERE fund_code=? AND date=(SELECT MAX(date) FROM fund_basket_weights WHERE fund_code=?)",
                    conn, params=(code, code)
                )
                if weight_df.empty:
                    continue
                db_weights = {
                    row['underlying_symbol'].replace('^', ''): float(row['weight'])
                    for _, row in weight_df.iterrows() if pd.notna(row['weight'])
                }

                # 对比 yaml 当前篮子，打印差异（仅观测，不改 yaml）
                yaml_syms = set()
                for port_key in ['valuation_portfolio', 'hedging_portfolio']:
                    if port_key in fund:
                        yaml_syms.update(
                            item.get('symbol', '').replace('^', '') for item in fund[port_key]
                        )
                added = set(db_weights) - yaml_syms
                removed = yaml_syms - set(db_weights)
                if added or removed:
                    self.logger.info(
                        f"🔎 [{code}] DB最新篮子(权威): "
                        + ", ".join(f"{s}={db_weights[s]:.2f}%" for s in sorted(db_weights, key=lambda x: -db_weights[x]))
                    )
                    if added:
                        self.logger.info(f"   ➕ 成分({sorted(added)}) DB有而yaml无")
                    if removed:
                        self.logger.info(f"   ➖ 成分({sorted(removed)}) yaml有而DB已无")
                    if db_pos is not None:
                        self.logger.info(f"   📌 DB最新仓位: {db_pos}")
            conn.close()
            self.logger.info("✅ 步骤2.5 观测完成（已不反写 yaml，篮子/仓位以 DB 为唯一权威）。")
        except Exception as e:
            self.logger.error(f"❌ 观测 DB 因子失败: {e}")

    def step3_fetch_exchange_rate(self):
        """[AI-2026-07-08] 步骤三：抓取汇率（中间价/在岸价/离岸价）存入库，2 级降级：VPS 同步 → 本地直连备用源"""
        self.logger.info("=== 步骤三：抓取汇率（中间价/在岸价/离岸价） ===")
        today_str = datetime.now().strftime('%Y-%m-%d')

        # 🛡️ 数据库检查：仅当今日三个汇率字段（中间价/在岸价/离岸价）都已齐备时才跳过
        # [AI-2026-07-08] 改为三字段全齐才跳过，避免"中间价有了但 spot/cnh 永远不补"的漏洞
        try:
            conn = self.db._get_conn()
            row = conn.execute(
                "SELECT usd_cny_mid, usd_cny_spot, usd_cnh FROM exchange_rate WHERE date = ?",
                (today_str,)
            ).fetchone()
            conn.close()
            if row and row[0] is not None and row[1] is not None and row[2] is not None:
                self.logger.info(f"✅ 今日({today_str})三汇率已齐备，无需连VPS，直接跳过。")
                self.db.mark_access_synced(today_str, source='official_exchange_rate')
                return
        except Exception as e:
            self.logger.warning(f"检查汇率数据库时出错: {e}，继续连接VPS获取")

        # Level 0: 尝试从 VPS 增量同步汇率数据并入库
        vps_fx_data = self._try_sync_all_from_vps('fx')
        if vps_fx_data:
            self.logger.info(f"🔄 [VPS] 发现 {len(vps_fx_data)} 份历史汇率数据，正在同步入库...")
            for item in vps_fx_data:
                file_date = item['date']
                content = item['content']
                try:
                    # [AI-2026-07-08] 读取全部汇率字段（中间价/在岸价/离岸价）
                    # [AI-2026-09-24 根因修复] 补读 jpy_cny_mid：VPS 的 fx 文件本就含该字段
                    #  （与 usd/hkd 同源），但此前入库只取 4 个键、从未读 jpy；而 ARM 是
                    #  DASHBOARD_MODE 又跳过日元中间价直连 ⇒ 两路落空，jpy_cny_mid 自 09-21
                    #  起在 ARM 永久为 NULL。upsert 是逐字段合并，传 None 不会清空已有值。
                    date_info = content.get('date')
                    usd_val = content.get('usd_cny_mid')
                    hkd_val = content.get('hkd_cny_mid')
                    jpy_val = content.get('jpy_cny_mid')
                    eur_val = content.get('eur_cny_mid')
                    cny_spot_val = content.get('usd_cny_spot')
                    cnh_val = content.get('usd_cnh')
                    # 中间价缺失则尝试用其他汇率备用源日期，避免整条跳过
                    if date_info and (usd_val or cny_spot_val or cnh_val):
                        date_info_str = pd.to_datetime(str(date_info)).strftime('%Y-%m-%d')
                        self.db.upsert_exchange_rate(
                            date_info_str,
                            usd_cny_mid=usd_val,
                            hkd_cny_mid=hkd_val,
                            jpy_cny_mid=jpy_val,
                            eur_cny_mid=eur_val,
                            usd_cny_spot=cny_spot_val,
                            usd_cnh=cnh_val,
                        )
                        self.logger.info(f"   ✅ [VPS] 同步入库汇率: {date_info_str} -> USD:{usd_val}, HKD:{hkd_val}, JPY:{jpy_val}, EUR:{eur_val}, CNYSpot:{cny_spot_val}, CNH:{cnh_val}")
                        # 标记云端文件在此日期已完成同步
                        self.db.mark_access_synced(file_date, 'fx_vps_sync')
                except Exception as e:
                    self.logger.error(f"   ❌ [VPS] 解析日期 {file_date} 汇率时出错: {e}")

        # [AI-2026-08-17] 治本修复：东京 VPS 的 fx 文件只带中间价(usd_cny_mid/hkd_cny_mid)，
        #   不含在岸价(usd_cny_spot/jpy_cny_spot)。原 DASHBOARD_MODE 在此整体 return，
        #   导致 ARM 在岸价永远停在 8-07、维护报告误报"H5滞后"。
        #   修正：DASHBOARD_MODE 下仍本地直连补抓在岸价(USD/JPY spot)，
        #   仅跳过中间价/离岸价/日元中间价（由 VPS 提供，避免双源不一致）。
        #   不碰"VPS 不采集在岸价"铁律——ARM 侧 spot 本就由 ARM 自抓新浪，与设计一致。
        # [AI-2026-09-24] 注：日元中间价(jpy_cny_mid)已由上方 VPS fx 入库循环补读（见 jpy_val），
        #   此处仍跳过直连以维持"中间价统一由 VPS 供数"的单一真相源；不再依赖 ARM 本地直连。
        if DASHBOARD_MODE:
            self.logger.info("🖥️ [DASHBOARD_MODE] 跳过中间价/离岸价/日元中间价直连（VPS 提供），仅补抓在岸价(USD/JPY spot)")
            self._fetch_spot_rates(today_str)
            return

        # [AI-2026-08-17] DASHBOARD_MODE 下跳过中间价直连（由 VPS 提供），只在岸价走本地
        if not DASHBOARD_MODE:
            # 检查今天是否已经同步到最新的汇率
            if self.db.is_access_synced_today(today_str, source='official_exchange_rate'):
                self.logger.info("✅ 今日已同步过人民币中间价，跳过实时抓取。")
                return

            # Level 1: 实时抓取作为备用源（[AI-2026-07-08] 单级备用源补全：中间价 + 在岸价 + 离岸价 一次抓全）
            self.logger.info("📡 [Level 1] 尝试实时抓取人民币中间价/在岸价/离岸价...")
            from arbcore.fetchers.data_fetcher import data_fetcher
            exchange_rate_data = data_fetcher.fetch_official_exchange_rate()
            if exchange_rate_data:
                date_info = exchange_rate_data.get('日期')
                usd_val = exchange_rate_data.get('usd_cny_mid')
                hkd_val = exchange_rate_data.get('hkd_cny_mid')
                eur_val = exchange_rate_data.get('eur_cny_mid')
                # [AI-2026-10-08 东哥] 日志补 JPY：同一次 chinamoney 抓取已带 jpy_cny_mid，
                # 原日志只打 USD/HKD/EUR。**仅取变量用于打印，不改下方 upsert 行为**——
                # JPY 仍由本函数后段（has_jpy 分支）单独 upsert，避免在此重复写库打乱既有判据。
                jpy_val = exchange_rate_data.get('jpy_cny_mid')
                # [A根因修复 2026-09-18] 中间价尚未发布(usd/hkd为空)时，绝不 upsert 今天行
                # 避免建出"中间价=NULL"的脏行；保留已有最新行，待 9:15 后官方发布再补
                if date_info and usd_val is not None and hkd_val is not None:
                    try:
                        date_info_str = pd.to_datetime(str(date_info)).strftime('%Y-%m-%d')
                        self.db.upsert_exchange_rate(date_info_str, usd_cny_mid=usd_val, hkd_cny_mid=hkd_val, eur_cny_mid=eur_val)
                        self.logger.info(f"✅ 人民币中间价入库: {date_info_str} -> USD:{usd_val}, HKD:{hkd_val}, JPY:{jpy_val}, EUR:{eur_val}")

                        # 关键修复：只有当抓取到的汇率实际生效日期是今天（或更晚）时，才标记今日已同步
                        # 如果抓到的是昨天的日期，说明今天最新的还没更新，我们绝不标记今日同步，以便稍后重试
                        if date_info_str >= today_str:
                            self.db.mark_access_synced(today_str, source='official_exchange_rate')
                            self.logger.info(f"✅ 成功获取到今日 ({today_str}) 最新汇率，已打标。")
                        else:
                            self.logger.warning(f"⚠️ 抓取到的汇率日期为过去日期 ({date_info_str})，未更新到今天，因此不标记今日已同步。")
                    except Exception as e:
                        self.logger.error(f"❌ 本地汇率解析异常: {e}")
                elif date_info:
                    self.logger.warning(f"⚠️ 官方中间价今日尚未发布(usd/hkd为空)，跳过 upsert，保留最新已有行；待 9:15 后重试")

        # [AI-2026-08-17] 在岸价(USD/JPY spot)——抽成 _fetch_spot_rates，DASHBOARD_MODE 与非 DASHBOARD_MODE 共用
        self._fetch_spot_rates(today_str)
        # [AI-2026-08-17] DASHBOARD_MODE 下跳过离岸价直连（VPS 提供），只在岸价走本地
        if not DASHBOARD_MODE:
            # 离岸价 CNH
            try:
                conn_cnh = self.db._get_conn()
                has_cnh = conn_cnh.execute(
                    "SELECT COUNT(*) FROM exchange_rate WHERE date = ? AND usd_cnh IS NOT NULL", (today_str,)
                ).fetchone()[0] > 0
                conn_cnh.close()
                if not has_cnh:
                    cnh_data = data_fetcher.fetch_cnh_offshore_rate()
                    if cnh_data:
                        cnh_date = pd.to_datetime(str(cnh_data.get('日期', today_str))).strftime('%Y-%m-%d')
                        cnh_rate = cnh_data.get('离岸价')
                        if cnh_rate is not None:
                            self.db.upsert_exchange_rate(cnh_date, usd_cnh=cnh_rate)
                            self.logger.info(f"✅ [Level 1] 离岸价 CNH 入库: {cnh_date} -> {cnh_rate}")
            except Exception as e:
                self.logger.error(f"❌ [Level 1] 离岸价直连备用源失败: {e}")

        # [AI-2026-08-17] DASHBOARD_MODE 下跳过日元中间价直连（VPS 提供），只在岸价走本地
        if not DASHBOARD_MODE:
            # [AI-2026-07-23] JPY/CNY 日元汇率——优先使用国家外汇管理局中间价
            # [AI-2026-07-10] 原新浪在岸价仅作备用源
            try:
                conn_jpy = self.db._get_conn()
                has_jpy = conn_jpy.execute(
                    "SELECT COUNT(*) FROM exchange_rate WHERE date = ? AND jpy_cny_mid IS NOT NULL", (today_str,)
                ).fetchone()[0] > 0
                conn_jpy.close()
                if not has_jpy:
                    # 优先从国家外汇管理局中间价获取（与 USD/HKD 同源）
                    official_rates = data_fetcher.fetch_official_exchange_rate()
                    jpy_rate = official_rates.get('jpy_cny_mid') if official_rates else None
                    jpy_date = official_rates.get('日期', today_str) if official_rates else today_str
                    if jpy_rate is not None:
                        jpy_date_str = pd.to_datetime(str(jpy_date)).strftime('%Y-%m-%d')
                        self.db.upsert_exchange_rate(jpy_date_str, jpy_cny_mid=jpy_rate)
                        self.logger.info(f"✅ [Level 1] JPY/CNY 中间价入库: {jpy_date_str} -> {jpy_rate}")
                    else:
                        # 备用源：新浪在岸价
                        jpy_data = data_fetcher.fetch_jpy_cny_rate()
                        if jpy_data:
                            jpy_date = pd.to_datetime(str(jpy_data.get('日期', today_str))).strftime('%Y-%m-%d')
                            jpy_rate = jpy_data.get('jpy_cny_rate')
                            if jpy_rate is not None:
                                self.db.upsert_exchange_rate(jpy_date, jpy_cny_mid=jpy_rate)
                                self.logger.info(f"✅ [Level 1] JPY/CNY 备用源入库(新浪): {jpy_date} -> {jpy_rate}")
            except Exception as e:
                self.logger.error(f"❌ [Level 1] JPY/CNY 直连备用源失败: {e}")

        self.logger.info(f"✅ 步骤三完成：今日({today_str})汇率（中间价/在岸价/离岸价/日元）采集结束。")

    def _fetch_spot_rates(self, today_str):
        """[AI-2026-08-17] 在岸价(USD/JPY spot)抓取——DASHBOARD_MODE 与非 DASHBOARD_MODE 共用。
        东京 VPS 的 fx 文件不含在岸价，故展示副本(ARM)也需本地直连新浪补抓，避免维护报告误报滞后。
        其余汇率(中间价/离岸价/日元中间价)由 VPS 提供，不在本方法内。"""
        from arbcore.fetchers.data_fetcher import data_fetcher
        # 在岸价 USDCNY
        try:
            conn_spot = self.db._get_conn()
            has_spot = conn_spot.execute(
                "SELECT COUNT(*) FROM exchange_rate WHERE date = ? AND usd_cny_spot IS NOT NULL", (today_str,)
            ).fetchone()[0] > 0
            conn_spot.close()
            if not has_spot:
                spot_data = data_fetcher.fetch_cny_spot_rate()
                if spot_data:
                    spot_date = pd.to_datetime(str(spot_data.get('日期', today_str))).strftime('%Y-%m-%d')
                    spot_rate = spot_data.get('人民币在岸价')
                    if spot_rate is not None:
                        self.db.upsert_exchange_rate(spot_date, usd_cny_spot=spot_rate)
                        self.logger.info(f"✅ [Level 1] 在岸价 USDCNY 入库: {spot_date} -> {spot_rate}")
        except Exception as e:
            self.logger.error(f"❌ [Level 1] 在岸价直连备用源失败: {e}")
        # [AI-2026-08-02] JPY/CNY 在岸价——ETF 实时估值 fx_base 用（与 USD 在岸价同管道、同时点 9:20 清晨刷新）
        # 铁律：ETF 实时估值两端都在岸价；LOF 两端都中间价；静态估值一律中间价。QDII日本 4 只都是 ETF。
        try:
            conn_jpy_spot = self.db._get_conn()
            has_jpy_spot = conn_jpy_spot.execute(
                "SELECT COUNT(*) FROM exchange_rate WHERE date = ? AND jpy_cny_spot IS NOT NULL", (today_str,)
            ).fetchone()[0] > 0
            conn_jpy_spot.close()
            if not has_jpy_spot:
                jpy_spot_data = data_fetcher.fetch_jpy_cny_spot_rate()
                if jpy_spot_data:
                    jpy_spot_date = pd.to_datetime(str(jpy_spot_data.get('日期', today_str))).strftime('%Y-%m-%d')
                    jpy_spot_rate = jpy_spot_data.get('jpy_cny_spot')
                    if jpy_spot_rate is not None:
                        self.db.upsert_exchange_rate(jpy_spot_date, jpy_cny_spot=jpy_spot_rate)
                        self.logger.info(f"✅ [Level 1] JPY/CNY 在岸价入库: {jpy_spot_date} -> {jpy_spot_rate}")
        except Exception as e:
            self.logger.error(f"❌ [Level 1] JPY/CNY 在岸价直连失败: {e}")
        # [AI-2026-10-07] EUR/CNY 在岸价——QDII欧洲 德/法 ETF 实时估值 fx_base 用（与 USD/JPY 同管道）
        try:
            conn_eur_spot = self.db._get_conn()
            has_eur_spot = conn_eur_spot.execute(
                "SELECT COUNT(*) FROM exchange_rate WHERE date = ? AND eur_cny_spot IS NOT NULL", (today_str,)
            ).fetchone()[0] > 0
            conn_eur_spot.close()
            if not has_eur_spot:
                eur_spot_data = data_fetcher.fetch_eur_cny_spot_rate()
                if eur_spot_data:
                    eur_spot_date = pd.to_datetime(str(eur_spot_data.get('日期', today_str))).strftime('%Y-%m-%d')
                    eur_spot_rate = eur_spot_data.get('eur_cny_spot')
                    if eur_spot_rate is not None:
                        self.db.upsert_exchange_rate(eur_spot_date, eur_cny_spot=eur_spot_rate)
                        self.logger.info(f"✅ [Level 1] EUR/CNY 在岸价入库: {eur_spot_date} -> {eur_spot_rate}")
        except Exception as e:
            self.logger.error(f"❌ [Level 1] EUR/CNY 在岸价直连失败: {e}")

    def _safe_save_fund_data(self, date_str, fund_code, price=None, nav=None, trade_volume=None, volume=None):
        """
        [AI-2026-06-28] premium 计算按基金分类分支：
          - QDII欧美 / 黄金原油（美股/期货有时差）→ T价 / T-1净值
          - QDII亚洲 / QDII日本 / 国内LOF（无隔夜时差）→ T价 / T净值
        [AI-2026-08-06] 修复此前统一用 T-1 净值的错误，对齐 AGENTS.md TOP 2 铁律。
        """
        conn = self.db._get_conn()
        row = None
        category = None
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT price, nav FROM unified_fund_history WHERE date=? AND fund_code=?", (date_str, fund_code))
            row = cursor.fetchone()
            # [AI-2026-06-28] 查 T-1 净值用于溢价计算
            cursor.execute("SELECT nav FROM unified_fund_history WHERE fund_code=? AND date<? AND nav IS NOT NULL ORDER BY date DESC LIMIT 1", (fund_code, date_str))
            t1_row = cursor.fetchone()
            # [AI-2026-08-06] 查基金分类以确定溢价口径
            cursor.execute("SELECT category FROM unified_fund_list WHERE fund_code=?", (fund_code,))
            cat_row = cursor.fetchone()
            category = cat_row[0] if cat_row else None
        finally:
            conn.close()

        exist_price = row[0] if row and row[0] is not None else None
        exist_nav = row[1] if row and row[1] is not None else None
        t1_nav = float(t1_row[0]) if t1_row and t1_row[0] is not None else None

        new_price = price if price is not None else exist_price
        new_nav = nav if nav is not None else exist_nav

        # [AI-2026-08-06] 按分类选分母：有时差的用 T-1 净值，无的用 T 净值
        premium = None
        if new_price is not None:
            if category in ('QDII欧美', '黄金原油') and t1_nav is not None and t1_nav > 0:
                premium = round((float(new_price) - t1_nav) / t1_nav * 100, 4)
            elif new_nav is not None and float(new_nav) > 0:
                premium = round((float(new_price) - float(new_nav)) / float(new_nav) * 100, 4)
            elif t1_nav is not None and t1_nav > 0:
                # 无当日净值时的降级：用 T-1（仅备用，不掩盖缺失）
                premium = round((float(new_price) - t1_nav) / t1_nav * 100, 4)
            
        # [AI-2026-07-31] 净值日期一并落库：本表约定「行内 nav 即 date 当日净值」
        # （东财 nav_df 每行自带日期，date_str 就是净值日期；此前只写 nav 不写 nav_date 属遗漏）
        self.db.save_unified_history(
            date_str=date_str,
            fund_code=fund_code,
            price=new_price,
            nav=new_nav,
            nav_date=date_str if new_nav is not None else None,
            premium=premium,
            trade_volume=trade_volume,
            # [FIX 2026-09-11] 成交额(万元) = 成交量(手)×100×收盘 / 10000 = trade_volume×price/100。
            # 腾讯 qfq 日K线仅给成交量(手)无成交额字段，故由成交量×收盘反算。此前某次重构把 volume 参数
            # 整段删掉，导致 2026-08-21 之后每日行的成交额恒为 NULL（主看板盘后空白）。
            volume=volume
        )

    def _step4_fix_holiday_prices(self, codes_to_fix=None):
        """[AI-2026-06-28] 用腾讯历史 K 线修复假期导致的错误收盘价"""
        from arbcore.fetchers.historical.tencent import TencentHistoricalFetcher
        self.logger.info("🔧 [假期修复] 开始用腾讯历史 K 线修复错误收盘价...")
        
        if codes_to_fix is None:
            # 修复所有基金（不只是 NULL 的）
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT fund_code FROM unified_fund_list ORDER BY fund_code")
            codes_to_fix = [str(r[0]) for r in cursor.fetchall()]
            conn.close()
        
        self.logger.info(f"🔧 [假期修复] 共 {len(codes_to_fix)} 只基金待检查...")
        
        fetcher = TencentHistoricalFetcher()
        fixed_count = 0
        corrected_count = 0
        
        for code in codes_to_fix:
            try:
                # 腾讯代码格式: sz159518 或 sh513350
                tx_code = f"sz{code}" if code.startswith(('0', '1', '3')) else f"sh{code}"
                df = fetcher.fetch_prices(tx_code, start_date='2026-06-01')
                
                if df.empty:
                    continue
                
                # [AI-2026-06-28] 用 _safe_save_fund_data 直接写入 unified_fund_history
                need_update = False
                for _, row in df.iterrows():
                    date_str = row['date'].strftime('%Y-%m-%d')
                    price = float(row['close'])
                    # 检查是否已有数据
                    conn = self.db._get_conn()
                    existing = conn.execute("SELECT price FROM unified_fund_history WHERE date=? AND fund_code=?", (date_str, code)).fetchone()
                    conn.close()
                    
                    should_update = False
                    if existing:
                        if existing[0] is None:
                            should_update = True
                        elif abs(float(existing[0]) - price) > 0.0001:
                            should_update = True
                    else:
                        should_update = True
                    
                    if should_update:
                        # [AI-2026-06-28] 直接写入 unified_fund_history（废弃 fund_data）
                        self._safe_save_fund_data(date_str=date_str, fund_code=code, price=price)
                        need_update = True
                
                if need_update:
                    fixed_count += 1
                    self.logger.info(f"  ✅ [{code}] 修复历史价格")
                else:
                    self.logger.debug(f"  ⏭️ [{code}] 价格正确，无需修复")
            except Exception as e:
                self.logger.error(f"  ❌ [{code}] 修复失败: {e}")
        
        self.logger.info(f"🔧 [假期修复] 完成，修复 {fixed_count}/{len(codes_to_fix)} 只基金")

    def _step4_fetch_prices(self, *, local_kline_only: bool = False):
        """[AI-2026-07-28] 获取各基金官方收盘价 —— 两条**互斥的时序通道**（docs/002_1 §六/§十）

        [2026-11-09 方案A·东哥拍板] 恢复盘后主通道。二者**不是兜底关系，是时序分工**：

        ① ``local_kline_only=True`` —— 盘后 15:00 ``--daily-close`` 专用（**必要通道**）。
           VPS 的 ``prices_{T}.json`` 要 **T+1 03:00** 才产出（docs/002_1 §二），
           此刻 VPS 手里最多只有 T-1 的价；而 ``main.py::_after_hours_close_price_loop``
           判据要的是 ``date=T`` 的 price ⇒ 若走 VPS-first，覆盖率判据再准也判不出
           T 日数据，必然每 5 分钟空转到次日凌晨（本次故障的直接表现）。
           故本通道**完全不看 VPS**，直连腾讯/新浪日K写 T 日官方收盘。
           次日凌晨流水线（``local_kline_only=False``）拉到 VPS 权威文件后由
           ``_safe_save_fund_data`` upsert 覆盖 ⇒ 最终态仍以 VPS 为**唯一真相源**，
           本地只是"提前可见"，不制造第二个真相源。

        ② ``local_kline_only=False``（默认）—— 凌晨 / 完整流水线，VPS-first + 10-11 铁律：
           部分覆盖或断供一律「报警 + 留空」，**绝不本地兜底**。
        """
        self.logger.info("=== 步骤4P：获取各基金收盘价 ===")
        today_str = datetime.now().strftime('%Y-%m-%d')
        conn_ufl = self.db._get_conn()
        all_codes = [str(r[0]) for r in conn_ufl.execute("SELECT fund_code FROM unified_fund_list").fetchall() if r[0]]
        conn_ufl.close()

        # ── 通道①：盘后公共日K（必要通道，非兜底） ──────────────────────────
        if local_kline_only:
            self.logger.info(
                f"📋 [方案A·盘后公共日K] 共 {len(all_codes)} 只基金，"
                f"直连腾讯/新浪写 {today_str} 官方收盘（VPS 的该日文件 T+1 03:00 才产）")
            return self._fetch_prices_from_public_kline(all_codes, today_str)

        # ── 通道②：VPS-first（凌晨 / 完整流水线，铁律不变） ───────────────────
        self.logger.info(f"📋 共 {len(all_codes)} 只基金，走 VPS 优先通道...")

        # [AI-2026-10-10] VPS优先：先尝试从东京VPS拉取当日收盘价（prices_{date}.json），
        # 若全覆盖则直接返回，减少本地直连腾讯/新浪的请求量。
        vps_prices = self._try_sync_all_from_vps('prices')
        if vps_prices:
            # [2026-11-09 事项3] 全历史照常入库（增量补历史），但**覆盖率只认目标交易日**
            target_date = self._vps_expected_trade_date('prices')
            window = self._vps_coverage_window('prices', target_date)
            vps_codes_covered = set()
            for entry in vps_prices:
                content = entry.get('content', {})
                for row in content.get('rows', []):
                    code_v = str(row.get('code', ''))
                    date_v = str(row.get('date', ''))[:10]
                    close_v = row.get('close')
                    vol_v = row.get('volume', 0)
                    amt_v = row.get('amount_wan', 0)
                    if code_v and date_v and close_v and close_v > 0:
                        self._safe_save_fund_data(date_str=date_v, fund_code=code_v, price=close_v, trade_volume=vol_v, volume=amt_v)
                        if self._vps_row_is_fresh(date_v, window):
                            vps_codes_covered.add(code_v)
            if self._vps_coverage_ok('prices', vps_codes_covered, set(all_codes), target_date):
                self.logger.info(f"✅ [VPS] 收盘价 {target_date} 已全覆盖 {len(vps_codes_covered)} 只基金，跳过本地采集")
            # [AI-2026-10-11 东哥铁律] 部分覆盖 / 断供 → 报警 + 留空，不再本地兜底
            return

        # [AI-2026-10-11 东哥铁律] VPS 无今日收盘价 → 报警(超时) + 留空，不再本地兜底。
        self._vps_missing_alert('prices')
        return

    def _fetch_prices_from_public_kline(self, all_codes, today_str):
        """[2026-11-09 方案A·东哥拍板] T 日盘后公共日K通道：腾讯 fqkline 首选、新浪兜底。

        原为 10-11「按铁律禁用、保留可回退」的 ``return`` 之后死代码；方案A 恢复为
        **盘后必要主通道**（docs/002_1 §六「不是兜底，别误拆」）。写入统一走
        ``_safe_save_fund_data``（upsert + 按 category 算 premium），次日凌晨
        VPS 权威文件到达即覆盖。腾讯日K close 与 woody/雪球 收盘价一分钱不差。
        """

        # [AI-2026-08-03] 无日K线基金跳过名单（持久化）：腾讯/新浪均无日K线的基金（债券/封闭LOF）
        # 首次探测到即写入 arbcore/config/no_kline_funds.json，后续启动直接跳过，不再刷 WARNING。
        no_kline = _load_no_kline_denylist()
        # [AI-2026-09-28 东哥点名修复] 同 step4 净值预期日：原实现只跳周末 ⇒ 9-25(中秋)
        # 被当成交易日，盘前（<15:10）会误判「近期收盘价未齐全」而重复爬腾讯日K线整轮
        # 全量基金。改走 A股交易日历；周末补班日 A股不交易，本就该跳过。
        def _prev_trading_day(d):
            t = d - timedelta(days=1)
            while not is_trading_day('A_SHARE', t):
                t -= timedelta(days=1)
            return t

        import requests
        import time
        import json

        # === [BUG-2026-10-06-05 修复] 腾讯K线被 WAF 拦截(HTTP 501)，新增新浪兜底 ===
        # 腾讯仍为首选（WAF 解除后自动恢复）；WAF/解析失败/无数据 → 退新浪 A 股K线。
        # 新浪 A 股代码规则与腾讯一致（sz/sh 按交易所），直接复用 tx_code，of 前缀不通。
        def _fetch_tencent_kline(tx_code):
            """返回 (status, items, qt_close)；status: 'ok' | 'no_data' | 'fail'"""
            try:
                url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={tx_code},day,,,5,qfq&r=0.{time.time()}"
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12,
                                    proxies={"http": None, "https": None})
                txt = resp.text
                if "=" in txt:
                    txt = txt.split("=", 1)[1]
                data = json.loads(txt)
                dd = data.get("data")
                if not isinstance(dd, dict):
                    return ('fail', None, None)
                node = dd.get(tx_code, {})
                kline = node.get("day") or node.get("qfqday")
                if not kline:
                    return ('no_data', None, None)
                items = list(kline.values()) if isinstance(kline, dict) else kline
                qt_node = node.get("qt", {}).get(tx_code)
                qt_close = None
                if isinstance(qt_node, list) and len(qt_node) > 3 and qt_node[3]:
                    try:
                        qt_close = float(qt_node[3])
                    except (ValueError, TypeError):
                        qt_close = None
                return ('ok', items, qt_close)
            except Exception as e:
                self.logger.debug(f"🔄 [{tx_code}] 腾讯K线失败，转新浪兜底: {e}")
                return ('fail', None, None)

        def _fetch_sina_kline(tx_code):
            """新浪 A 股K线兜底，返回与腾讯 day 同构的 items 列表 或 None（无数据/失败）"""
            try:
                url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={tx_code}&scale=240&ma=no&datalen=5"
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                    "Referer": "https://finance.sina.com.cn/"}, timeout=12,
                                    proxies={"http": None, "https": None})
                arr = resp.json()
                if not isinstance(arr, list) or not arr:
                    return None
                items = []
                for d in arr:
                    vol_shares = float(d.get('volume') or 0)
                    # 新浪 volume 单位=股，腾讯 day[5]=手(1手=100份) → /100 对齐
                    items.append([d.get('day'), float(d.get('open', 0)), float(d.get('close', 0)),
                                  float(d.get('high', 0)), float(d.get('low', 0)), vol_shares / 100.0])
                return items
            except Exception as e:
                self.logger.debug(f"🔄 [{tx_code}] 新浪K线兜底也失败: {e}")
                return None

        for code in all_codes:
            if not code: continue
            # [FIX 2026-07-31] 移除盘前 mark 导致的"收盘后跳过今天价"问题：
            # 原先盘前跑写入历史价后会 mark lof_price_*，使收盘后跑被 is_access_synced
            # 直接 continue 跳过，当天收盘价永远延迟到次日才入库。改为每次都尝试
            # upsert（幂等无害），今日价是否写入由下方 k_date==today 的时间判断控制。
            tx_code = f"sz{code}" if code.startswith(('0', '1', '3')) else f"sh{code}"

            # [AI-2026-08-03] 跳过已知无日K线基金（债券/封闭LOF 等腾讯新浪均无数据）
            if code in no_kline:
                self.logger.debug(f"⏭️ [{code}] 已知无日K线，跳过收盘价采集")
                continue

            # [AI-2026-08-03] 早跳过：收盘前(15:10 前)且近期收盘价已齐全则跳过重爬，
            # 避免每次后端启动都重爬全部 ~200 只（幂等但浪费+刷屏）。收盘后(>=15:10)仍爬当日收盘。
            _chk = datetime.now()
            if _chk.hour < 15 or (_chk.hour == 15 and _chk.minute < 10):
                conn_s = self.db._get_conn()
                latest = conn_s.execute(
                    "SELECT MAX(date) FROM unified_fund_history WHERE fund_code=? AND price IS NOT NULL", (code,)
                ).fetchone()
                conn_s.close()
                if latest and latest[0]:
                    latest_d = datetime.strptime(latest[0], '%Y-%m-%d').date()
                    if latest_d >= _prev_trading_day(_chk.date()):
                        self.logger.debug(f"⏭️ [{code}] 近期收盘价已齐全，跳过重爬")
                        continue

            # === 腾讯首选，WAF/解析失败则新浪兜底 (BUG-2026-10-06-05) ===
            k_status, items, qt_close = _fetch_tencent_kline(tx_code)
            if k_status == 'fail':
                self.logger.info(f"🔄 [{code}] 腾讯K线被WAF拦截，改用新浪兜底")
                items = _fetch_sina_kline(tx_code)
                qt_close = None
                if items is None:
                    # [AI-2026-10-09 方案B·东哥拍板] 双源**瞬态**失败（腾讯WAF/超时 + 新浪对
                    # LOF 恒返回 null/异常）绝不能当"该基金无K线"写入**永久**跳过名单。
                    # 2026-10-09 实盘事故：一次性毒化 unified_fund_list 全部 36 只（含 162411）
                    # → 收盘价永久写不进、盘后兜底空转、269 只缺当日行；毒名单还被提交进 git
                    # （3387f2c 09:20）。修复：瞬态失败只跳过本轮、下轮重试；只有腾讯**结构上
                    # 确实无 kline 节点**（下方 no_data 分支）才允许进跳过名单。
                    self.logger.warning(f"⚠️ [{code}] 腾讯/新浪双源瞬态失败，跳过本轮（不入跳过名单，下轮重试）")
                    continue
            if k_status == 'no_data':
                # [AI-2026-08-03] 首次探测到无K线：写入跳过名单，避免后续每次启动刷 WARNING
                # [AI-2026-10-09 方案B] 仅腾讯成功解析但无 kline 节点才走到这里（真·无日K线基金）；
                # 瞬态失败已在上方 continue，不再污染本名单。
                if code not in no_kline:
                    no_kline[code] = "腾讯/新浪均无日K线(债券/封闭LOF)"
                    _save_no_kline_denylist(no_kline)
                    self.logger.warning(f"⚠️ [{code}] K线无数据（已加入跳过名单，后续不再报错）")
                else:
                    self.logger.debug(f"⏭️ [{code}] K线无数据（已在跳过名单）")
                continue
            # k_status == 'ok'（腾讯）或新浪兜底成功
            written = 0
            for it in items:
                # 腾讯day格式: [date, open, close, high, low, volume(手), amount?]
                k_date = str(it[0])
                k_close = float(it[2]) if len(it) > 2 and it[2] else 0
                # [2026-07-30] 成交量(手)：1 手 = 100 份；换手率 = 成交量(手) / 份额(万) × 100，与 woody 网页对齐
                k_volume = float(it[5]) if len(it) > 5 and it[5] else 0
                # [FIX 2026-09-11] 成交额(万元) = 成交量(手)×100×收盘 / 10000 = k_volume×k_close/100。
                # 腾讯 qfq 日K线无独立成交额字段，由成交量×收盘反算（与 fund_service 盘中 rt['amount'] 万元量纲一致）。
                k_amount_wan = (k_volume * k_close / 100.0) if (k_volume and k_close > 0) else 0.0
                if k_close <= 0:
                    continue
                if k_date > today_str:
                    # 未来日期：防御性跳过（腾讯 K 线不应返回未来）
                    continue
                if k_date == today_str:
                    # [FIX 2026-07-31] 仅"盘中(15:10 前)"跳过当天快照价；
                    # 收盘后(>=15:10)应写入当天正式收盘价。
                    _now = datetime.now()
                    if _now.hour < 15 or (_now.hour == 15 and _now.minute < 10):
                        continue
                # [AI-2026-08-04] 最新日优先用 qt 收盘(更高精度)覆盖 K 线被截断的 close（仅腾讯源有 qt 节点）
                close_to_write = k_close
                if k_date == today_str and qt_close and qt_close > 0 and abs(qt_close - k_close) < 0.05:
                    k_dec = len(str(k_close).split('.')[-1]) if '.' in str(k_close) else 0
                    q_dec = len(str(qt_close).split('.')[-1]) if '.' in str(qt_close) else 0
                    if q_dec > k_dec:
                        close_to_write = qt_close
                # 收盘后 / 历史日期：正常写入官方收盘价
                self._safe_save_fund_data(date_str=k_date, fund_code=code, price=close_to_write, trade_volume=k_volume, volume=k_amount_wan)
                written += 1

            if written > 0:
                self.db.mark_access_synced(today_str, source=f'lof_price_{code}')
                self.logger.info(f"✅ [{code}] 写入 {written} 天官方收盘价")
            else:
                self.logger.info(f"⏭️ [{code}] 无新历史收盘价需写入（今日盘中跳过）")

    def step4_fetch_lof_market(self):
        """[AI-2026-06-28] 步骤四：仅获取净值（收盘价已分离到 _step4_fetch_prices）"""
        self.logger.info("=== 步骤四：抓取各基金最新净值 (东财API) ===")
        today_str = datetime.now().strftime('%Y-%m-%d')

        # 从大一统基金列表获取所有基金代码+分类
        conn_ufl = self.db._get_conn()
        all_fund_rows = conn_ufl.execute("SELECT fund_code, category FROM unified_fund_list").fetchall()
        conn_ufl.close()
        fund_categories = {str(r[0]): (r[1] or '') for r in all_fund_rows if r[0]}
        all_codes = list(fund_categories.keys())

        # [AI-2026-10-10] VPS优先：先尝试从东京VPS拉取净值（nav_{date}.json），
        # 全量导入后由下方 per-code expected_nav_date 检查自然跳过已补齐的 code。
        vps_nav = self._try_sync_all_from_vps('nav')
        if vps_nav:
            # [2026-11-09 事项3] nav 单文件是 21 天滚动窗口，历史文件不能充当「今日已覆盖」
            target_date = self._vps_expected_trade_date('nav')
            window = self._vps_coverage_window('nav', target_date)
            vps_nav_codes = set()
            for entry in vps_nav:
                content = entry.get('content', {})
                for row in content.get('rows', []):
                    code_v = str(row.get('code', ''))
                    date_v = str(row.get('date', ''))[:10]
                    nav_v = row.get('nav')
                    if code_v and date_v and nav_v:
                        self._safe_save_fund_data(date_str=date_v, fund_code=code_v, nav=nav_v)
                        if self._vps_row_is_fresh(date_v, window):
                            vps_nav_codes.add(code_v)
            if self._vps_coverage_ok('nav', vps_nav_codes, set(all_codes), target_date):
                self.logger.info(f"✅ [VPS] 净值 {target_date} 已全覆盖 {len(vps_nav_codes)} 只，跳过本地东财")
            # [AI-2026-10-11 东哥铁律] 部分覆盖 / 断供 → 报警 + 留空，不再本地兜底
            return
        # [AI-2026-10-11 东哥铁律] VPS 无今日净值 → 报警(超时) + 留空，不再本地兜底
        self._vps_missing_alert('nav')
        return
        # ===== 以下为原本地兜底（东财逐 code），已按铁律禁用，保留可回退 =====
        # [AI-2026-07-23] QDII欧美/黄金原油（美股基金）净值在北京时间上午尚未发布，
        # 按 expected = T-2 判定，下午 3 点后才改为 T-1，避免无谓的东财抓取。
        # [AI-2026-08-06] 国内LOF/指数LOF/QDII亚洲/QDII日本 与 A 股同日收盘、无时差，
        # T 日净值当日可得，expected 必须设为 T 日，否则会用 T-1 净值跳过 T 日采集。
        T2_CATEGORIES = {'黄金原油', 'QDII欧美'}
        T0_CATEGORIES = {'国内LOF', '指数LOF', '国内指数', 'QDII亚洲', 'QDII日本'}
        # 北京时间下午 3 点后，美股基金 T-1 净值通常已发布
        NAV_CUTOFF_HOUR = 15

        for code in all_codes:
            if not code: continue
            # 获取东财净值 ---
            # [AI-2026-09-28 东哥点名修复] 原实现只跳周末、不认中国法定假日 ⇒ 9-25(中秋)
            # 被当成交易日，expected_nav_date 落在「根本不存在的净值日」上 ⇒ 每次都判
            # "落后于预期"、每次白抓一次东财（9-28 日志 501018 出现 498 次即此）。
            # 改走 A股交易日历 is_trading_day('A_SHARE')（chinese_calendar 1.11 已能正确
            # 判 2026-09-25 为假日；缺失时退 _HOLIDAYS_2026 硬编码）。保持原结构不变：
            # 先退 n 个自然日，再回溯到最近一个交易日（周一 n=2 折叠到周五的既有语义保留）。
            def get_prev_trading_day(dt, n=1):
                t = dt.date() - timedelta(days=n)
                while not is_trading_day('A_SHARE', t):
                    t -= timedelta(days=1)
                return t
                
            t_1_date = get_prev_trading_day(datetime.now())
            t_2_date = get_prev_trading_day(datetime.now(), n=2)
            today_str = datetime.now().strftime('%Y-%m-%d')
            
            # 预期最新净值日期：
            #   QDII欧美/黄金原油：上午用 T-2，下午 3 点后用 T-1
            #   国内LOF/指数LOF/QDII亚洲/QDII日本：与 A 股同日收盘，T 日净值当日可得
            #   其他基金：始终 T-1
            category = fund_categories.get(code, '')
            now_hour = datetime.now().hour
            if category in T2_CATEGORIES:
                if now_hour < NAV_CUTOFF_HOUR:
                    expected_nav_date = t_2_date.strftime('%Y-%m-%d')
                else:
                    expected_nav_date = t_1_date.strftime('%Y-%m-%d')
            elif category in T0_CATEGORIES:
                # [AI-2026-08-20] 国内LOF/QDII亚洲/日本：盘中(15:00前)净值尚未出，expected 用 T-1 避免白刷东财；盘后 >=15:00 才预期 T 日。
                expected_nav_date = (today_str if now_hour >= NAV_CUTOFF_HOUR else t_1_date.strftime('%Y-%m-%d'))
            else:
                expected_nav_date = t_1_date.strftime('%Y-%m-%d')
            
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(date) FROM unified_fund_history WHERE fund_code = ? AND nav IS NOT NULL", (code,))
            max_nav_row = cursor.fetchone()
            conn.close()
            
            db_max_nav_date = max_nav_row[0] if max_nav_row and max_nav_row[0] else "2000-01-01"
            
            if db_max_nav_date >= expected_nav_date:
                nav_label = 'T-2' if category in T2_CATEGORIES else 'T-1'
                self.logger.info(f"✅ [{code}] 数据库已有{nav_label}净值 ({db_max_nav_date})，跳过东财接口...")
                self.db.mark_access_synced(today_str, source=f'lof_nav_{code}')
                continue
                
            self.logger.info(f"🔍 [{code}] 数据库最新净值({db_max_nav_date})落后于预期进度({expected_nav_date})，前往东财获取...")
            nav_df = self.hist_manager.get_nav(code, source="eastmoney")
            if not nav_df.empty:
                latest_nav_date = nav_df['date'].max().strftime('%Y-%m-%d')
                self.logger.info(f"✅ [{code}] 获取到历史净值，最新日期: {latest_nav_date}")
                for _, row in nav_df.iterrows():
                    d_str = row['date'].strftime('%Y-%m-%d')
                    self._safe_save_fund_data(date_str=d_str, fund_code=code, nav=row['nav'])
                if latest_nav_date >= expected_nav_date:
                    self.db.mark_access_synced(today_str, source=f'lof_nav_{code}')
            else:
                self.logger.warning(f"⚠️ [{code}] 东财接口未返回任何净值数据。")

        # [AI-2026-06-28] 假期修复移至 _run_pipeline，nav-only 不修价格

    def step4_5_sync_fund_purchase_status(self):
        """步骤4.5：[AI-2026-08-04] 从东京VPS拉取申赎状态JSON并入库(不再本地爬akshare)

        数据链路: 东京VPS每日02:30爬东财（purchase_status_LOFETF.py）→/root/ArbSiphon/data/purchase_status.json
        → 本步用 account_private.VPS_* 密钥 paramiko 拉回→导入本地 fund_purchase_status。
        失败仅告警(非致命), 次日重试; 与"本地黄金源→推ARM"架构一致(本地不再自爬)。

        ⚠️ 易混点（2026-09-29 标注）：本步骤采的是**申赎状态**（暂停申购/赎回/限额等，
        fund_purchase_status 表），**与步骤九的"场内份额(shares)"是两码事**：
        - 步骤九（份额）：原东京VPS shares_*.json 通道自 09-22 起半死，已于 2026-09-28
          改为**交易所直采**(复用 ETFarb core/share_direct_fetcher)，不再读 VPS。
        - 本步骤（申赎状态）：走 VPS 另一个文件 purchase_status.json，通道健康、无
          交易所直采替代源，故**保留 VPS 拉取**，不要因步骤九改直采而误删本步骤。
        """
        self.logger.info("=== 步骤4.5：基金申赎状态同步（东京VPS；注意：与步骤九场内份额直采是两码事）===")
        try:
            import subprocess, sys, os
            from arbcore.config import account_private as ap
            # [AI-2026-10-08 修正] deploy/ 已移出 src/，现与 src **平级**（ARM 上更早就是 ~/arbtest/deploy）。
            #   原 4 层 dirname 只上溯到 <repo>/src ⇒ tool 指向不存在的 <repo>/src/deploy/core/…，
            #   两机步骤4.5 长期静默跳过（ARM 自 10-02 起、本机自 10-07 搬迁后），fund_purchase_status 停更。
            #   项目根 = src 的父，deploy/ 与 database/ 均在该层。
            _src_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            root = os.path.dirname(_src_root)  # <repo>
            tool = os.path.join(root, "deploy", "core", "import_purchase_status.py")
            if not os.path.exists(tool):
                self.logger.warning(f"⚠️ 未找到导入工具 {tool}，跳过申赎状态同步")
                return
            tmp = os.path.join(root, "database", "_ps_tmp.json")
            cmd = [sys.executable, tool, "pull",
                   "--host", ap.VPS_HOST, "--port", str(ap.VPS_PORT),
                   "--user", ap.VPS_USER, "--key", VPS_KEY_PATH,
                   "--keypass", VPS_KEY_PASSWORD or "",
                   "--remote", "/root/ArbSiphon/data/purchase_status.json",
                   "--tmp", tmp]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            for line in (r.stdout + r.stderr).splitlines():
                self.logger.info("[申赎] " + line.strip())
            if r.returncode != 0:
                self.logger.error("❌ 申赎状态拉取失败(非致命, 次日重试)")
        except Exception as e:
            self.logger.error(f"❌ 申赎状态同步异常: {e}")

    def step5_fetch_usa_market_data(self, weekend_mode=False):
        """步骤五：抓取美股市场交易数据"""
        self.logger.info("=== 步骤五：抓取海外及指数市场交易数据 (标准库模式) ===")
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # [AI-2026-07-25] 周末模式：检测数据库最新日期，自动补全缺失交易日
        conn = self.db._get_conn()
        try:
            db_latest = conn.execute(
                "SELECT MAX(date) FROM usa_etf_daily_prices WHERE symbol='XOP'"
            ).fetchone()[0]
        except Exception:
            db_latest = None
        finally:
            conn.close()
        
        if weekend_mode and db_latest:
            # 周末：从数据库最新日期+1天开始采集，直到今天
            gap_days = (datetime.now() - datetime.strptime(db_latest, '%Y-%m-%d')).days
            start_date = db_latest
            self.logger.info(f"📅 [周末补采] 数据库最新美股数据: {db_latest}，距今 {gap_days} 天")
            if gap_days <= 1:
                self.logger.info("⏭️ [周末补采] 数据未缺失，跳过步骤五")
                return
        else:
            start_date = (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d')
            if not weekend_mode:
                self.logger.info(f"[平日] 采集最近15天美股数据 (start={start_date})")

        # access_sync_status 防刷：今天已跑过步骤五就跳过
        if self.db.is_access_synced_today(today_str, source='usa_etf_data'):
            self.logger.info("⏭️ [海外/指数] 今日已抓取，跳过步骤五")
            return

        # [AI-2026-09-23] 单一真相源：symbol_master（DB 权威）取代原先读 YAML config.funds
        #  的 valuation/hedging portfolio + related_index 白名单。只取走新浪/腾讯可补的海外
        #  市场 ETF/INDEX（含 woody 合成标的；N225 由 step5b VPS 链路负责，此处排除 JP INDEX）。
        #  symbol_master 缺失时退化回 YAML，保证不丢抓取能力。
        symbols = set()
        sym_meta = {}  # sym -> (market, asset_type)，循环内区分 ETF/INDEX 落库表
        sm_rows = None
        try:
            conn = self.db._get_conn()
            try:
                sm_rows = conn.execute(
                    "SELECT symbol, market, asset_type FROM symbol_master "
                    "WHERE active=1 AND asset_type IN ('ETF','INDEX') AND ("
                    " market IN ('US','CH','LONDON','HK','SYNTHETIC') "
                    " OR (market='JP' AND asset_type='ETF')) "
                    "ORDER BY symbol"
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            self.logger.warning(f"⚠️ [海外/指数] symbol_master 读取失败，退化用 YAML: {e}")
            sm_rows = None
        if sm_rows:
            for sym, mkt, atype in sm_rows:
                symbols.add(sym)
                sym_meta[sym] = (mkt, atype)
        else:
            # 退化：老 YAML 逻辑
            us_index_whitelist = {'.NDX', '.INX'}
            for fund in self.config.get('funds', []):
                for item in fund.get('valuation_portfolio', []) + fund.get('hedging_portfolio', []):
                    sym = str(item.get('symbol', '')).replace('^', '').split('-')[0]
                    # 港股代码(5位纯数字如00700)允许通过，其他纯数字跳过
                    if sym and (not sym.isdigit() or len(sym) == 5):
                        symbols.add(sym)
                ri = str(fund.get('related_index', '') or '').strip()
                if ri and ri.upper() in us_index_whitelist:
                    symbols.add(ri)
            for s in ('.NDX', '.INX'):
                sym_meta.setdefault(s, ('US', 'INDEX'))

        # [AI-2026-10-08 东哥定夺] 补：被基金用作 related_index 的「ETF」其日线也须进 index_history。
        #   step11 从 index_history[related_index] 取指数比率；但 09-23 起"ETF 类型只写 usa_etf"，
        #   导致 XOP/XBI/SOXX/RSPH/INDA/XLY/GLD/USO/VGT/KWEB/XLE 等 related_index 停在 2026-06-25，
        #   step11 取不到指数 → 回退无窗口 → 旧日期 index_pct/static_val 成垃圾。此处恢复同步，
        #   与 INDEX 类型合并为「index_history 落库符号集」，避免此类停滞复发。
        related_index_syms = set()
        try:
            conn = self.db._get_conn()
            try:
                related_index_syms = {
                    str(r[0]).strip() for r in conn.execute(
                        "SELECT DISTINCT related_index FROM unified_fund_list "
                        "WHERE related_index IS NOT NULL AND related_index != '' "
                        "AND related_index != '-' AND related_index NOT LIKE '%,%'"
                    ).fetchall() if r[0]
                }
            finally:
                conn.close()
        except Exception as e:
            self.logger.warning(f"⚠️ [海外/指数] related_index 读取失败，仅按 asset_type 落库: {e}")
        # index_history 落库符号集 = INDEX 类型 ∪ 被用作 related_index 的 ETF
        index_write_syms = {s for s, (m, a) in sym_meta.items() if a == 'INDEX'} | related_index_syms

        # [AI-2026-10-10] VPS优先：先尝试从东京VPS拉取美股/指数日线（usa_prices_{date}.json），
        # 已覆盖的 symbol 在下方 per-symbol 循环中跳过，减少本地直连新浪的请求量。
        vps_usa_covered = set()
        vps_usa = self._try_sync_all_from_vps('usa_prices')
        if vps_usa:
            # [2026-11-09 事项3] usa_prices 单文件是 48 标的全历史（18 万行），
            # 旧判据必被旧文件凑满 ⇒ 覆盖率只认新鲜窗口内的行。
            target_date = self._vps_expected_trade_date('usa_prices')
            window = self._vps_coverage_window('usa_prices', target_date)
            for entry in vps_usa:
                content = entry.get('content', {})
                for row in content.get('rows', []):
                    sym_v = str(row.get('symbol', ''))
                    date_v = row.get('date', '')
                    close_v = row.get('close')
                    if sym_v and date_v and close_v and close_v > 0:
                        self.db.upsert_usa_etf_price(date=date_v, symbol=sym_v, price=float(close_v))
                        if sym_v in index_write_syms:
                            self.db.upsert_index_history(symbol=sym_v, date=date_v, close=float(close_v))
                        if self._vps_row_is_fresh(str(date_v)[:10], window):
                            vps_usa_covered.add(sym_v)
            if self._vps_coverage_ok('usa_prices', vps_usa_covered, set(symbols), target_date):
                self.logger.info(f"✅ [VPS] 美股/指数日线 {target_date} 已全覆盖 {len(vps_usa_covered)} 只，跳过本地")
            # [AI-2026-10-11 东哥铁律] 部分覆盖 / 断供 → 报警 + 留空，不再本地兜底
            return
        # [AI-2026-10-11 东哥铁律] VPS 无今日数据 → 报警(超时) + 留空，不再本地兜底
        self._vps_missing_alert('usa_prices')
        return
        # ===== 以下为原本地兜底（新浪 per-symbol），已按铁律禁用，保留可回退 =====

        for sym in symbols:
            # [AI-2026-10-10] VPS 已覆盖的标的跳过本地直连（此前漏写 continue，VPS 块形同虚设、新浪请求量未减）
            if sym in vps_usa_covered:
                continue
            df = self.hist_manager.get_prices(sym, source="sina", start_date=start_date)
            if not df.empty:
                skipped_count = 0
                written_count = 0
                for _, row in df.iterrows():
                    date_str = row['date'].strftime('%Y-%m-%d')
                    close_val = row['close']
                    # [AI-2026-07-25] 过滤空值：close 为 None/NaN/<=0 时丢弃，不写入数据库
                    # 防止无效数据覆盖历史有效价格（曾导致 XOP/GLD 等 ETF price 被置 NULL）
                    import math
                    if close_val is None or (isinstance(close_val, float) and (math.isnan(close_val) or math.isinf(close_val))) or (isinstance(close_val, (int, float)) and close_val <= 0):
                        skipped_count += 1
                        continue
                    self.db.upsert_usa_etf_price(date=date_str, symbol=sym, price=float(close_val))
                    written_count += 1
                    # [AI-2026-09-23] 指数类型（.NDX/.INX/.SP500-45 等）写 index_history，ETF 类型只写 usa_etf_daily_prices。
                    # [AI-2026-10-08 东哥定夺] 被用作 related_index 的 ETF 也写 index_history（见上方 index_write_syms）。
                    if sym in index_write_syms:
                        self.db.upsert_index_history(symbol=sym, date=date_str, close=float(close_val))
                msg = f"✅ [海外/指数] {sym} 行情同步完成 (写入{written_count}条)"
                if skipped_count > 0:
                    msg += f" ⚠️ 跳过{skipped_count}条空值数据！"
                    self.logger.warning(msg)
                else:
                    self.logger.info(msg)

        self.db.mark_access_synced(today_str, source='usa_etf_data')
        self.logger.info(f"✅ [海外/指数] 步骤五完成，已标记防刷")

    # [AI-2026-07-20] 新增：从 VPS 同步 Yahoo 指数收盘价（N225 → index_history）
    def step5b_sync_vps_index_data(self):
        """从 VPS 同步 Yahoo 指数收盘价（如 N225），写入 index_history"""
        self.logger.info("=== 步骤五B：从VPS同步Yahoo指数收盘价 (N225) ===")
        today_str = datetime.now().strftime('%Y-%m-%d')

        vps_data = self._try_sync_all_from_vps('index')
        if not vps_data:
            # [AI-2026-10-11 东哥铁律] 本步本就 VPS-only（无本地兜底）；无数据 → 报警(超时) + 留空
            self._vps_missing_alert('index')
            return

        self.logger.info(f"🔄 [VPS-INDEX] 发现 {len(vps_data)} 份指数数据，正在同步入库...")
        for item in vps_data:
            file_date = item['date']
            content = item['content']
            try:
                symbol = content.get('symbol')
                close_price = content.get('close')
                trade_date = content.get('trade_date', file_date)

                if symbol and close_price is not None:
                    # [AI-2026-08-02] trade_date 以 VPS 东京脚本返回的「Yahoo 柱真实交易日」为准，
                    # 不再强求 == file_date。东京采集已挪到收盘后，且周末/日本假期时
                    # trade_date 自然滞后于 file_date（Yahoo 无该日 bar），属正确表现，应照常入库。
                    # 仅做轻量断言：不晚于今天(+1天容差)、不过期(>10天) 才写入，避免脏数据。
                    try:
                        td = datetime.strptime(trade_date, '%Y-%m-%d').date()
                        today = datetime.now().date()
                    except Exception:
                        self.logger.warning(f"   ⚠️ [VPS-INDEX] {symbol} 日期解析失败 trade_date={trade_date} file_date={file_date}")
                        continue
                    if td > today + timedelta(days=1):
                        self.logger.warning(f"   ⚠️ [VPS-INDEX] {symbol} trade_date={trade_date} 晚于今天，跳过写入")
                        continue
                    if (today - td).days > 10:
                        self.logger.warning(f"   ⚠️ [VPS-INDEX] {symbol} trade_date={trade_date} 距今天{(today - td).days}天，疑似过期，跳过写入")
                        continue
                    if td != datetime.strptime(file_date, '%Y-%m-%d').date():
                        self.logger.info(f"   ℹ️ [VPS-INDEX] {symbol} trade_date={trade_date} ≠ 文件名日期={file_date}（周末/假期正常），照常入库")
                    self.db.upsert_index_history(
                        symbol=symbol,
                        date=trade_date,
                        close=close_price
                    )
                    self.logger.info(f"   ✅ [VPS-INDEX] {symbol} {trade_date} -> close={close_price}")
                else:
                    self.logger.warning(f"   ⚠️ [VPS-INDEX] {file_date} 数据不完整: {content}")

                self.db.mark_access_synced(file_date, 'index_vps_sync')
            except Exception as e:
                self.logger.error(f"   ❌ [VPS-INDEX] 解析 {file_date} 出错: {e}")

        self.logger.info(f"✅ [VPS-INDEX] 指数数据同步完成")

    # [AI-2026-08-15] 新增：新浪 rt_hkHSSI 昨收记录器 → index_history(HSSI, source='sina')
    # Yahoo ^HSSI 已下架(404)，woody 自身也是用新浪 rt_hkHSSI 日记录器维护 ^HSSI 历史。
    def step5b2_record_hssi_daily(self):
        """从新浪记录 HSSI 昨收，写 index_history(HSSI, source='sina')。
        早晨(港股已收盘)跑时 rt_hkHSSI[3]=昨收=上一港股交易日收盘。

        [AI-2026-10-10] ⚠️ LOFarb 已停用 HSSI：SZ161124 已迁 ETFarb，
        LOFarb 不再记录 HSSI 昨收（下方实现保留供 ETFarb 复用，东哥 2026-10-10 指令）。
        """
        self.logger.info("⏭️ [步骤五B2] HSSI 已停用（SZ161124 迁 ETFarb），跳过记录")
        return
        # [AI-2026-10-10] 以下实现保留（LOFarb 不再执行，供 ETFarb 参考复用）：
        self.logger.info("=== 步骤五B2：新浪 HSSI 昨收记录器 ===")

        # [AI-2026-10-10] VPS优先：先尝试从东京VPS拉取 HSSI 昨收（hssi_{date}.json），
        # 成功则写入 index_history 并直接返回，减少本地直连新浪的请求量。
        vps_hssi = self._try_sync_all_from_vps('hssi')
        # [AI-2026-10-10] 只取 file date 最新的一份（原 for 循环取首份并 return，多日积压时会写入旧数据）
        if vps_hssi:
            best = max(vps_hssi, key=lambda e: str(e.get('date', '')))
            content = best.get('content', {})
            close_v = content.get('close')
            if close_v:
                # 与本地逻辑一致：rt_hkHSSI[3] 是昨收，归属日期为上一交易日（跳周末）
                d = datetime.now().date() - timedelta(days=1)
                while d.weekday() >= 5:
                    d -= timedelta(days=1)
                date_str = d.strftime('%Y-%m-%d')
                self.db.upsert_index_history(symbol='HSSI', date=date_str, close=float(close_v), source='vps')
                self.logger.info(f"   ✅ [HSSI VPS] {date_str} -> close={close_v} (source=vps, file={best.get('date')})")
                return

        try:
            # [AI-2026-08-21] 新浪请求节流
            import time as _tu
            _LAST = getattr(self, '_sina_last_t', 0.0)
            if _tu.time() - _LAST < 15:
                _tu.sleep(15 - (_tu.time() - _LAST))
            self._sina_last_t = _tu.time()
            import urllib.request
            url = "http://hq.sinajs.cn/list=rt_hkHSSI"
            req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode('gbk')
            parts = text.split('"')[1].split(',')
            if len(parts) < 4:
                self.logger.warning(f"   ⚠️ [HSSI] 新浪返回异常: {text[:80]}")
                return
            prev_close = float(parts[3])
            d = datetime.now().date() - timedelta(days=1)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            date_str = d.strftime('%Y-%m-%d')
            self.db.upsert_index_history(symbol='HSSI', date=date_str, close=prev_close, source='sina')
            self.logger.info(f"   ✅ [HSSI] {date_str} -> close={prev_close} (source=sina)")
        except Exception as e:
            self.logger.error(f"   ❌ [HSSI] 记录失败: {e}")

    # [AI-2026-07-20] 新增：从 VPS 同步 Yahoo ETF 净值（^XOP-IV → usa_etf_daily_prices.netvalue）
    def step5c_sync_vps_etf_nav(self):
        """从 VPS 同步 Yahoo ETF 净值（^XOP-IV, ^SPY-IV 等），写入 usa_etf_daily_prices.netvalue"""
        self.logger.info("=== 步骤五C：从VPS同步Yahoo ETF净值 (^XOP-IV 等) ===")

        vps_data = self._try_sync_all_from_vps('etf_nav')
        if not vps_data:
            # [AI-2026-10-11 东哥铁律] 本步本就 VPS-only（无本地兜底）；无数据 → 报警(超时) + 留空
            self._vps_missing_alert('etf_nav')
            return

        self.logger.info(f"🔄 [VPS-NAV] 发现 {len(vps_data)} 份净值数据，正在同步入库...")
        updated_count = 0
        for item in vps_data:
            file_date = item['date']
            content = item['content']
            try:
                # [AI-2026-07-20] Yahoo NAV 为前一美东交易日收盘，必须用 VPS 返回的 trade_date 入库，否则净值日期错移一天
                sync_date = content.get('trade_date') or file_date
                nav_data = content.get('nav_data', {})
                for sym, nav_value in nav_data.items():
                    # 跳过 N225（走 index 同步路径）
                    if sym == 'N225':
                        continue
                    # 跳过非数值类型（如 dict 格式的 N225 数据）
                    if not isinstance(nav_value, (int, float)):
                        continue
                    if nav_value is not None and float(nav_value) > 0:
                        # 使用 upsert_usa_etf_netvalue 只更新净值和创建行，不覆盖 price
                        self.db.upsert_usa_etf_netvalue(
                            date=sync_date,
                            symbol=sym,
                            netvalue=float(nav_value)
                        )
                        updated_count += 1

                self.db.mark_access_synced(sync_date, 'etf_nav_vps_sync')
                if updated_count > 0:
                    self.logger.info(f"   ✅ [VPS-NAV] {sync_date}: 更新 {updated_count} 条净值")
            except Exception as e:
                self.logger.error(f"   ❌ [VPS-NAV] 解析 {file_date} 出错: {e}")

        self.logger.info(f"✅ [VPS-NAV] ETF净值同步完成，共更新 {updated_count} 条")

    def step6_fetch_woody_regional_etfs(self):
        """步骤六：抓取 Woody 特有的区域变种虚拟 ETF (如 ^GLD-EU) 历史行情"""
        self.logger.info("=== 步骤六：抓取 Woody 区域变种虚拟 ETF 历史行情 ===")
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        regional_etfs = set()
        
        # 智能提取所有带有 ^ 前缀的区域虚拟 ETF
        for fund in self.config.get('funds', []):
            for item in fund.get('valuation_portfolio', []) + fund.get('hedging_portfolio', []):
                sym = str(item.get('symbol', ''))
                if sym.startswith('^'):
                    regional_etfs.add(sym)
                elif any(sym.endswith(suffix) for suffix in ['-EU', '-JP', '-HK']):
                    regional_etfs.add(f"^{sym}")
                    
        # [AI-2026-08-07] 提取为空即跳过，禁止用默认集掩盖（缺失即不跑，不编造标的，SUPREME 铁律）
        if not regional_etfs:
            self.logger.warning("⚠️ 区域ETF提取为空（config 未配置区域变种），跳过历史行情爬取")
            return

        # === 防刷检查：先检查数据库中是否已有最新数据 ===
        # 考虑美股时差：北京时间5月28日，美国是5月27日
        # 所以如果数据库中有今天或昨天的数据，就认为是最新的
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        today_str = today.strftime('%Y-%m-%d')
        yesterday_str = yesterday.strftime('%Y-%m-%d')
        
        etfs_needing_update = []
        for sym in regional_etfs:
            latest_date = self.db.get_latest_usa_etf_date(sym)
            # 如果最新日期是今天或昨天，就认为是最新的（考虑时差）
            if not latest_date or latest_date not in [today_str, yesterday_str]:
                etfs_needing_update.append(sym)
        
        if not etfs_needing_update:
            self.logger.info(f"✅ 所有区域ETF数据已是最新，跳过爬取...")
            return
        
        self.logger.info(f"需要更新的区域ETF: {etfs_needing_update}")
        
        # 只有在需要更新时才登录
        if not self._login_woody_if_needed():
            self.logger.warning("⚠️ Woody 未登录，跳过区域ETF数据爬取...")
            return
        
        missing_etfs = []
        for sym in etfs_needing_update:  # 只爬取需要更新的ETF
            # 每次爬取最近 10 天的历史数据，覆盖假期停机的缺口
            df = self.woody_crawler.fetch_woody_historical_data(sym, max_records=10)
            if df is not None and not df.empty:
                saved_count = 0
                for _, row in df.iterrows():
                    date_str = row['日期']
                    price = row['价格']
                    if price > 0:
                        self.db.upsert_usa_etf_price(date=date_str, symbol=sym, price=price)
                        saved_count += 1
                self.logger.info(f"✅ 区域变种 [{sym}] 历史行情入库完成，共更新 {saved_count} 天。")
            else:
                missing_etfs.append(sym)
                
        if missing_etfs:
            self.logger.error(f"🚨 健壮性告警：以下 Woody 区域变种 ETF 数据抓取失败：{', '.join(missing_etfs)}")
        else:
            self.db.mark_access_synced(today_str, source='regional_etf')


    def step7_fetch_extra_calibrations(self):
        """步骤七：从Woody网页补充抓取核心指数/商品的校准值"""
        self.logger.info("=== 步骤七：获取核心指数/商品校准值 (API优先模式) ===")

        today_str = datetime.now().strftime('%Y-%m-%d')
        if self.db.is_access_synced_today(today_str, source='woody_extra_calibrations'):
            self.logger.info("✅ 今日已成功同步校准值，跳过...")
            return

        # 🌟 优先尝试从刚刚抓取的 API 原始数据湖中提取
        raw_json_str = self.db.get_raw_api_data(today_str, source='woody_lof')
        api_success = False
        
        if raw_json_str:
            try:
                api_data = json.loads(raw_json_str)
                # Woody API 包装在 text 字段里
                if 'text' in api_data: api_data = api_data['text']
                
                symbol_map = {'GLD': 'GC', 'USO': 'CL', '^GSPC': 'ES', '^NDX': 'NQ'}
                found_count = 0
                
                for api_sym, db_sym in symbol_map.items():
                    if api_sym in api_data:
                        item = api_data[api_sym]
                        calib_val = item.get('calibration')
                        date_str = item.get('est_date', item.get('date', today_str))
                        
                        if calib_val:
                            self.db.upsert_futures_daily(date=date_str, symbol=db_sym, calibration=float(calib_val))
                            self.logger.info(f"✅ [API] {db_sym} ({date_str}) -> {calib_val} 同步成功。")
                            found_count += 1
                
                if found_count >= 4:
                    api_success = True
                    self.logger.info("🎉 所有核心校准值已从 API 成功同步。")
            except Exception as e:
                self.logger.error(f"❌ 从 API 缓存解析校准值失败: {e}")

        if api_success:
            self.db.mark_access_synced(today_str, source='woody_extra_calibrations')
            return

        # --- 备选方案：原来的网页爬虫 (已因 Woody 强制登录失效，仅作备份参考) ---
        self.logger.warning("⚠️ API 未能提供完整校准值，尝试网页备份路径(可能因登录限制失败)...")
        
        # calibration_values = self.woody_crawler.get_future_calibration_values()
        calibration_values = None # 暂时强制禁用爬虫以防封号，如有需要再开启
        
        if not calibration_values:
            self.logger.warning("⚠️ 未能通过任何途径获取到今日校准值数据。")
            return

        # symbol_map_legacy: {key: db_symbol}
        symbol_map_legacy = {
            'gold': 'GC',
            'oil': 'CL',
            'sp500': 'ES',
            'nasdaq': 'NQ'
        }

        for key, db_sym in symbol_map_legacy.items():
            if key not in calibration_values:
                continue

            calib_val = calibration_values[key]
            date_str = calibration_values.get(f'{key}_date', '')

            if calib_val and calib_val > 0:
                self.db.upsert_futures_daily(date=date_str, symbol=db_sym, calibration=calib_val)
                self.logger.info(f"✅ [网页备份] {db_sym} ({date_str}) -> {calib_val} 入库成功。")
            else:
                self.logger.warning(f"⚠️ [{db_sym}] 获取到的校准值无效，跳过入库。")
                
        self.db.mark_access_synced(today_str, source='woody_extra_calibrations')


    def step8_fetch_sina_futures_from_vps(self):
        """步骤八：从VPS同步新浪期货数据，并带有本地接口备用源（收盘价和结算价）"""
        self.logger.info("=== 步骤八：获取新浪期货数据 (VPS优先 -> 本地备用源) ===")
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # 如果今天的数据已经同步成功过，直接跳过
        if self.db.is_access_synced_today(today_str, source='futures_data'):
            self.logger.info("✅ 今日已同步过期货数据，跳过抓取。")
            return
            
        vps_futures_data = self._try_sync_all_from_vps('futures')
        vps_today_success = False
        
        if vps_futures_data:
            self.logger.info(f"🔄 [VPS] 发现 {len(vps_futures_data)} 份历史期货数据，正在同步入库...")
            for item in vps_futures_data:
                file_date = item['date']
                content = item['content']
                try:
                    date_info = content.get('date', file_date)
                    futures_list = content.get('data', [])
                    for f_data in futures_list:
                        symbol = f_data.get('symbol')
                        settle = f_data.get('settle')
                        close_price = f_data.get('close')
                        volume = f_data.get('volume')
                        
                        if symbol and (settle is not None or close_price is not None):
                            self.db.upsert_futures_daily(date=date_info, symbol=symbol, settle_price=settle, close_price=close_price, volume=volume)
                    
                    self.logger.info(f"   ✅ [VPS] 同步入库期货数据: {date_info} ({len(futures_list)} 个品种)")
                    self.db.mark_access_synced(file_date, 'futures_vps_sync')
                    if date_info >= today_str:
                        vps_today_success = True
                except Exception as e:
                    self.logger.error(f"   ❌ [VPS] 解析日期 {file_date} 期货数据时出错: {e}")

        if vps_today_success:
            # [AI-2026-07-23] VPS 同步成功后，检查 NK 数据是否存在
            # VPS 可能有其他期货数据但缺少 NK，需要从新浪备用源补齐
            try:
                _nk_conn = self.db._get_conn()
                try:
                    nk_check = _nk_conn.execute(
                        "SELECT COUNT(*) FROM futures_daily WHERE symbol='NK' AND date=? AND settle_price > 0",
                        (today_str,)
                    ).fetchone()
                    if nk_check[0] == 0:
                        self.logger.warning(f"⚠️ [VPS] 今日 NK 期货数据缺失，从新浪补齐...")
                        from arbcore.fetchers.data_fetcher import data_fetcher
                        nk_data = data_fetcher.get_futures_settlement_data()
                        for f_data in nk_data:
                            if f_data.get('symbol') == 'NK':
                                settle = f_data.get('settle')
                                close_price = f_data.get('close')
                                volume = f_data.get('volume')
                                if settle is not None or close_price is not None:
                                    self.db.upsert_futures_daily(date=today_str, symbol='NK', settle_price=settle, close_price=close_price, volume=volume)
                                    self.logger.info(f"  ✅ NK 结算价={settle}, 收盘价={close_price}")
                finally:
                    _nk_conn.close()
            except Exception as e:
                self.logger.warning(f"⚠️ [VPS] NK 数据检查失败: {e}")

            self.db.mark_access_synced(today_str, source='futures_data')
            self.logger.info("✅ [VPS] 今日期货数据同步完成！")
            return

        # [AI-2026-10-11 东哥铁律] VPS 无今日期货数据 → 报警(超时) + 留空，不再本地兜底。
        if self.db.is_access_synced_today(today_str, 'futures_vps_sync'):
            # 今日 VPS 已同步过（本次只是因防刷跳过返回空）→ 属正常，不报警
            self.logger.info("✅ [VPS] 今日期货数据此前已同步(VPS)，无需处理")
        else:
            self._vps_missing_alert('futures')
        return

        # ===== 以下为原本地备用源（data_fetcher 新浪期货），已按铁律禁用，保留可回退 =====
        from arbcore.fetchers.data_fetcher import data_fetcher
        fallback_data = data_fetcher.get_futures_settlement_data()
        if fallback_data:
            for f_data in fallback_data:
                symbol = f_data.get('symbol')
                settle = f_data.get('settle')
                close_price = f_data.get('close')
                volume = f_data.get('volume')
                # [AI-2026-08-31] AG0 昨结算价自带归属日期（上一交易日），不得写成今天
                row_date = f_data.get('date') or today_str
                if symbol and (settle is not None or close_price is not None):
                    self.db.upsert_futures_daily(date=row_date, symbol=symbol, settle_price=settle, close_price=close_price, volume=volume)
            self.db.mark_access_synced(today_str, source='futures_data')
            self.logger.info(f"✅ [本地备用源] 今日期货数据获取完成！")
        else:
            self.logger.error("❌ [本地备用源] 获取期货数据失败。")

    def step9_fetch_jsl_shares_from_vps(self):
        """步骤九：场内份额采集（**交易所直采**，2026-09-28 起；不再读东京 VPS）。

        背景：原实现从东京 VPS 拉 `shares_*.json` 入库；VPS 份额通道自 09-22 起半死
        （ETF 0 只、LOF 停 09-18），且其源本就是「交易所官方 + 东财派生」。
        现改为**复用 ETFarb 的份额直采模块**（`core/share_direct_fetcher.collect`），
        单一真源、不再维护 arbTest 本地副本：
          · 深 LOF 16xxxx / 深 ETF 159xxx → 深交所官方 scsj_fund_jjgm（带日期窗）
          · 沪 LOF 501/502/506            → 上交所官方 LOFGMTJ（逐日全量）
          · 沪 ETF 51/52/56/58            → 腾讯 qt.gtimg.cn 派生（交易所不公布）
        采集范围 = `lof_config.yaml` 的 `funds[].code`。
        """
        self.logger.info("=== 步骤九：场内份额采集（交易所直采）===")
        today_str = datetime.now().strftime('%Y-%m-%d')

        # 防刷：今日已直采过则跳过（避免每次启动重复抓交易所）
        if self.db.is_access_synced_today(today_str, source='shares_direct_sync'):
            self.logger.info("✅ 今日份额已直采，跳过")
            return

        # [AI-2026-10-10] VPS优先：先尝试从东京VPS拉取份额（shares_{date}.json）。
        # 仅当 VPS 覆盖了本地直采标的全集（_share_codes()）才标记防刷并跳过本地直采；
        # 部分覆盖（如 30/35）严禁提前 return——否则未覆盖标的当日无份额，静默数据缺口。
        vps_shares = self._try_sync_all_from_vps('shares')
        if vps_shares:
            # [2026-11-09 事项3] shares 单文件含 17 天滚动窗口，历史行不能充当今日覆盖
            expected_share_codes = set(self._share_codes())
            target_date = self._vps_expected_trade_date('shares')
            window = self._vps_coverage_window('shares', target_date)
            vps_rows_count = 0
            vps_codes = set()          # 全历史（仅日志）
            fresh_codes = set()        # 新鲜窗口内（唯一判据）
            for entry in vps_shares:
                content = entry.get('content', {})
                for row in content.get('rows', []):
                    code_v = str(row.get('code', ''))
                    date_v = str(row.get('date', ''))[:10]
                    shares_v = row.get('shares')
                    if code_v and date_v and shares_v is not None:
                        try:
                            self.db.save_unified_history(date_str=date_v, fund_code=code_v, shares=float(shares_v))
                            vps_rows_count += 1
                            vps_codes.add(code_v)
                            if self._vps_row_is_fresh(date_v, window):
                                fresh_codes.add(code_v)
                        except Exception as e:
                            self.logger.debug(f"   [VPS份额] 入库 {code_v} {date_v} 失败: {e}")
            if not expected_share_codes:
                self.logger.warning("⚠️ [VPS] 份额标的清单为空，无法判覆盖，不标防刷")
                return
            if self._vps_coverage_ok('shares', fresh_codes, expected_share_codes, target_date):
                self.logger.info(f"📥 [VPS] 份额已导入 {vps_rows_count} 行（{target_date} 覆盖全部 {len(expected_share_codes)} 只标的），跳过本地直采")
                self.db.mark_access_synced(today_str, source='shares_direct_sync')
            # [AI-2026-10-11 东哥铁律] 部分覆盖 / 断供 → 报警 + 留空，不再本地直采
            return
        # [AI-2026-10-11 东哥铁律] VPS 无份额数据 → 报警 + 留空，不再本地直采
        self._vps_missing_alert('shares')
        return

        # ===== 以下为原本地直采（share_direct_fetcher），已按铁律禁用，保留可回退 =====

        try:
            S = self._import_share_fetcher()
        except Exception as e:
            self.logger.error(f"❌ [份额] 直采模块导入失败: {e}")
            return

        codes = self._share_codes()
        if not codes:
            self.logger.warning("⚠️ [份额] 未取到标的清单，跳过")
            return

        try:
            rows, failed = S.collect(codes, days=30, logger=self.logger)
        except Exception as e:
            self.logger.error(f"❌ [份额] 直采异常: {e}")
            return

        # 落库：unified_fund_history(date, fund_code, shares) —— UPSERT，只写 shares 列
        ok = 0
        for code, date_iso, shares, src in rows:
            try:
                self.db.save_unified_history(date_str=date_iso, fund_code=code, shares=shares)
                ok += 1
            except Exception as e:
                self.logger.error(f"   ❌ [份额] 入库 {code} {date_iso} 失败: {e}")

        self.logger.info(f"✅ [份额] 直采完成: 入库 {ok} 行 / 抓取 {len(rows)} 行，失败 {len(failed)} 条")
        if failed[:5]:
            self.logger.warning(f"   ⚠️ [份额] 失败样本: {failed[:5]}")
        self.db.mark_access_synced(today_str, source='shares_direct_sync')

    def _share_codes(self):
        """份额采集标的清单：`lof_config.yaml` 的 `funds[].code`。"""
        config_path = getattr(self, 'config_path', None)
        if not config_path or not os.path.exists(config_path):
            fallback = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))))),
                "arbcore", "config", "lof_config.yaml")
            config_path = fallback if os.path.exists(fallback) else None
        if not config_path:
            return []
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f)
            return [str(x.get('code')).strip() for x in cfg.get('funds', []) if x.get('code')]
        except Exception as e:
            self.logger.error(f"❌ [份额] 读取 lof_config.yaml 失败: {e}")
            return []

    @staticmethod
    def _import_share_fetcher():
        """复用 ETFarb 的份额直采模块（单一真源，避免两份代码漂移）。

        候选路径（按序探测，存在的才注入 sys.path）：
          · `ARB/ETFarb/src/core`  —— 现行布局（ETFarb 已迁入 src/，2026-10-07 起）
          · `ARB/ETFarb/core`      —— 2026-10 前的旧布局，兼容保留
          · ARM `/home/ubuntu/etfarb/src/core`、`/home/ubuntu/etfarb/core`
        亦可用环境变量 ETFARB_CORE 直接指定。
        找到即 import `share_direct_fetcher`（含 `collect()`）。
        """
        _here = os.path.dirname(os.path.abspath(__file__))
        # 标准 clone 布局：ETFarb 与 LOFarb 同级（均在 ARB/ 下）
        # _here = <ARB>/LOFarb/src/ArbDashboard/backend/scheduler ⇒ 上溯 5 层 = <ARB>
        _arb = os.path.normpath(os.path.join(_here, "..", "..", "..", "..", ".."))
        candidates = [
            os.environ.get("ETFARB_CORE", ""),
            os.path.join(_arb, "ETFarb", "src", "core"),   # [AI-2026-10-07] 现行布局
            os.path.join(_arb, "ETFarb", "core"),          # 旧布局兼容
            "/home/ubuntu/etfarb/src/core",                # ARM（Linux 区分大小写，显式小写）
            "/home/ubuntu/etfarb/core",
        ]
        candidates = [c for c in candidates if c]
        for d in candidates:
            if os.path.isdir(d) and d not in sys.path:
                sys.path.insert(0, d)
        try:
            import share_direct_fetcher as S
        except ImportError as e:
            # [AI-2026-10-07] 报出已尝试路径，避免下次再盲找
            raise ImportError(
                f"{e}（已尝试候选路径: {candidates}；可用环境变量 ETFARB_CORE 指定）"
            ) from e
        return S

    def _step10_calculate_static_valuation(self):
        """步骤十：基于同步后的因子数据，计算所有基金的静态估值 (static_val)

        [AI-2026-10-11 东哥定调] **迭代口径改为 DB 权威**（「一切以 DB 为准，YAML 仅作备用」）：
          旧实现遍历 lof_config.yaml 的 funds[]，YAML 一旦与 DB 漂移（漏列新基金/改名）
          就会**漏算**——YAML 是「主表」时的历史残留。现改为遍历 unified_fund_list（DB 全集），
          YAML 降格为「按 code 的可选补充」（仅提供 DB 尚无的 valuation_method 等字段，缺失不致命）。
          与 step5 的 symbol_master 手法同源（DB 权威 + YAML 退化，见本文件 :1188 注释）。
          篮子标的仍以 fund_basket_weights 表为权威（process_fund 内部逻辑，未变）。
        """
        # 1) 读 YAML —— 仅作「按 code 的补充配置」（不再是遍历主表）
        config_path = getattr(self, 'config_path', None)
        if not config_path or not os.path.exists(config_path):
            fallback_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "arbcore", "config", "lof_config.yaml")
            config_path = fallback_path if os.path.exists(fallback_path) else None
        yaml_by_code = {}
        if config_path:
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    _cfg = yaml.safe_load(f) or {}
                yaml_by_code = {str(fd.get('code', '')): fd
                                for fd in _cfg.get('funds', []) if fd.get('code')}
            except Exception as e:
                self.logger.warning(f"⚠️ [静态估值] 读取 YAML 补充配置失败（继续用 DB）: {e}")

        # 2) 从 DB 取基金全集 + 篮子归属（DB 权威）
        conn = self.db._get_conn()
        try:
            rows = conn.execute(
                "SELECT fund_code, fund_name, category, related_index, pos_ratio "
                "FROM unified_fund_list ORDER BY fund_code"
            ).fetchall()
            basket_codes = {str(r[0]) for r in conn.execute(
                "SELECT DISTINCT fund_code FROM fund_basket_weights"
            ).fetchall()}
        finally:
            conn.close()

        calc = StaticValuationCalculator(self.db)
        success = 0
        total = 0
        for code, name, category, related_index, pos_ratio in rows:
            code = str(code)
            yc = yaml_by_code.get(code, {})
            yaml_portfolio = yc.get('valuation_portfolio') or yc.get('hedging_portfolio')
            if code not in basket_codes and not yaml_portfolio:
                continue  # 无篮子且无 YAML 配置 → 交 step11（跟踪指数公式）
            fund = {
                'code': code,
                'name': name or yc.get('name', ''),
                'category': category,                          # DB 权威
                'related_index': related_index or yc.get('related_index', ''),
                'pos_ratio': pos_ratio,
                'valuation_method': yc.get('valuation_method', ''),   # YAML 补充；缺→''→resolve_method 按 category
                'valuation_portfolio': yc.get('valuation_portfolio', []),
                'hedging_portfolio': yc.get('hedging_portfolio', []),
            }
            total += 1
            try:
                if calc.process_fund(fund):
                    success += 1
            except Exception as e:
                self.logger.error(f"  ❌ [{code}] 静态估值计算失败: {e}")
        self.logger.info(f"✅ [静态估值] 计算完成，{success}/{total} 只基金已更新")

    # ================================================================
    # 步骤十一：跟踪指数公式静态估值 (覆盖 QDII亚洲 / 国内LOF / 指数LOF)
    # 公式: static_val = prev_nav * (1 + pos_ratio * (idx_ratio * fx_ratio - 1))
    # 与魔法公式区别：用指数比率代替 ETF 价格/hedge，无需 hedge 参数
    # ================================================================
    def step11_simple_static_valuation(self, recent_days=None):
        """
        跟踪指数公式静态估值：覆盖 step10 未处理的基金
        公式: static_val = prev_nav * (1 + pos_ratio * (idx_ratio * fx_ratio - 1))
        与魔法公式区别：用指数比率代替 ETF 价格/hedge，无需 hedge 参数
        """
        import sqlite3
        self.logger.info("=== 步骤十一：跟踪指数公式静态估值 (QDII亚洲/国内LOF/指数LOF) ===")

        # [AI-2026-08-16] 活库移出仓库根（物理隔离防泄漏）
        # [AI-2026-10-06] 搬入 src/ 后层数全变，改走 arbcore 统一解析（env ARB_MASTER_DB 优先 → _shared → database/ 上溯）
        from arbcore.database.db_manager import _resolve_db_path
        db_path = _resolve_db_path()
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        cursor = conn.cursor()

        # 1. 获取所有基金
        cursor.execute("SELECT fund_code, category, related_index, pos_ratio, COALESCE(data_source, '') FROM unified_fund_list")
        all_funds = cursor.fetchall()

        # 2. 分类：step10 已处理黄金原油和有basket的基金，这里处理其余
        #    - 国内指数/指数LOF → A股，无汇率
        #    - QDII亚洲 → 港币
        #    - QDII欧美（无basket的，如标普500）→ 美元
        #    - 白银(161226) → 跳过（期货估值特殊，交 step12）
        a_share = []
        hk_funds = []
        us_funds = []
        jp_funds = []

        # 读取 fund_list.csv 获取分类（与 jsl/03_eod_all.py 一致）
        csv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "jsl", "fund_list.csv")
        fund_category = {}
        if os.path.exists(csv_path):
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                import csv
                reader = csv.DictReader(f)
                for row in reader:
                    code = row.get('代码', '').strip()
                    cat = row.get('分类', '').strip()
                    if code and cat:
                        fund_category[code] = cat

        hk_index_syms = {'HSI', 'HSCEI', 'HSCCI', 'HSMCI', 'HSTECH', 'HSCI', 'HSSI', 'HSSNCNE', 'HSMI'}

        for fund_code, category, related_index, pos_ratio, data_source in all_funds:
            # [AI-2026-10-11 东哥定调] 161226 白银：分类已由「白银」并入「商品」，
            #   但它走期货(A G0)口径、由 step12 负责 ⇒ 判据改按基金代码跳过（不再依赖 category 名）。
            if fund_code == '161226':
                continue  # 白银跳过（交 step12）

            ri = str(related_index).strip() if related_index else ''
            if not ri or ri == '-' or ri == '0':
                continue  # 无跟踪标的，跳过

            ri_upper = ri.upper()
            ri_lower = ri.lower()

            # 按 category 分类
            if category in ('国内指数', '指数LOF', '国内LOF'):
                a_share.append((fund_code, ri, pos_ratio or 0.95))
            elif category == 'QDII亚洲':
                hk_funds.append((fund_code, ri, pos_ratio or 0.95))
            elif category == 'QDII日本':
                jp_funds.append((fund_code, ri, pos_ratio or 0.95))
            elif category == 'QDII欧美':
                us_funds.append((fund_code, ri, pos_ratio or 0.95))
            elif category == '黄金原油':
                # [AI-2026-10-11 东哥定调] 黄金原油按 data_source 路由：
                #   woody（真·多标的篮子，step10 用 woody position/篮子处理）→ 跳过；
                #   非 woody（自算，如油气 3：162411/513350/159518，单标的 XOP）→ 走指数跟踪（usd）。
                #   依据：单标的时 魔法公式 ≡ 矩阵公式 ≡ 指数跟踪（unified_valuation.py:56），结果等价；
                #   且 step11 消费 DB pos_ratio，无需 woody 每日下发 position。
                #   ⚠️ 口径差：step10 用 usa_etf_daily_prices.netvalue(NAV)，step11 用 index_history.close(市场价)。
                if data_source == 'woody':
                    continue
                us_funds.append((fund_code, ri, pos_ratio or 0.95))
            else:
                # 根据指数代码推断
                if ri_upper in hk_index_syms or ri_lower in {s.lower() for s in hk_index_syms}:
                    hk_funds.append((fund_code, ri, pos_ratio or 0.95))
                elif ri_lower.startswith('399') or ri_lower.startswith('000') or ri_lower.startswith('001') or '.csi' in ri_lower:
                    a_share.append((fund_code, ri, pos_ratio or 0.95))
                elif ri_upper in ('.INX', '.NDX', 'SPY', 'QQQ', 'XOP', 'XLY', 'XBI', 'KWEB', 'RSPH', 'INDA'):
                    us_funds.append((fund_code, ri, pos_ratio or 0.95))

        self.logger.info(f"  分类结果: A股={len(a_share)}, 港股={len(hk_funds)}, 美股={len(us_funds)}, 日股={len(jp_funds)}")

        total_updated = 0

        if recent_days:
            self.logger.info(f"   ⚙️ 仅核对近 {recent_days} 个交易日（优化：避免全量历史重算）")

        def _close(a, b, eps=1e-9):
            """浮点/None 安全比较：都为 None 视为相等；数值差小于 eps 视为相等。"""
            if a is None or b is None:
                return a is None and b is None
            try:
                return abs(float(a) - float(b)) < eps
            except (TypeError, ValueError):
                return a == b

        def process_batch(funds, batch_name, fx_type='none'):
            nonlocal total_updated
            if not funds:
                return
            self.logger.info(f"  --- {batch_name} ({len(funds)} 只) ---")

            for fund_code, related_index, pos_ratio in funds:
                # 获取指数历史数据
                # [AI-2026-10-11 东哥定夺] 严格 NAV：related_index 若为美股 ETF（存在于 usa_etf_daily_prices），
                #   取 netvalue(官方 NAV) 优先、退回 price(市场价)；否则退回 index_history.close。
                #   目的：XOP 等「被当作指数用」的美股 ETF 与 step10 口径统一（均 NAV），消除 <0.1% 市场价差。
                #   注：.INX/.NDX 在 usa_etf_daily_prices 中 netvalue 恒空 → COALESCE 落到 price，
                #       与 index_history.close 同值，故指数类基金不受影响。
                idx_rows = []
                for _sym in (related_index, related_index.upper(), related_index.lower()):
                    cursor.execute(
                        "SELECT date, COALESCE(NULLIF(netvalue, 0), price) FROM usa_etf_daily_prices WHERE symbol = ? ORDER BY date",
                        (_sym,))
                    idx_rows = cursor.fetchall()
                    if idx_rows:
                        break
                if not idx_rows:
                    for _sym in (related_index.upper(), related_index.lower()):
                        cursor.execute(
                            "SELECT date, close FROM index_history WHERE symbol = ? ORDER BY date",
                            (_sym,))
                        idx_rows = cursor.fetchall()
                        if idx_rows:
                            break
                if not idx_rows:
                    self.logger.warning(f"    [{fund_code}] 指数 {related_index} 无历史数据，跳过")
                    continue

                idx_data = {r[0]: r[1] for r in idx_rows}

                # 获取该基金的交易日数据
                if recent_days:
                    # [AI-2026-08-06] 仅取近 N 个交易日重算（东哥确认：全量历史重算无意义，近 5 天足够）
                    cursor.execute(
                        "SELECT date, nav, price, index_close, static_val, premium, "
                        "index_pct, calibration, rt_premium FROM unified_fund_history "
                        "WHERE fund_code = ? AND nav IS NOT NULL ORDER BY date DESC LIMIT ?",
                        (fund_code, recent_days))
                else:
                    cursor.execute(
                        "SELECT date, nav, price, index_close, static_val, premium, "
                        "index_pct, calibration, rt_premium FROM unified_fund_history "
                        "WHERE fund_code = ? AND nav IS NOT NULL ORDER BY date",
                        (fund_code,))
                fund_rows = cursor.fetchall()

                count = 0
                last_fx_pct = 0

                for date, nav, price, existing_idx_close, ex_static, ex_premium, ex_index_pct, ex_calib, ex_rt_prem in fund_rows:
                    if not nav or float(nav) <= 0:
                        continue

                    # [AI-2026-07-20] 始终用 index_history 的最新值覆盖 index_close，
                    # 不再仅在为空时回写。这样手动修正 index_history 后重跑 step11 即可生效，
                    # 无需额外 SQL 同步（旧逻辑若 index_close 已非空会跳过回写，导致修正不生效）。
                    if date in idx_data:
                        current_idx_close = idx_data[date]
                        # [AI-2026-08-06] 仅在 index_close 实际变化时才写回，避免每次启动全量冗余 UPDATE
                        if not _close(current_idx_close, existing_idx_close):
                            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 index_close 是谁写的"
                            cursor.execute(
                                "UPDATE unified_fund_history SET index_close = ?, writer = 'lofarb:daily_updater.step11' "
                                "WHERE fund_code = ? AND date = ?",
                                (current_idx_close, fund_code, date))
                    else:
                        # [AI-2026-10-08 东哥定夺] 当日指数无数据（指数市场休市 / 指数源缺口）：
                        #   绝不复用库里可能陈旧或脏的 index_close —— 曾致 164906 沪市假日行
                        #   复用遗留脏值 5498（KWEB 实为 ~35）算出 index_pct=+15296%、static_val=185。
                        #   一律置 None ⇒ 下方 continue（宁缺毋假），该行静态估值交由 step10/篮子或保持原值。
                        current_idx_close = None

                    if current_idx_close is None:
                        continue

                    # [AI-2026-07-13] 找基准日：前一个有 nav 的交易日（prev_idx_close 从 idx_data 获取，与 current_idx_close 同源）
                    cursor.execute(
                        "SELECT date, nav FROM unified_fund_history "
                        "WHERE fund_code = ? AND date < ? AND nav IS NOT NULL "
                        "ORDER BY date DESC LIMIT 1",
                        (fund_code, date))
                    prev_row = cursor.fetchone()
                    if not prev_row:
                        continue

                    prev_date, prev_nav = prev_row
                    if not prev_nav or float(prev_nav) <= 0:
                        continue

                    # 从 index_history 获取基准日指数值（与 current_idx_close 同源），避免 step5 写入的 ETF 价格污染
                    # [AI-2026-07-23] 修复：如果 prev_date 是休市日（无指数数据），往前找最近有数据的交易日
                    # [AI-2026-10-08 东哥定夺] 加回看窗口：仅允许回溯到 prev_date 前 7 个自然日内的最近
                    #   有数据交易日（覆盖两市场假期错位）；超出窗口 ⇒ 说明指数源有缺口（如 XOP/XBI/SOXX/
                    #   RSPH/INDA/XLY 的 index_history 曾停在 06-25），此时 continue（宁缺毋假），
                    #   绝不无限回溯到数周前 —— 否则 idx_ratio 离谱，index_pct/static_val 成垃圾
                    #   （实证 162411 07-17 曾算出 index_pct=+12.49%、误差 +9%）。
                    _LOOKBACK_MAX_DAYS = 7
                    _lookup_date = prev_date
                    if _lookup_date not in idx_data:
                        _sorted_dates = sorted([d for d in idx_data.keys() if d < _lookup_date], reverse=True)
                        _lookup_date = _sorted_dates[0] if _sorted_dates else None
                        if _lookup_date is not None:
                            try:
                                _gap_days = (datetime.strptime(prev_date, '%Y-%m-%d')
                                             - datetime.strptime(_lookup_date, '%Y-%m-%d')).days
                            except Exception:
                                _gap_days = 0
                            if _gap_days > _LOOKBACK_MAX_DAYS:
                                self.logger.warning(
                                    f"    [{fund_code}] {date} 指数 {related_index} 基准日 {prev_date} "
                                    f"最近数据在 {_lookup_date}（相隔 {_gap_days} 天 > {_LOOKBACK_MAX_DAYS}），"
                                    f"疑似指数源缺口，跳过（宁缺毋假）")
                                _lookup_date = None
                    if _lookup_date is None:
                        continue
                    prev_idx_close = idx_data[_lookup_date]
                    if not prev_idx_close or float(prev_idx_close) <= 0:
                        continue

                    # 汇率比率（用 _lookup_date 对齐指数基准日）
                    fx_ratio = 1.0
                    if fx_type != 'none':
                        # [AI-2026-10-07 东哥抓错] 德/法基金（513030/159561/513080）虽归 QDII欧美 批次(usd)，
                        #   但计价货币是欧元 —— fx_ratio 必须用 eur_cny_mid，否则用美元会让静态估值系统性偏离
                        #   （实测 159561 09-24：用美元算出 +0.37% 误差、改用欧元后 ~-0.03%，与 woody 的 0% 一致）。
                        #   口径与 common.py resolve_fx、dynamic_valuation.py 完全对称。缺失 eur 时 continue（宁缺毋假）。
                        _fx_col = {'usd': 'usd_cny_mid', 'jpy': 'jpy_cny_mid', 'hkd': 'hkd_cny_mid'}.get(fx_type, 'usd_cny_mid')
                        if fx_type == 'usd' and fund_code in EUR_FUND_CODES:
                            _fx_col = 'eur_cny_mid'
                        cursor.execute(f"SELECT {_fx_col} FROM exchange_rate WHERE date = ?", (date,))
                        curr_fx = cursor.fetchone()
                        cursor.execute(f"SELECT {_fx_col} FROM exchange_rate WHERE date = ?", (_lookup_date,))
                        prev_fx = cursor.fetchone()

                        curr_val = curr_fx[0] if curr_fx and curr_fx[0] else None
                        prev_val = prev_fx[0] if prev_fx and prev_fx[0] else None
                        if curr_val and prev_val and prev_val > 0:
                            fx_ratio = curr_val / prev_val
                        else:
                            continue

                    # 指数比率
                    idx_ratio = float(current_idx_close) / float(prev_idx_close)

                    # 静态估值
                    static_val = float(prev_nav) * (1 + pos_ratio * (idx_ratio * fx_ratio - 1))

                    # [AI-2026-07-13] 溢价率口径（东哥铁律；[AI-2026-08-05] 还原此前误改）：
                    #   QDII欧美(usd，跟美股有时差) → T价 / T-1净值(prev_nav)
                    #   QDII亚洲(hkd)/QDII日本(jpy)/国内LOF(none，无时差) → T价 / T净值(nav 同日)
                    # ⚠️ 严禁统一成 T-1：曾误改导致 QDII亚洲/日本/国内LOF 溢价口径错（严重错误，见 AGENTS.md TOP 2）
                    # [AI-2026-10-08 东哥抓 ping-pong] premium 精度口径统一为 **4 位小数**，公式结构也
                    #   与其余三个写手逐位对齐 —— 采集端 `_safe_save_fund_data`(round 4)、盘后对账
                    #   `fund_reconcile`(round 4)、step10 `arbcore/calculators/static_valuation.py`
                    #   全部是 `round((price - denom)/denom*100, 4)`。此前本处写「全精度 + (price/nav-1)*100」，
                    #   与上述三者浮点 ULP 级差异 ⇒ 下方 `_close(_, _, eps=1e-9)` 每轮都判「不等」
                    #   ⇒ 同一批行被反复 UPDATE（实测每轮约 24 行、数值等价）。统一后 `_close` 恒真、零重写。
                    premium = None
                    if price and float(price) > 0:
                        if fx_type == 'usd' and prev_nav and float(prev_nav) > 0:
                            premium = round((float(price) - float(prev_nav)) / float(prev_nav) * 100, 4)
                        elif float(nav) > 0:
                            premium = round((float(price) - float(nav)) / float(nav) * 100, 4)

                    # 估值误差
                    calibration = (static_val / float(nav) - 1) * 100 if float(nav) > 0 else None

                    # 指数涨幅
                    index_pct = (idx_ratio - 1) * 100

                    # 溢价率误差（估算溢价 - 实际溢价）
                    rt_premium = None
                    if price and float(price) > 0 and static_val > 0:
                        est_premium = (float(price) / static_val - 1) * 100
                        if premium is not None:
                            rt_premium = est_premium - premium

                    # [AI-2026-08-06] 仅当计算结果与已存值不一致时才写回，
                    # 避免每次启动全量历史重算产生无意义 UPDATE（非重复行，原即为 in-place UPDATE）
                    if (_close(static_val, ex_static) and _close(premium, ex_premium)
                            and _close(index_pct, ex_index_pct) and _close(calibration, ex_calib)
                            and _close(rt_premium, ex_rt_prem)):
                        continue  # 计算结果无变化，跳过写回

                    # [AI-2026-10-09] 打 writer 标识，供事后追查"这行静态估值/溢价是谁写的"
                    cursor.execute(
                        "UPDATE unified_fund_history SET static_val=?, premium=?, "
                        "index_pct=?, calibration=?, rt_premium=?, writer='lofarb:daily_updater.step11' "
                        "WHERE fund_code=? AND date=?",
                        (static_val, premium, index_pct, calibration, rt_premium,
                         fund_code, date))
                    if cursor.rowcount > 0:
                        count += 1

                # [AI-2026-10-08 东哥] 补算「官方净值尚未公布的最近交易日」(T-1) 的静态估值。
                #   上面主循环的 fund_rows 查询带 `nav IS NOT NULL` ⇒ 最新那个只有收盘价、
                #   净值未出的交易日根本不进循环 ⇒ static_val 恒留空 ⇒ 历史记录页最新一行
                #   「估值」永远空白（如 513080 法国ETF 2026-09-30）。用「最近已公布净值
                #   × (指数比 × 汇率比)」向前推一步补上；溢价 premium / 校准 calibration
                #   依赖当日官方净值 ⇒ 保持 NULL（宁缺勿假，与 159687 两步链同哲学）。
                #   范围限「最后一个已公布净值日之后」⇒ 天然只覆盖 1~2 个待补交易日。
                _pending = []
                try:
                    cursor.execute(
                        "SELECT date, static_val, index_pct FROM unified_fund_history "
                        "WHERE fund_code = ? AND nav IS NULL AND price IS NOT NULL "
                        "AND date > (SELECT MAX(date) FROM unified_fund_history "
                        "            WHERE fund_code = ? AND nav IS NOT NULL) "
                        "ORDER BY date", (fund_code, fund_code))
                    _pending = cursor.fetchall()
                except Exception:
                    _pending = []
                for _pdate, _p_ex_static, _p_ex_idxpct in _pending:
                    _p_idx = idx_data.get(_pdate)
                    if _p_idx is None or float(_p_idx) <= 0:
                        continue  # 当日指数无数据（指数市场休市 / 指数源缺口）⇒ 不推
                    cursor.execute(
                        "SELECT date, nav FROM unified_fund_history "
                        "WHERE fund_code = ? AND date < ? AND nav IS NOT NULL "
                        "ORDER BY date DESC LIMIT 1", (fund_code, _pdate))
                    _p_prev = cursor.fetchone()
                    if not _p_prev or not _p_prev[1] or float(_p_prev[1]) <= 0:
                        continue
                    _p_prev_date, _p_prev_nav = _p_prev
                    # 基准日指数（同主循环：允许回看 7 天内最近有数据的交易日，超窗 continue）
                    _p_lookup = _p_prev_date
                    if _p_lookup not in idx_data:
                        _p_cand = sorted([d for d in idx_data.keys() if d < _p_lookup], reverse=True)
                        _p_lookup = _p_cand[0] if _p_cand else None
                        if _p_lookup is not None:
                            try:
                                _p_gap = (datetime.strptime(_p_prev_date, '%Y-%m-%d')
                                          - datetime.strptime(_p_lookup, '%Y-%m-%d')).days
                            except Exception:
                                _p_gap = 0
                            if _p_gap > 7:
                                _p_lookup = None
                    if _p_lookup is None:
                        continue
                    _p_prev_idx = idx_data.get(_p_lookup)
                    if not _p_prev_idx or float(_p_prev_idx) <= 0:
                        continue
                    # 汇率比率（与主循环同口径：德/法基金用 eur_cny_mid，缺失即 continue）
                    _p_fx_ratio = 1.0
                    if fx_type != 'none':
                        _p_fx_col = {'usd': 'usd_cny_mid', 'jpy': 'jpy_cny_mid',
                                     'hkd': 'hkd_cny_mid'}.get(fx_type, 'usd_cny_mid')
                        if fx_type == 'usd' and fund_code in EUR_FUND_CODES:
                            _p_fx_col = 'eur_cny_mid'
                        cursor.execute(f"SELECT {_p_fx_col} FROM exchange_rate WHERE date = ?", (_pdate,))
                        _p_cf = cursor.fetchone()
                        cursor.execute(f"SELECT {_p_fx_col} FROM exchange_rate WHERE date = ?", (_p_lookup,))
                        _p_pf = cursor.fetchone()
                        _p_cv = _p_cf[0] if _p_cf and _p_cf[0] else None
                        _p_pv = _p_pf[0] if _p_pf and _p_pf[0] else None
                        if _p_cv and _p_pv and _p_pv > 0:
                            _p_fx_ratio = _p_cv / _p_pv
                        else:
                            continue
                    _p_idx_ratio = float(_p_idx) / float(_p_prev_idx)
                    _p_static = float(_p_prev_nav) * (1 + pos_ratio * (_p_idx_ratio * _p_fx_ratio - 1))
                    _p_idx_pct = (_p_idx_ratio - 1) * 100
                    if _close(_p_static, _p_ex_static) and _close(_p_idx_pct, _p_ex_idxpct):
                        continue  # 无变化，跳过写回
                    # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 T-1 待补静态估值是谁写的"
                    cursor.execute(
                        "UPDATE unified_fund_history SET static_val = ?, index_close = ?, index_pct = ?, writer = 'lofarb:daily_updater.step11.pending' "
                        "WHERE fund_code = ? AND date = ?",
                        (_p_static, float(_p_idx), _p_idx_pct, fund_code, _pdate))
                    if cursor.rowcount > 0:
                        count += 1

                total_updated += count
                if count > 0:
                    self.logger.info(f"    [{fund_code}] 更新 {count} 条")

        process_batch(a_share, "A股指数/LOF (无汇率)", fx_type='none')
        process_batch(hk_funds, "QDII亚洲 (港币)", fx_type='hkd')
        process_batch(us_funds, "QDII欧美 (美元)", fx_type='usd')
        process_batch(jp_funds, "QDII日本 (日元)", fx_type='jpy')

        conn.commit()
        conn.close()
        self.logger.info(f"✅ [简单估值] 完成，共更新 {total_updated} 条记录")

    # ================================================================
    # 步骤十二：期货驱动基金官方估值（历史表 static_val 列）
    # 公式: static_val(D) = NAV(D-1) × 期货结算价(D) / 期货结算价(D-1)
    # 驱动源 = futures_daily（新浪官方结算价，每日盘后入库；历史可用 backfill_futures_history.py 回填）
    #   - 161226 白银 ← AG0（沪银连续，上期所）
    #   - 159985 豆粕 ← M0（豆粕连续，大商所）
    # 与主面板实时官方估值 nav_home × 今结算/昨结算 同源：
    #   - 主面板盘中用 VWAP 替身；历史闭合日用当日官方结算价 settle(D) 替代（历史无 VWAP 概念）
    #   - 二者均"以昨结算为基准、用当日期货比值缩放昨净值"，故数字与真实 NAV 接近，用于和 NAV 对比
    # 严格不 fallback：NAV(D-1)/settle(D)/settle(D-1) 任一为 0 或空 → 留 NULL（绝不拿 NAV 冒充）
    # ================================================================
    # [AI-2026-10-11 东哥定调] 泛化为「期货驱动」通用步骤：新增期货品种基金只需在下方登记 (fund_code, symbol)
    FUTURES_STATIC_TARGETS = (
        ('161226', 'AG0'),   # 白银 LOF ← 沪银连续
        ('159985', 'M0'),    # 豆粕 ETF ← 豆粕连续
    )

    def step12_futures_static_valuation(self, recent_days=None):
        """步骤十二：期货驱动基金官方估值（历史表 static_val 列）。逐只调用通用内核。"""
        self.logger.info("=== 步骤十二：期货官方估值 (历史表 static_val 列) ===")
        for fund_code, symbol in self.FUTURES_STATIC_TARGETS:
            self._futures_static_valuation_one(fund_code, symbol, recent_days)

    def _futures_static_valuation_one(self, fund_code, symbol, recent_days=None):
        """通用内核：static_val(D) = NAV(D-1) × settle(D) / settle(D-1)（settle 取自 futures_daily[symbol]）"""
        import sqlite3

        # [AI-2026-08-16] 活库移出仓库根（物理隔离防泄漏）
        # [AI-2026-10-06] 搬入 src/ 后层数全变，改走 arbcore 统一解析（env ARB_MASTER_DB 优先 → _shared → database/ 上溯）
        from arbcore.database.db_manager import _resolve_db_path
        db_path = _resolve_db_path()
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        cursor = conn.cursor()

        # 1. 取该基金的 NAV 序列（按日期升序）
        cursor.execute(
            "SELECT date, nav FROM unified_fund_history WHERE fund_code=? ORDER BY date", (fund_code,))
        nav_rows = cursor.fetchall()

        # 2. 取期货结算价序列（过滤掉 0/None，避免脏分母）
        cursor.execute(
            "SELECT date, settle_price FROM futures_daily WHERE symbol=? ORDER BY date", (symbol,))
        fut_rows = cursor.fetchall()
        fut_settle = {d: float(s) for d, s in fut_rows if s is not None and float(s) > 0}

        if not nav_rows or not fut_settle:
            self.logger.warning(f"   [{fund_code}] NAV 或 {symbol} 数据不足，跳过")
            conn.close()
            return

        # 3. 逐日计算：prev_nav 作 NAV(D-1)，分母取「最近一个 <D 且有期货结算价」的交易日
        # [AI-2026-10-09 修复] 旧写法用 NAV 序列相邻行做 D-1，国庆休市(10-07)时 10-08 的
        #   prev_date=09-30 虽能命中结算价，但中间跨了休市；且历史陈旧值(如 1.6456)未覆盖。
        #   现改为：分母 sp = 结算价中「严格小于 date 的最大日期」的结算价（最近有结算价的交易日），
        #   分子 sd = 结算价[date]。跨休市/长假都能取到正确分母（10-08 的分母=09-30）。
        #   无结算价的 D（date not in fut_settle）跳过；D 之前无任何结算价也跳过。
        fut_sorted_dates = sorted(fut_settle.keys())
        updates = []
        prev_nav = None
        today_str = datetime.now().strftime('%Y-%m-%d')
        for date, nav in nav_rows:
            # [AI-2026-08-21] 今天这一行不写历史估值：盘中未收盘、结算价为昨结算，写进历史表会误导
            if date != today_str and prev_nav is not None and prev_nav > 0 and date in fut_settle:
                sd = fut_settle[date]
                # 分母：最近一个严格小于 date 且有结算价的交易日
                sp = None
                for d in reversed(fut_sorted_dates):
                    if d < date:
                        sp = fut_settle[d]
                        break
                if sp is not None and sd > 0 and sp > 0:
                    updates.append((round(prev_nav * sd / sp, 4), date))
            prev_nav = nav if nav is not None else prev_nav

        if not updates:
            self.logger.info(f"   [{fund_code}] 无可用期货官方估值计算")
            conn.close()
            return

        # 4. recent_days 优化：仅保留最近 N 天
        if recent_days:
            cutoff = (datetime.now() - timedelta(days=recent_days)).strftime('%Y-%m-%d')
            updates = [(v, d) for v, d in updates if d >= cutoff]

        # 5. 写回 static_val（专属列，不碰其他字段）
        count = 0
        for val, date in updates:
            # [AI-2026-10-09] 打 writer 标识，供事后追查"这行 static_val 是谁写的"
            cursor.execute(
                "UPDATE unified_fund_history SET static_val=?, writer='lofarb:daily_updater.step12' WHERE fund_code=? AND date=?",
                (val, fund_code, date))
            if cursor.rowcount > 0:
                count += 1
        conn.commit()
        conn.close()
        self.logger.info(f"✅ [{fund_code}] 期货官方估值({symbol}) 更新 {count} 条 static_val")

    def run(self, nav_only=False, refresh_morning=False, static_valuation=False, daily_close=False):
        today_str = datetime.now().strftime('%Y-%m-%d')
        now = datetime.now()

        if static_valuation:
            self.logger.info("🚀 [静态估值模式] 仅执行静态估值计算 (step10 + step11 + step12)...")
            self._step10_calculate_static_valuation()
            self.step11_simple_static_valuation()
            self.step12_futures_static_valuation()
            self.logger.info("🎉 [静态估值模式] 静态估值计算完成！")
            return

        # ── 统一「非交易日停跑」门控（[AI-2026-10-11 东哥拍板]）──────────────────────
        # 东哥定：**周末 / 法定假期停跑全部"自动"模式** —— 默认（无参，后端启动触发）、
        #   `--nav-only`（18:00/19:30/21:00）、`--daily-close`（盘后兜底）、`--refresh-morning`（09:20）。
        #   显式手动模式 `--static-valuation`（上面那支，12 号按钮）**放行** —— 人要看就能看。
        # 判据 = **A股交易日历** `is_trading_day('A_SHARE')`（覆盖调休与国庆/春节等法定假期）；
        #   日历不可用时退回「是否周末」。
        # 回退开关：设环境变量 `ARB_WEEKEND_PIPELINE=1` 并重启后端 → 恢复旧行为（非交易日照跑）。
        try:
            _is_cn_td = is_trading_day('A_SHARE', now.date())
        except Exception as _e_td:
            self.logger.warning(f"[交易日门控] A股日历不可用({_e_td})，退回周末判断")
            _is_cn_td = now.weekday() < 5
        if not _is_cn_td and os.environ.get('ARB_WEEKEND_PIPELINE', '0') != '1':
            # [AI-2026-10-12 东哥拍板] 非交易日不再"直接停跑"，改为轻量补拉后停跑：
            #   东哥定"周末也需要爬数据"—— VPS crontab 每天跑（docs/002_1:46 `* * *`），
            #   周五美股数据必须周六清晨收（etf_nav due：昨日=周五美交易日）。
            #   此前门控直接 return，导致周六 VPS 文件从未被本机拉取 → XOP 等 QDII 周五净值
            #   一直缺到下周一。现改为：非交易日执行 _weekend_vps_pull_only()（step5+5b+5c+静态重算）再返回。
            self.logger.info(
                f"📅 [{now.strftime('%Y-%m-%d')}] 非A股交易日 → 轻量补拉（step5美股+5b指数+5c净值+静态重算）后停跑。"
                f"如需强制全量运行：设 ARB_WEEKEND_PIPELINE=1 后重启。")
            self._weekend_vps_pull_only()
            return

        if daily_close:
            self.logger.info("🚀 [收盘后更新] 仅写当日官方收盘价 + 净值 + 静态估值（公共API，不依赖VPS）...")
            # [2026-11-09 方案A·东哥拍板] 盘后走「公共日K必要通道」，不进 VPS-first：
            # VPS prices_{T} 要 T+1 03:00 才产，此刻走 VPS 会让 main.py 盘后循环空转到次日。
            # 这里与 run() 分支原有的「公共API，不依赖VPS」注释一致 —— 10-11 改判据时被误伤。
            self._step4_fetch_prices(local_kline_only=True)
            self.step4_fetch_lof_market()
            # [AI-2026-08-31] AG0 官方结算价**只在盘后产生**：盘中(<15:00) futures_daily 不写 AG0
            # （VWAP 不能当结算价，也不拿昨结算兜底）。故收盘后必须再抓一次期货数据，
            # 否则当天的官方结算价永远写不进 futures_daily，白银静态估值会长期缺失。
            # 清掉早晨的 futures_data 标记，防止 step8 被"今日已同步"跳过。
            self.db.remove_access_sync_status(today_str, 'futures_data')
            self.step8_fetch_sina_futures_from_vps()
            self._step10_calculate_static_valuation()
            self.step11_simple_static_valuation(recent_days=5)
            self.step12_futures_static_valuation(recent_days=5)
            # [AI-2026-09-24 东哥拍板] 原油三基金持仓静态估值**已从本分支移出**。
            # 原因：美股 04:00(北京) 收盘，清晨刷新(08:00) step5 抓到 T-1 收盘价后即可算；
            # 而白天 A 股时段的实时估值正需要这份持仓静态估值 —— 等 15:00 盘后再算已毫无意义。
            # 新触发点：refresh_morning 分支（见下方）。本分支保留 step13 做「季报持仓推 ARM + 拉回」。
            self.step13_oil_data_sync()
            self.logger.info("🎉 [收盘后更新] 收盘价/净值/静态估值已更新！")
            return

        if refresh_morning:
            self.logger.info("🚀 [清晨刷新] 清除9:20前旧标记，重新抓取 Woody/汇率/VPS 上午数据...")
            if now.weekday() in (5, 6):
                self.logger.info("📅 周末跳过。")
                return
            # [AI-2026-10-12 修 Q1 双闸] 必须连「VPS 来源标记」一起清 —— 否则 09:20 的权威文件
            #   ① 被 step1 的 VPS 来源闸 (woody_vps_ingested) 判定为「已入库」而跳过；
            #   ② 被 _try_sync_all_from_vps 的 per-file 标记 (woody_vps_sync) 挡在门外（连文件都不加载）。
            #   三个一起清，本次刷新才会真正重取 VPS 文件并用它覆盖凌晨直连 API 抢跑的数据。
            for src in ['woody_lof_batch', 'woody_vps_sync', 'woody_vps_ingested',
                        'official_exchange_rate', 'futures_data', 'jsl_shares_data']:
                self.logger.info(f"🗑️ 清除 {src} 标记")
                self.db.remove_access_sync_status(today_str, src)
            self._run_pipeline()
            # [AI-2026-09-24 东哥拍板] 原油三基金持仓静态估值：改由「清晨刷新」触发。
            # 美股 04:00(北京) 收盘 → _run_pipeline 的 step5 抓到 T-1 收盘价 → 立即重算，
            # 供白天 A 股时段的实时估值使用；不再等 15:00 盘后（那时已无意义）。
            # 放在 _run_pipeline() **之后**：必须先有当日美股价才能算。
            #   - ARM 分支：自算并落 ARM 库（权威源头）；
            #   - 本机分支：跳过（本机只靠 step13 从 ARM 拉回）。
            # ⚠️ 代码保留不删除（故障兜底）：ARM 死机/不可达时，去掉下面的 if 改为无条件调用
            #    `self._step_oil_recalc()` 即可让本机恢复自算（改完重启本机后端）。
            if _is_arm_runtime():
                self._step_oil_recalc()
            self.logger.info("🎉 [清晨刷新] Woody/汇率/VPS 数据已重新同步！")
            return

        if nav_only:
            self.logger.info("🚀 [NAV模式] 仅执行净值更新 (step4)...")
            self.step4_fetch_lof_market()
            self.logger.info("🎉 [NAV模式] 净值更新完毕！")
            return

        # 默认：完整流水线
        # [AI-2026-10-11 东哥拍板] 非交易日已在**函数入口统一拦掉**（见上「统一门控」）。
        #   下面这段"周末轻量块"仅在回退开关 ARB_WEEKEND_PIPELINE=1 放行时才可达：
        #   净值+收盘价+Woody补采+美股补采+静态估值（原为「防外出多日漏采」而设）。
        if now.weekday() in (5, 6):
            self.logger.info("📅 [周末·回退开关已开] 执行轻量块：净值+收盘价+Woody补采+静态估值+美股补采...")
            self.step4_fetch_lof_market()
            # [AI-2026-07-25] 周末轻量块也取收盘价，否则 LOF 收盘价缺失
            # [2026-11-09 方案A] 此处仍走 VPS-first（默认）：该块的目的是补历史 VPS 文件，
            #   不是写「T 日盘后价」；盘后腾讯通道已独立到 --daily-close，别混。
            self._step4_fetch_prices()
            # [AI-2026-07-25] 周末也跑 step1：从 VPS 补采缺失的 Woody 因子数据
            self.step1_and_2_fetch_woody_api()
            # [AI-2026-07-25] 周末也跑 step5：补采上次遗漏的美股 ETF 数据
            self.step5_fetch_usa_market_data(weekend_mode=True)
            # [AI-2026-10-11 东哥报缺] 该轻量块须含 step11/step12 —— 否则非篮子基金（跟踪指数
            #   公式的 ETF/LOF）与期货类基金 static_val 全空，基金详情「历史误差」整列空白（如 159659）。
            self._step10_calculate_static_valuation()
            self.step11_simple_static_valuation(recent_days=5)
            self.step12_futures_static_valuation(recent_days=5)
            self.logger.info("🎉 [周末净值+Woody补采+静态估值+美股补采] 更新完毕！")
            return
        self._run_pipeline()

    def _run_pipeline(self):
        self.logger.info("🚀 开始执行每日数据大一统更新流水线...")
        self.step1_and_2_fetch_woody_api()
        self.step2_5_sync_yaml_with_latest_factors()
        self.step3_fetch_exchange_rate()
        # [AI-2026-06-28] 价格+净值分开，nav-only 只拉净值
        # [AI-2026-07-23] 删除 _step4_fix_holiday_prices() — 数据管道已稳定，全量历史价格核对不再必要
        self._step4_fetch_prices()
        self.step4_fetch_lof_market()
        self.step4_5_sync_fund_purchase_status()
        self.step5_fetch_usa_market_data()
        self.step5b_sync_vps_index_data()
        self.step5b2_record_hssi_daily()
        self.step5c_sync_vps_etf_nav()
        self.step7_fetch_extra_calibrations()
        self.step8_fetch_sina_futures_from_vps()
        self.step9_fetch_jsl_shares_from_vps()
        self._step10_calculate_static_valuation()
        self.step11_simple_static_valuation(recent_days=5)
        self.step12_futures_static_valuation(recent_days=5)
        self.step13_oil_data_sync()
        self.logger.info("🎉 流水线执行完毕，数据大盘一切就绪！")

    def _weekend_vps_pull_only(self):
        """[AI-2026-10-12 东哥拍板] 非交易日（周六为主）轻量补拉 step5b+step5c，不跑完整流水线。

        动机：VPS crontab 每天跑（`* * *`，docs/002_1:46），周五美股数据必须**周六清晨**收。
        此前门控直接 return，周六从未拉 VPS → XOP 等 QDII 周五净值一直缺到下周一。

        due 判定（_try_sync_all_from_vps 内部按 file_date 幂等）：
          - step5c etf_nav：昨日=周五美交易日 → 周六 due，补拉周五净值 ✓
          - step5b index：周六非日/欧交易日 → 不 due（no-op），但周五欧/日指数仍会补到 ✓
        每步独立 try/except，单步失败（VPS 不可达等）不中断；重复跑安全（幂等）。
        """
        self.logger.info("🛰️ [周末轻量补拉] step5b(指数) + step5c(ETF净值)...")
        for name, fn in (("step5b", self.step5b_sync_vps_index_data),
                         ("step5c", self.step5c_sync_vps_etf_nav)):
            try:
                fn()
            except Exception as _e:
                self.logger.warning(f"⚠️ [周末轻量补拉] {name} 失败（不中断）: {_e}")
        self.logger.info("🎉 [周末轻量补拉] 完成")

    def step13_oil_data_sync(self):
        """[AI-2026-09-23 B方案] 原油三基金数据同步（替代旧 step13_sync_oil_to_arm）。

        ① 季报持仓 本机→ARM（sync_report_holdings_to_arm，季度自动覆盖）；
        ② holding_static_val ARM自算→本地拉回（pull_oil_static_from_arm，日更）。
        失败仅记日志不中断流水线。复用 HoldingService.sync_oil_to_arm（SSH 通道）。

        [AI-2026-10-03 东哥点名修复] **仅本机有效**：ARM 自身即权威源，在 ARM 上跑只会得到
        `ssh: Could not resolve hostname arm`（arm 是本机 ~/.ssh/config 别名，ARM 无此解析），
        每次白跑 2 次失败 SSH。与 _step_oil_recalc「仅 ARM 运行时调用」互为镜像，ARM 侧直接跳过。
        """
        if _is_arm_runtime():
            self.logger.info("ℹ️ [step13] ARM 运行时跳过（自身即权威源，无需推拉）")
            return
        try:
            sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
            from services.holding_service import HoldingService
            svc = HoldingService(self.db)
            r = svc.sync_oil_to_arm()
            if r.get("status") == "ok":
                self.logger.info(f"✅ [step13] 原油数据同步完成: "
                                 f"holdings推ARM={r['report_holdings'].get('updated')} 行, "
                                 f"static从ARM拉回={r['holding_static'].get('updated')} 行")
            else:
                self.logger.warning(f"⚠️ [step13] 原油数据同步异常: {r}")
        except Exception as e:
            self.logger.warning(f"⚠️ [step13] 原油数据同步失败(不中断流水线): {e}")

    def _step_oil_recalc(self):
        """[AI-2026-09-24] ARM 端原油三基金持仓静态估值自算（get_recalc_history 落 ARM 库）。

        触发点：**清晨刷新**（refresh_morning 分支；ARM 上 08:00）。
        美股 04:00(北京) 收盘 → step5 抓到 T-1 收盘价 → 立即重算，供白天 A 股时段实时估值使用。
        ARM 数据全齐(季报持仓/美股ETF价/LOF净值/汇率)即可算，无需本机推送。
        仅 ARM 运行时调用；本机只靠 step13_oil_data_sync 从 ARM 拉回。
        历史：2026-09-23 曾在 daily_close(15:00 盘后) 分支触发，因对白天实时估值无意义已移至清晨。
        """
        try:
            sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
            from services.holding_service import HoldingService
            svc = HoldingService(self.db)
            for code in ("160723", "161129", "501018"):
                r = svc.get_recalc_history(code, start="2026-01-01")
                if isinstance(r, dict) and r.get("error") is None:
                    self.logger.info(f"✅ [oil_recalc] {code}: {r.get('count', 0)} 行")
                else:
                    self.logger.warning(f"⚠️ [oil_recalc] {code}: {r}")
        except Exception as e:
            self.logger.warning(f"⚠️ [oil_recalc] 异常(不中断流水线): {e}")

# [AI-2026-10-03 东哥拍板] daily_updater 日志保留上限（个）。
# 口径 = 最近 10 个运行日，与 ARM systemd journald 的 MaxRetentionSec=10day 对齐。
DAILY_LOG_RETAIN = 10


def prune_daily_logs(keep: int = DAILY_LOG_RETAIN, log_dir: str = None) -> int:
    """[AI-2026-10-03 东哥拍板] 清理 daily_updater_YYYYMMDD.log，只保留最近 keep 个。

    背景：该日志此前**无任何清理机制**，只增不减 —— 正常交易日 ~200KB，但 A股法定
    假期的「盘后兜底」循环曾把单日日志顶到 12.7MB(10-01) / 10.3MB(10-02)（该循环已
    在 main.py `_after_hours_close_price_loop` 加假期门控修复）。日志按 %Y%m%d 命名，
    多个进程并发追加同一文件 ⇒ 直接按文件名日期排序取最新 keep 个即可。

    规则：仅匹配 `daily_updater_<8位数字>.log`，**绝不动** stdout_*.log / 其他 app 日志。
    幂等、无状态、零依赖：每次 daily_updater 进程启动调用一次；旧文件可能被并发进程
    先行删除 ⇒ 吞掉 FileNotFoundError，任何异常都不得打断主流程。

    Returns: 本次实际删除的文件数（异常时返回 0）。
    """
    if log_dir is None:
        log_dir = PROJECT_LOGS_DIR
    try:
        pat = re.compile(r'^daily_updater_(\d{8})\.log$')
        dated = []
        for fn in os.listdir(log_dir):
            m = pat.match(fn)
            if m:
                dated.append((m.group(1), fn))
        dated.sort()  # YYYYMMDD 升序
        if keep <= 0:
            stale = dated
        else:
            stale = dated[:-keep]  # 最旧的 (总数 - keep) 个
        removed = 0
        for _d, fn in stale:
            try:
                os.remove(os.path.join(log_dir, fn))
                removed += 1
            except FileNotFoundError:
                pass
        return removed
    except Exception:
        return 0


if __name__ == "__main__":
    import argparse
    import subprocess

    parser = argparse.ArgumentParser(description="ArbTest 日度数据流水线")
    parser.add_argument("--nav-only", action="store_true", help="仅更新基金净值 (step4)")
    parser.add_argument("--refresh-morning", action="store_true", help="清除上午标记后重新抓取 Woody/汇率/VPS")
    parser.add_argument("--static-valuation", action="store_true", help="仅计算静态估值 (step10 + step11)")
    parser.add_argument("--daily-close", action="store_true", help="收盘后专用: 写当日收盘价+净值+静态估值(公共API)")
    args = parser.parse_args()

    # 进程互斥锁（跨平台）：用 PID 文件防多实例
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_updater.lock")

    def _is_pid_alive(pid):
        try:
            if os.name == 'nt':
                # 中文Windows下tasklist输出GBK，text=True默认utf-8会解码崩溃，故用errors='ignore'
                result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                       capture_output=True, encoding='gbk', errors='ignore', timeout=5)
                return str(pid) in (result.stdout or '')
            else:
                os.kill(pid, 0)
                return True
        except Exception:
            return False

    locked = False
    try:
        with open(lock_path, 'x') as f:
            f.write(str(os.getpid()))
        locked = True
    except FileExistsError:
        try:
            with open(lock_path, 'r') as f:
                old_pid = int(f.read().strip())
            if _is_pid_alive(old_pid):
                print(f"[SKIP] 另一个 daily_updater 实例正在运行 (PID {old_pid})，退出。")
                sys.exit(0)
            # 旧进程已死，抢锁
            with open(lock_path, 'w') as f:
                f.write(str(os.getpid()))
            locked = True
        except (ValueError, FileNotFoundError):
            with open(lock_path, 'w') as f:
                f.write(str(os.getpid()))
            locked = True

    try:
        # [AI-2026-10-03 东哥拍板] 启动即清理历史日志（保留最近 DAILY_LOG_RETAIN 个），幂等无副作用。
        # 放在 lock 之后：只由真正要干活的实例执行，被 [SKIP] 挡掉的实例不做无谓 IO。
        _n_pruned = prune_daily_logs()
        if _n_pruned:
            print(f"[LOG-PRUNE] 已清理 {_n_pruned} 个历史 daily_updater 日志（保留最近 {DAILY_LOG_RETAIN} 个）")
        DailyUpdater().run(nav_only=args.nav_only, refresh_morning=args.refresh_morning, static_valuation=args.static_valuation, daily_close=args.daily_close)
    finally:
        if locked:
            try:
                os.remove(lock_path)
            except OSError:
                pass
