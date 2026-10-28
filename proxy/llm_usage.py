"""每日大模型 token 记账与限额（issue #83, v2 ledger）。

状态文件 llm_usage.json（0644 原子写）按日永久保留 prompt/completion/total
token 与请求数、未返回 usage 的请求数以及最近失败原因。
严格基于中国标准时间 UTC+8 切日，并返回 timezone 标注。
跨日不丢失历史记录，旧 flat 记录自动平滑迁移且标记 historyIncomplete 与 statsStartedAt。

支持 proxy 单写与 WebUI fresh disk 只读（跨进程实时可见，绝不全天缓存），
原子落盘临时文件 chmod 0644 后 replace；损坏/缺失/权限拒绝/落盘失败等状态明确返回
非假 0。损坏原文件绝不悄然覆盖。
失败原因脱敏，绝不输出 API key。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import tempfile
import threading
import time

_LOCK = threading.Lock()

DEFAULT_DAILY_TOKEN_LIMIT = 1_000_000
_MAX_INT = 1_000_000_000
_TZ_SHANGHAI = timezone(timedelta(hours=8))
TIMEZONE_LABEL = "Asia/Shanghai"

# 内存账本与元信息缓存
_MEM_LEDGER: dict | None = None
_LAST_PATH: str | None = None
_STATUS: str = "ok"
_STATUS_MSG: str = "正常"

# 兼容 conftest.py 与旧测试的重置 hook
_STATE: dict | None = None
_STATE_DAY: str = ""

# API Key 与敏感凭证脱敏正则
_BEARER_PATTERN = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{6,}", re.IGNORECASE)
_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE)
_KEY_PARAM_PATTERN = re.compile(r"((?:api[_-]?key|key|token|auth)=)[A-Za-z0-9_\-\.]{6,}", re.IGNORECASE)
_JSON_KEY_PATTERN = re.compile(r'("(?:api[_-]?key|token|secret|key)"\s*:\s*")[^"]+(")', re.IGNORECASE)


def _sanitize_failure_text(text: str) -> str:
    """脱敏失败原因中的 API key 与敏感授权信息，并截断。"""
    if not text:
        return ""
    s = str(text)
    env_key = (os.environ.get("FNMUSIC_LLM_API_KEY") or "").strip()
    if len(env_key) >= 4:
        s = s.replace(env_key, "***")
    s = _BEARER_PATTERN.sub(r"\1***", s)
    s = _SK_PATTERN.sub(r"sk-***", s)
    s = _KEY_PARAM_PATTERN.sub(r"\1***", s)
    s = _JSON_KEY_PATTERN.sub(r"\1***\2", s)
    return s


def _safe_int(val: object, default: int = 0) -> int:
    try:
        if val is None:
            return default
        v = int(val)
        return max(0, min(_MAX_INT, v))
    except (TypeError, ValueError):
        return default


def _safe_failure(val: object) -> dict | None:
    if not isinstance(val, dict):
        return None
    reason = str(val.get("reason") or "").strip()
    if not reason:
        return None
    detail = _sanitize_failure_text(str(val.get("detail") or ""))
    at = _safe_int(val.get("at"), int(time.time()))
    return {
        "reason": reason[:80],
        "detail": detail[:500],
        "at": at,
    }


def _state_path() -> str:
    env = (os.environ.get("FNMUSIC_LLM_USAGE_FILE") or "").strip()
    if env:
        return os.path.normpath(env)
    home = (os.environ.get("FNMUSIC_HOME") or "").strip()
    if not home:
        home = (os.environ.get("WEBUI_REPO_DIR") or "").strip()
    if not home:
        home = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.normpath(os.path.join(home, "llm_usage.json"))


def daily_limit() -> int:
    """每日 token 上限（FNMUSIC_LLM_DAILY_TOKEN_LIMIT）：默认 100 万，0=不限制。"""
    try:
        value = int(os.environ.get("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "") or DEFAULT_DAILY_TOKEN_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_TOKEN_LIMIT
    return max(0, min(_MAX_INT, value))


def _today() -> str:
    """严格按照 UTC+8（中国标准时间）切日。"""
    return datetime.now(_TZ_SHANGHAI).strftime("%Y%m%d")


def _empty_day() -> dict:
    return {
        "prompt": 0,
        "completion": 0,
        "total": 0,
        "requests": 0,
        "unknownUsageRequests": 0,
        "lastFailure": None,
    }


def _new_ledger(today: str) -> dict:
    return {
        "version": 2,
        "timezone": TIMEZONE_LABEL,
        "historyIncomplete": False,
        "statsStartedAt": today,
        "days": {},
    }


def _migrate_old_flat(data: dict, today: str) -> dict:
    """平滑迁移旧版 flat 记录：保留旧日期数据，标记 historyIncomplete 与 statsStartedAt。"""
    old_date = str(data.get("date") or today)
    if not (len(old_date) == 8 and old_date.isdigit()):
        old_date = today

    day_record = {
        "prompt": _safe_int(data.get("prompt")),
        "completion": _safe_int(data.get("completion")),
        "total": _safe_int(data.get("total")),
        "requests": _safe_int(data.get("requests")),
        "unknownUsageRequests": _safe_int(data.get("unknownUsageRequests")),
        "lastFailure": _safe_failure(data.get("lastFailure")),
    }
    days = {old_date: day_record}

    return {
        "version": 2,
        "timezone": TIMEZONE_LABEL,
        "historyIncomplete": True,
        "statsStartedAt": old_date,
        "days": days,
    }


def _parse_ledger_content(raw_bytes: bytes, today: str) -> tuple[dict | None, bool]:
    """解析文件内容。返回 (ledger_dict, is_old_flat)。若内容损坏返回 (None, False)。"""
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None, False

    if not isinstance(data, dict):
        return None, False

    # 检查是否为 v2 格式
    if data.get("version") == 2 and isinstance(data.get("days"), dict):
        ledger = {
            "version": 2,
            "timezone": str(data.get("timezone") or TIMEZONE_LABEL),
            "historyIncomplete": bool(data.get("historyIncomplete")),
            "statsStartedAt": str(data.get("statsStartedAt") or today),
            "days": {},
        }
        for d_str, val in data["days"].items():
            if not isinstance(val, dict):
                continue
            ledger["days"][str(d_str)] = {
                "prompt": _safe_int(val.get("prompt")),
                "completion": _safe_int(val.get("completion")),
                "total": _safe_int(val.get("total")),
                "requests": _safe_int(val.get("requests")),
                "unknownUsageRequests": _safe_int(val.get("unknownUsageRequests")),
                "lastFailure": _safe_failure(val.get("lastFailure")),
            }
        return ledger, False

    # 检查是否为旧版 flat 格式
    if "date" in data or "total" in data:
        return _migrate_old_flat(data, today), True

    return None, False


def _atomic_save(path: str, ledger: dict) -> None:
    """原子保存 v2 账本：写入临时文件 -> chmod 0644 -> replace。
    记账失败绝不抛出任何异常阻断推荐。
    """
    global _STATUS, _STATUS_MSG
    parent = os.path.dirname(path) or "."
    tmp_file = None
    tmp_path: str | None = None
    try:
        os.makedirs(parent, exist_ok=True)
        tmp_file = tempfile.NamedTemporaryFile("w", dir=parent, delete=False, encoding="utf-8")
        tmp_path = tmp_file.name

        json.dump(ledger, tmp_file, ensure_ascii=False, indent=2)
        tmp_file.flush()
        try:
            os.fsync(tmp_file.fileno())
        except OSError:
            pass
        tmp_file.close()
        tmp_file = None

        try:
            # 显式 0644：保持全局可读供容器 webui（uid=1000）只读访问
            os.chmod(tmp_path, 0o644)
        except OSError as e:
            if os.name != "nt":
                _STATUS = "save_failed"
                _STATUS_MSG = f"账本 chmod 0644 失败: {e}"
                try:
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                return

        os.replace(tmp_path, path)
        tmp_path = None
        _STATUS = "ok"
        _STATUS_MSG = "正常"
    except PermissionError:
        _STATUS = "permission_denied"
        _STATUS_MSG = "账本文件无写入权限"
    except OSError as err:
        _STATUS = "save_failed"
        _STATUS_MSG = f"账本保存失败: {err}"
    except Exception as err:
        _STATUS = "save_failed"
        _STATUS_MSG = f"账本写入异常: {err}"
    finally:
        if tmp_file is not None:
            try:
                tmp_file.close()
            except OSError:
                pass
        if tmp_path is not None:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass


def _load_or_sync(*, read_only: bool = False) -> tuple[dict, str, str]:
    """读取或同步账本。

    读者 (read_only=True)：
      fresh disk 检查，磁盘更新立刻可见，绝不全天缓存；旧 flat 仅在内存迁移供展示，绝不写盘；
      若磁盘损坏，返回 status="corrupt"，绝不覆盖。
    写者 (read_only=False)：
      若磁盘损坏，拒绝覆盖原文件并置 status="corrupt"；
      若为旧 flat，落盘迁移为 v2；
      落盘故障保留内存账本，不被旧 disk 覆盖，下次写请求重试保存；
      正常按 UTC+8 更新并原子落盘。
    """
    global _STATE, _STATE_DAY, _MEM_LEDGER, _LAST_PATH, _STATUS, _STATUS_MSG

    path = _state_path()
    today = _today()

    # 路径变化或外部测试显式清空 _STATE 时重置内存状态，防止跨路径串账
    if _STATE is None or path != _LAST_PATH:
        _MEM_LEDGER = None
        _LAST_PATH = path
        _STATUS = "ok"
        _STATUS_MSG = "正常"

    # 1. 读者 (read_only=True)：每次 fresh 读磁盘，绝不写盘、绝不迁移磁盘文件
    if read_only:
        try:
            with open(path, "rb") as f:
                content = f.read()
            ledger, _ = _parse_ledger_content(content, today)
            if ledger is None:
                return _new_ledger(today), "corrupt", "账本文件损坏，已保护现场未覆盖"
            return ledger, "ok", "正常"
        except FileNotFoundError:
            return _new_ledger(today), "missing", "账本文件尚不存在"
        except PermissionError:
            return _new_ledger(today), "permission_denied", "无权限读取账本文件"
        except OSError as err:
            return _new_ledger(today), "read_failed", f"读取账本失败: {err}"

    # 2. 写者 (read_only=False)：
    # 若上一轮发生 save_failed / permission_denied，内存中有未落盘的更新，
    # 必须保留内存账本，绝不能被旧的磁盘文件冲掉！
    if _MEM_LEDGER is not None and _STATUS in ("save_failed", "permission_denied"):
        _STATE = dict(_MEM_LEDGER.get("days", {}).get(today) or _empty_day())
        _STATE["date"] = today
        _STATE_DAY = today
        return _MEM_LEDGER, _STATUS, _STATUS_MSG

    # 正常加载磁盘
    try:
        with open(path, "rb") as f:
            content = f.read()
        ledger, is_old_flat = _parse_ledger_content(content, today)
        if ledger is None:
            _STATUS = "corrupt"
            _STATUS_MSG = "账本文件损坏，已保护现场未覆盖"
            if _MEM_LEDGER is None:
                _MEM_LEDGER = _new_ledger(today)
            return _MEM_LEDGER, _STATUS, _STATUS_MSG

        _MEM_LEDGER = ledger
        _STATUS = "ok"
        _STATUS_MSG = "正常"

        # 旧 flat 仅在内存转换；由真正的 record 调用统一原子持久化。
        # summary / within_limit / used_today 均不能因读取而写盘。
    except FileNotFoundError:
        if _MEM_LEDGER is None:
            _MEM_LEDGER = _new_ledger(today)
        _STATUS = "missing"
        _STATUS_MSG = "账本文件尚不存在"
    except PermissionError:
        _STATUS = "permission_denied"
        _STATUS_MSG = "无权限读取账本文件"
        if _MEM_LEDGER is None:
            _MEM_LEDGER = _new_ledger(today)
    except OSError as err:
        _STATUS = "save_failed"
        _STATUS_MSG = f"读取账本异常: {err}"
        if _MEM_LEDGER is None:
            _MEM_LEDGER = _new_ledger(today)

    _STATE = dict(_MEM_LEDGER.get("days", {}).get(today) or _empty_day())
    _STATE["date"] = today
    _STATE_DAY = today

    return _MEM_LEDGER, _STATUS, _STATUS_MSG


def record(
    usage: dict | None = None,
    failure_reason: str | None = None,
    failure_detail: str = "",
    *,
    actual_request: bool = True,
    is_unknown_usage: bool = False,
) -> None:
    """记一次 LLM 请求或失败。

    actual_request=True: 发生实际 HTTP 尝试，requests 计数 +1；
    actual_request=False: 例如 quota 拦截在请求前被拒绝，仅记 failure，不加 requests；
    usage 非空: 累加 prompt/completion/total；
    is_unknown_usage=True: 未能获取 usage 统计，累加 unknownUsageRequests，不估算假 token；
    failure_reason 非空: 覆盖记录最近失败（经安全脱敏，截断 API key 等凭据）。
    """
    global _STATE, _STATE_DAY
    # 超时/HTTP 错误也发生了尝试，但无法取得供应商 usage，不能当成已知零消耗。
    is_unknown_usage = bool(actual_request and (is_unknown_usage or usage is None))
    with _LOCK:
        today = _today()
        path = _state_path()
        ledger, status, _ = _load_or_sync(read_only=False)

        # 保护损坏文件：若磁盘已损坏，绝不悄然覆盖原损坏文件
        if status == "corrupt":
            day = ledger["days"].setdefault(today, _empty_day())
            if actual_request:
                day["requests"] += 1
            if usage:
                day["prompt"] += _safe_int(usage.get("prompt"))
                day["completion"] += _safe_int(usage.get("completion"))
                day["total"] += _safe_int(usage.get("total"))
            if is_unknown_usage:
                day["unknownUsageRequests"] += 1
            if failure_reason:
                day["lastFailure"] = _safe_failure({
                    "reason": failure_reason,
                    "detail": failure_detail,
                    "at": int(time.time()),
                })
            _STATE = dict(day)
            _STATE["date"] = today
            _STATE_DAY = today
            return

        day = ledger["days"].setdefault(today, _empty_day())
        if actual_request:
            day["requests"] += 1
        if usage:
            day["prompt"] += _safe_int(usage.get("prompt"))
            day["completion"] += _safe_int(usage.get("completion"))
            day["total"] += _safe_int(usage.get("total"))
        if is_unknown_usage:
            day["unknownUsageRequests"] += 1
        if failure_reason:
            day["lastFailure"] = _safe_failure({
                "reason": failure_reason,
                "detail": failure_detail,
                "at": int(time.time()),
            })

        _atomic_save(path, ledger)
        _STATE = dict(day)
        _STATE["date"] = today
        _STATE_DAY = today


def used_today() -> dict:
    """获取当日账本（兼容旧接口）。writer 视角，优先体现 pending 内存。"""
    with _LOCK:
        today = _today()
        ledger, _, _ = _load_or_sync(read_only=False)
        day = ledger.get("days", {}).get(today) or _empty_day()
        res = dict(day)
        res["date"] = today
        return res


def within_limit(limit: int | None = None) -> bool:
    """当日 token 总量未达上限才放行；limit<=0 视为不限制。
    writer 视角，优先体现 pending 内存，确保丢盘故障时不超配额。
    """
    if limit is None:
        limit = daily_limit()
    if limit <= 0:
        return True
    with _LOCK:
        today = _today()
        ledger, _, _ = _load_or_sync(read_only=False)
        day = ledger.get("days", {}).get(today) or _empty_day()
        return _safe_int(day.get("total")) < limit


def summary(
    limit: int | None = None,
    history_limit: int = 0,
    history_offset: int = 0,
    *,
    read_only: bool = False,
) -> dict:
    """获取用量概览（healthz / WebUI 管理页）。

    默认 read_only=False 供 proxy 暴露真实 writer 状态（包括 save_failed/permission_denied 等）；
    WebUI 查询时显式传入 read_only=True 做纯 fresh disk 读，绝不写盘。
    """
    effective_limit = daily_limit() if limit is None else limit
    today = _today()

    with _LOCK:
        ledger, status, status_msg = _load_or_sync(read_only=read_only)
        days = ledger.get("days") or {}
        today_data = days.get(today) or _empty_day()

        # 计算全期累计统计 (allTime)
        all_prompt = sum(_safe_int(d.get("prompt")) for d in days.values())
        all_completion = sum(_safe_int(d.get("completion")) for d in days.values())
        all_total = sum(_safe_int(d.get("total")) for d in days.values())
        all_requests = sum(_safe_int(d.get("requests")) for d in days.values())
        all_unknown = sum(_safe_int(d.get("unknownUsageRequests")) for d in days.values())

        # 历史记录明细（按日期降序）
        h_limit = max(0, min(100, _safe_int(history_limit, 0)))
        h_offset = max(0, _safe_int(history_offset, 0))

        history_items: list[dict] = []
        if h_limit > 0:
            sorted_dates = sorted(days.keys(), reverse=True)
            paged_dates = sorted_dates[h_offset : h_offset + h_limit]
            for d_str in paged_dates:
                d_val = days[d_str]
                history_items.append({
                    "date": d_str,
                    "prompt": _safe_int(d_val.get("prompt")),
                    "completion": _safe_int(d_val.get("completion")),
                    "total": _safe_int(d_val.get("total")),
                    "requests": _safe_int(d_val.get("requests")),
                    "unknownUsageRequests": _safe_int(d_val.get("unknownUsageRequests")),
                    "lastFailure": d_val.get("lastFailure"),
                })

        total_tokens = _safe_int(today_data.get("total"))
        within = True if effective_limit <= 0 else (total_tokens < effective_limit)

        return {
            # 兼容旧版顶级字段
            "date": today,
            "prompt": _safe_int(today_data.get("prompt")),
            "completion": _safe_int(today_data.get("completion")),
            "total": total_tokens,
            "requests": _safe_int(today_data.get("requests")),
            "unknownUsageRequests": _safe_int(today_data.get("unknownUsageRequests")),
            "lastFailure": today_data.get("lastFailure"),
            "dailyLimit": effective_limit,
            "withinLimit": within,

            # v2 扩展字段
            "timezone": TIMEZONE_LABEL,
            "status": status,
            "statusMessage": status_msg,
            "statsStartedAt": str(ledger.get("statsStartedAt") or today),
            "historyIncomplete": bool(ledger.get("historyIncomplete", False)),
            "totalDays": len(days),
            "allTime": {
                "prompt": all_prompt,
                "completion": all_completion,
                "total": all_total,
                "requests": all_requests,
                "unknownUsageRequests": all_unknown,
            },
            "history": history_items,
        }
