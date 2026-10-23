"""每日大模型 token 记账与限额（issue #83）。

状态文件 llm_usage.json（0644 原子写）按日记录 prompt/completion/total
token 与请求数、最近一次失败原因；跨日读取自动清零。proxy 推荐链路在每次
LLM 调用前查额度、响应后记账，WebUI 管理页读取展示。纯 stdlib，记账失败
绝不阻断推荐。

宿主 proxy 以 root（UMask=0077）写、容器 webui 以 uid=1000 只读同一账本
（issue #85）：路径靠 WEBUI_REPO_DIR 对齐到 /repo，权限靠显式 chmod 0644
放宽——内容只有当日日期、token 计数与截断后的失败摘要，不含任何密钥。
"""
from __future__ import annotations

import json
import os
import threading
import time

_LOCK = threading.Lock()
_STATE: dict | None = None
_STATE_DAY = ""

DEFAULT_DAILY_TOKEN_LIMIT = 1_000_000
_MAX_INT = 1_000_000_000


def _state_path() -> str:
    env = (os.environ.get("FNMUSIC_LLM_USAGE_FILE") or "").strip()
    if env:
        return env
    home = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if not home:
        # 容器内 webui 没有 FNMUSIC_HOME：宿主仓库挂载在 /repo，
        # WEBUI_REPO_DIR 是定位宿主 proxy 所写账本的唯一线索（issue #85）。
        home = (os.environ.get("WEBUI_REPO_DIR") or "").strip()
    if not home:
        home = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(home, "llm_usage.json")


def daily_limit() -> int:
    """每日 token 上限（FNMUSIC_LLM_DAILY_TOKEN_LIMIT）：默认 100 万，0=不限制。"""
    try:
        value = int(os.environ.get("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "") or DEFAULT_DAILY_TOKEN_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_TOKEN_LIMIT
    return max(0, min(_MAX_INT, value))


def _today() -> str:
    return time.strftime("%Y%m%d", time.localtime())


def _load() -> dict:
    """读当日账本：跨日/缺失/损坏一律回到空账本。调用方必须持锁。"""
    global _STATE, _STATE_DAY
    today = _today()
    if _STATE is not None and _STATE_DAY == today:
        return _STATE
    state = {"date": today, "prompt": 0, "completion": 0, "total": 0,
             "requests": 0, "lastFailure": None}
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("date") == today:
            for key in ("prompt", "completion", "total", "requests"):
                value = data.get(key)
                state[key] = value if isinstance(value, int) and 0 <= value <= _MAX_INT else 0
            failure = data.get("lastFailure")
            if isinstance(failure, dict) and str(failure.get("reason") or "").strip():
                state["lastFailure"] = {
                    "reason": str(failure["reason"])[:80],
                    "detail": str(failure.get("detail") or "")[:500],
                    "at": int(failure.get("at") or 0),
                }
    except (OSError, ValueError, TypeError):
        pass
    _STATE, _STATE_DAY = state, today
    return state


def _save(state: dict) -> None:
    path = _state_path()
    part = f"{path}.{os.getpid()}.part"
    try:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        with open(part, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(part, path)
        try:
            # 显式 0644 而非 0600：宿主 proxy 在 UMask=0077 下写出的账本必须
            # 保持全局可读，容器 webui（uid=1000）才读得到（issue #85）。
            os.chmod(path, 0o644)
        except OSError:
            pass
    except OSError:
        # 记账失败不阻断推荐：进程内账本仍在，下次成功请求重试落盘
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass


def record(usage: dict | None, failure_reason: str | None = None, failure_detail: str = "") -> None:
    """记一次 LLM 请求：usage 非空累加 token，failure_reason 非空覆盖最近失败。"""
    with _LOCK:
        state = _load()
        if usage:
            state["prompt"] += int(usage.get("prompt") or 0)
            state["completion"] += int(usage.get("completion") or 0)
            state["total"] += int(usage.get("total") or 0)
        state["requests"] += 1
        if failure_reason:
            state["lastFailure"] = {
                "reason": str(failure_reason)[:80],
                "detail": (failure_detail or "")[:500],
                "at": int(time.time()),
            }
        _save(state)


def used_today() -> dict:
    with _LOCK:
        return dict(_load())


def within_limit(limit: int | None = None) -> bool:
    """当日 token 总量未达上限才放行；limit<=0 视为不限制。"""
    if limit is None:
        limit = daily_limit()
    if limit <= 0:
        return True
    with _LOCK:
        return int(_load().get("total") or 0) < limit


def summary() -> dict:
    """当日用量概览（healthz / WebUI）：账本 + 生效上限与放行状态。"""
    limit = daily_limit()
    state = used_today()
    state["dailyLimit"] = limit
    state["withinLimit"] = within_limit(limit)
    return state
