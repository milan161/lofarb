#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[AI-2026-10-11] 本机/ARM 告警通道（Telegram，支持经 ARM 转发）。

背景（东哥 2026-10-11 拍板）：
  本地/ARM 依赖东京 VPS 采集的历史数据；**失败即报警 + 人工介入，不做 fallback** ——
  兜底会掩盖 VPS 故障、并制造"第二个真相源"。故先建「告警」这一层。

通道拓扑（2026-10-11 实测）：
  · **ARM（美国）→ 直连 api.telegram.org ✅**（HTTP 302）
  · **本机（国内）→ 直连不通**（HTTP 000 超时）⇒ 自动回退「经 `ssh arm` 转发」；
    token 不落命令行（由 ARM 端自行读 secrets）。
  · 凭据走仓库外保险箱 account_info/secrets.py（ARM/Local 同一套读法），绝不硬编码。
  · 连续失败 → 5 分钟熔断（避免本机无通道时每条告警都干等）。
  · 全程失败只记日志、**绝不影响主流程**。

用法：
    from services.alert_service import send_alert
    send_alert("东京 VPS SSH 连不上", level="error", key="vps_fail_prices")
"""
import base64
import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("alert")

# [AI-2026-10-14] 冷却窗口默认 6h（21600s），可用环境变量 LOFARB_ALERT_COOLDOWN 覆盖。
# 6h 既能瞬间压住「15 分钟 40+ 条」的突发轰炸，又保留持续故障的每日心跳提醒。
COOLDOWN = int(os.environ.get("LOFARB_ALERT_COOLDOWN", "21600"))

# [AI-2026-10-14] 持久化去重中继：跨进程/跨后端重启生效，根治 40+ 条轰炸。
# alert_service 是所有 LOFarb Telegram 告警的唯一出口，去重在此处做最彻底。
try:
    from services.alert_dedup import (
        is_allowed as _dd_allowed,
        mark_sent as _dd_mark,
        dedup_key as _dd_key,
    )
except Exception:  # pragma: no cover - 兜底：去重模块缺失时绝不吞掉告警
    try:
        from .alert_dedup import (
            is_allowed as _dd_allowed,
            mark_sent as _dd_mark,
            dedup_key as _dd_key,
        )
    except Exception:
        def _dd_allowed(k, c=21600):
            return True
        def _dd_mark(k):
            pass
        def _dd_key(pk=None, t=None):
            return pk or "h:unknown"
SSH_ALIAS = "arm"       # 本机 ~/.ssh/config 别名（免密）
ARM_SECRETS = "/home/ubuntu/account_info/secrets.py"
# [AI-2026-10-11 东哥要求] 本机(Windows/国内) 直连 Telegram 必然失败（被墙）
# ⇒ 直接走 ARM 转发，不再白等 6s 超时。ARM(Linux) 上仍先试直连。
_IS_LOCAL = (os.name == "nt")

_broken_until = 0.0     # 熔断截止 ts
_direct_ok = None       # None=未知 / True=直连可用 / False=直连不通（改走 ARM 转发）


def _load_secrets_module():
    """从仓库外 account_info/secrets.py 加载统一凭据读取器（跨机：ARM/Local）。"""
    import importlib.util as ilu
    for cand in (Path("/home/ubuntu/account_info/secrets.py"),
                 Path(r"D:\Study\ARB\account_info\secrets.py")):
        if cand.exists():
            spec = ilu.spec_from_file_location("_unified_secrets", str(cand))
            m = ilu.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    return None


_SECRETS = _load_secrets_module()
TELEGRAM_BOT_TOKEN = _SECRETS.get_secret("TELEGRAM_BOT_TOKEN", "") if _SECRETS else ""
TELEGRAM_CHAT_ID = _SECRETS.get_secret("TELEGRAM_CHAT_ID", "") if _SECRETS else ""


def _enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _post_direct(text: str) -> bool:
    """本机/ARM 直连 Telegram。本机（国内）通常失败（返回 False）。"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    cmd = ["curl", "-s", "--max-time", "6", "-X", "POST", url,
           "--data-urlencode", f"chat_id={TELEGRAM_CHAT_ID}",
           "--data-urlencode", "parse_mode=Markdown",
           "--data-urlencode", f"text={text}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        resp = json.loads(r.stdout) if r.stdout else {}
        return bool(resp.get("ok"))
    except Exception as e:
        log.warning("直连 Telegram 异常: %s", e)
        return False


def _post_via_arm(text: str) -> bool:
    """经 ARM（美国，可直连）转发。token 由 ARM 端自行读 secrets，不落命令行。"""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    remote_py = (
        "import base64,subprocess,importlib.util as iu;"
        f"sp=iu.spec_from_file_location('sec','{ARM_SECRETS}');"
        "m=iu.module_from_spec(sp);sp.loader.exec_module(m);"
        "tok=m.get_secret('TELEGRAM_BOT_TOKEN','');cid=m.get_secret('TELEGRAM_CHAT_ID','');"
        f"t=base64.b64decode('{b64}').decode('utf-8');"
        "u='https://api.telegram.org/bot'+tok+'/sendMessage';"
        "print(subprocess.run(['curl','-s','--max-time','8','-X','POST',u,"
        "'--data-urlencode','chat_id='+cid,'--data-urlencode','parse_mode=Markdown',"
        "'--data-urlencode','text='+t],capture_output=True,text=True).stdout)"
    )
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", SSH_ALIAS,
             'python3 -c "' + remote_py + '"'],
            capture_output=True, text=True, timeout=45)
        out = (r.stdout or "").strip()
        if out:
            return bool(json.loads(out).get("ok"))
        log.warning("ARM 转发无输出: %s", (r.stderr or "")[:200])
    except Exception as e:
        log.warning("ARM 转发异常: %s", e)
    return False


def send_alert(msg: str, level: str = "warn", key: str = None) -> bool:
    """发送告警到 Telegram。返回是否已发送。

    链路：① 直连 → ② 失败则经 ARM 转发 → ③ 都失败则熔断 5 分钟。
    被防刷 / 未配置凭据 / 发送失败 → 返回 False（均只记日志，绝不抛错）。
    level: info | warn | error；key: 防刷键（COOLDOWN 秒内只发一次），None 按正文 hash 兜底。
    去重状态落盘（alert_dedup），跨进程/跨重启生效，根治 40+ 条轰炸。
    """
    global _broken_until, _direct_ok
    icon = {"info": "ℹ️", "warn": "⚠️", "error": "🚨"}.get(level, "⚠️")
    text = f"{icon} [LOFarb·{level.upper()}] {msg}"

    if time.time() < _broken_until:
        log.warning("告警通道熔断中(剩余%ds)，跳过: %s", int(_broken_until - time.time()), msg)
        return False
    # [AI-2026-10-14] 持久化去重：跨进程/跨重启生效，根治 40+ 条轰炸
    dk = _dd_key(key, text)
    if not _dd_allowed(dk, COOLDOWN):
        log.info("告警被防刷跳过(持久去重 key=%s): %s", dk, msg)
        return False
    if not _enabled():
        log.warning("告警未配置 Telegram 凭据，仅记日志: %s", text)
        return False

    ok = False
    if not _IS_LOCAL and _direct_ok is not False:   # 本机跳过直连（明知被墙，不等超时）
        ok = _post_direct(text)
        _direct_ok = ok
        if ok:
            log.info("告警已发送(直连): %s", text)
    if not ok:                            # 直连不通（本机被墙）→ 经 ARM 转发
        ok = _post_via_arm(text)
        if ok:
            log.info("告警已发送(经 ARM 转发): %s", text)

    if ok:
        _dd_mark(dk, COOLDOWN)
        return True

    _broken_until = time.time() + 300
    log.error("告警发送失败（直连 + ARM 转发均失败，已熔断 5 分钟）: %s", text)
    return False
