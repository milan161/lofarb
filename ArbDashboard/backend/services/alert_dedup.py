#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[AI-2026-10-14] LOFarb Telegram 告警去重中继（持久化）。

所有经 alert_service 发出的告警都先过本模块：同一 key（或同内容 hash）在
COOLDOWN 窗口内只允许放一条。状态落盘（默认 ~/arbtest/data/alert_dedup.json），
跨进程 / 跨后端重启 生效 —— 根治「后端因异常崩溃重启 → 内存去重清零 → 同一条
告警狂发 BOT」的 40+ 条轰炸问题（2026-10-13 实证：单日 995 次 VPS-MISS 调用、
cron 仅跑 1 次，说明是后端进程内循环 + 崩溃重启反复清零导致）。

冷却窗口默认 6h（21600s），可用环境变量 LOFARB_ALERT_COOLDOWN 覆盖（单位秒）。
6h 既能瞬间压住「15 分钟 40+ 条」的突发轰炸，又保留持续故障的每日心跳提醒。

用法（被 alert_service 调用）：
    from services.alert_dedup import is_allowed, mark_sent, dedup_key

命令行自测：
    python3 alert_dedup.py "东京 VPS 今日无数据" --key vps_miss_prices
    # 首次退出码 0（放行），6h 内再跑退出码 1（抑制）
    python3 alert_dedup.py --reset          # 清空全部去重状态
"""
import hashlib
import json
import os
import sys
import time

DEFAULT_COOLDOWN = int(os.environ.get("LOFARB_ALERT_COOLDOWN", "21600"))  # 6 小时
DEDUP_FILE = os.environ.get(
    "LOFARB_ALERT_DEDUP_FILE",
    "/home/ubuntu/arbtest/data/alert_dedup.json",
)

# 内存快路径（同进程内省一次文件读）
_mem = {}


def store_path() -> str:
    return DEDUP_FILE


def _load() -> dict:
    try:
        with open(store_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(store: dict) -> None:
    try:
        p = store_path()
        parent = os.path.dirname(p) or "."
        os.makedirs(parent, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f)
        os.replace(tmp, p)
    except Exception:
        pass  # 落盘失败不阻断主流程，仅退化为内存去重


def dedup_key(provided_key: str = None, text: str = None) -> str:
    """去重键：优先调用方显式 key；否则用消息正文 hash。

    注意：带时间戳的消息（如 '现已 15:00'）每次 hash 都不同，无法跨次去重，
    故调用方应尽量传稳定 key（daily_updater 的 vps_miss_* 已做到）。
    """
    if provided_key:
        return "k:" + provided_key
    if text:
        return "h:" + hashlib.md5(text.encode("utf-8")).hexdigest()
    return "h:unknown"


def is_allowed(key: str, cooldown: int = DEFAULT_COOLDOWN) -> bool:
    """是否允许发送（未超冷却期返回 False）。仅查询，不落盘。"""
    now = time.time()
    if key in _mem and (now - _mem[key] < cooldown):
        return False
    store = _load()
    if key in store and (now - float(store[key]) < cooldown):
        _mem[key] = float(store[key])
        return False
    return True


def mark_sent(key: str, cooldown: int = DEFAULT_COOLDOWN) -> None:
    """发送成功后记录时间戳（内存 + 落盘），并顺手 prune 过期键。"""
    now = time.time()
    _mem[key] = now
    store = _load()
    store[key] = now
    # 仅保留 4 倍冷却期内的键，避免文件无限增长
    cutoff = now - cooldown * 4
    store = {k: v for k, v in store.items() if float(v) >= cutoff}
    _save(store)


def reset(key: str = None) -> None:
    """手动清去重状态（调试用）。key=None 清空全部。"""
    if key is None:
        try:
            os.remove(store_path())
        except Exception:
            pass
        _mem.clear()
    else:
        store = _load()
        store.pop(key, None)
        _save(store)
        _mem.pop(key, None)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="LOFarb 告警去重中继（自测）")
    ap.add_argument("message", nargs="?", default="", help="告警正文（用于 hash 兜底）")
    ap.add_argument("--key", default=None, help="显式去重键")
    ap.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)
    ap.add_argument("--reset", action="store_true", help="清空去重状态后退出")
    args = ap.parse_args()

    if args.reset:
        reset()
        print("去重状态已清空")
        sys.exit(0)

    dk = dedup_key(args.key, args.message)
    if is_allowed(dk, args.cooldown):
        mark_sent(dk, args.cooldown)
        print(f"[ALLOW]    key={dk}  -> 已放行并记录")
        sys.exit(0)
    else:
        print(f"[SUPPRESS] key={dk}  -> 冷却期内，抑制")
        sys.exit(1)
