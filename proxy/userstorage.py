"""Validation and naming helpers for per-user download directories."""
from __future__ import annotations

import hashlib
import json
import os


def _guid(value: object, label: str = "用户 GUID") -> str:
    guid = str(value or "").strip()
    if not guid or guid == "shared" or len(guid) > 256:
        raise ValueError(f"{label}无效")
    if any(ord(char) < 32 or ord(char) == 127 for char in guid):
        raise ValueError(f"{label}无效")
    return guid


def _absolute_path(value: str, label: str) -> str:
    path = os.path.realpath(os.path.abspath(str(value or "").strip()))
    if not os.path.isabs(path) or path == os.sep:
        raise ValueError(f"{label}必须是绝对目录")
    if ".." in path.split(os.sep) or any(ord(char) < 32 for char in path):
        raise ValueError(f"{label}包含非法字符")
    return path


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


def parse_user_map(raw: object, *, kind: str, patch: bool = False) -> dict:
    """Parse a complete or patch user mapping while preserving explicit None deletion."""
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"{kind}必须是 JSON 对象") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{kind}必须是 JSON 对象")
    result: dict[str, object] = {}
    for key, value in raw.items():
        mapped_key = _guid(key)
        if kind == "下载目录映射":
            if value is None:
                if patch:
                    result[mapped_key] = None
                else:
                    continue
            elif isinstance(value, str) and value.strip():
                result[mapped_key] = _absolute_path(value, f"用户 {mapped_key} 下载目录")
            else:
                raise ValueError(f"用户 {mapped_key} 下载目录不能为空")
        elif kind == "曲库重扫映射":
            if value is None:
                if patch:
                    result[mapped_key] = None
                else:
                    continue
            else:
                result[mapped_key] = str(value if value is not None else "").strip()
        else:
            raise ValueError(f"未知用户配置类型: {kind}")
    return result


def merge_user_map(existing: dict, patch: dict) -> dict[str, str]:
    merged = {str(key): value for key, value in existing.items() if value is not None}
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def user_storage_key(user_guid: str) -> str:
    return hashlib.sha256(_guid(user_guid).encode("utf-8")).hexdigest()


def effective_user_root(conf: dict, user_guid: str, global_root: str) -> str:
    mapping = parse_user_map(conf.get("tee_save_dir_by_user"), kind="下载目录映射")
    configured = mapping.get(_guid(user_guid))
    if configured:
        return str(configured)
    root = _absolute_path(global_root, "下载目录")
    return os.path.join(root, "users", user_storage_key(user_guid))


def user_ref_name(user_guid: str, safe_guid: str) -> str:
    return f"u_{user_storage_key(user_guid)}__{safe_guid}.ref"


def validate_effective_roots(conf: dict, roots: dict[str, str], global_root: str = "") -> None:
    cache_dir = str(conf.get("cache_dir") or "").strip()
    normalized: dict[str, str] = {}
    for user_guid, raw_root in roots.items():
        root = _absolute_path(str(raw_root), f"用户 {_guid(user_guid)} 有效下载目录")
        for other_guid, other_root in normalized.items():
            if root == other_root or _within(root, other_root) or _within(other_root, root):
                raise ValueError("不同用户的下载目录不能相同或互为父子")
        if cache_dir and os.path.isabs(cache_dir):
            normalized_cache = _absolute_path(cache_dir, "歌曲缓存目录")
            if root == normalized_cache or _within(root, normalized_cache) or _within(normalized_cache, root):
                raise ValueError("用户下载目录不能与歌曲缓存目录相同或互为父子")
        normalized[user_guid] = root
    if global_root:
        normalized_global = _absolute_path(global_root, "下载目录")
        container = os.path.join(normalized_global, "users")
        for root in normalized.values():
            if root == normalized_global or _within(normalized_global, root):
                raise ValueError("用户下载目录不能包含或等于公共下载根目录")
            if root == container or _within(root, container) or _within(container, root):
                raise ValueError("用户下载目录不能与自动用户目录相同或互为父子")
