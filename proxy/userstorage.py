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
    raw_path = str(value or "").strip()
    if not os.path.isabs(raw_path):
        raise ValueError(f"{label}必须是绝对目录")
    if ".." in raw_path.split(os.sep) or any(ord(char) < 32 or ord(char) == 127 for char in raw_path):
        raise ValueError(f"{label}包含非法字符")
    path = os.path.realpath(raw_path)
    if path == os.sep:
        raise ValueError(f"{label}必须是绝对目录")
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


# 自动用户目录名 =「用户名.归属码」。用户名只给人看，程序靠点号后的归属码识别归属：
# 同名用户不会撞目录，用户改名后也能找回原目录（issue #154）。
_USER_DIR_NAMES: dict[str, str] = {}  # 启动时载入并冻结：用户 GUID -> 目录名
_CODE_LENGTHS = (8, 12, 64)           # 归属码位数，在用用户之间撞码时逐级加长
_HEX_DIGITS = frozenset("0123456789abcdef")


def user_dir_code(user_guid: str, length: int = 8) -> str:
    """归属码：用户完整 GUID 摘要的前 length 位十六进制。"""
    return user_storage_key(user_guid)[:length]


def _is_code(text: str) -> bool:
    """text 是否为合法长度的归属码：位数在允许范围内，且全是小写十六进制字符。"""
    return len(text) in _CODE_LENGTHS and all(char in _HEX_DIGITS for char in text)


def load_user_dir_names(users: list[dict], sanitize) -> dict[str, str]:
    """由官方库用户表生成「用户 GUID -> 自动目录名」，GUID 非法的条目跳过。

    sanitize 与 organize.safe_part 同签名，把用户名清洗成可做目录名的展示段。
    两个在用用户的归属码相同时，撞码双方一起加长，保证目录名绝不相同。
    """
    pending = []
    for user in users or []:
        try:
            pending.append((_guid(str((user or {}).get("guid") or "")),
                            str((user or {}).get("name") or "")))
        except ValueError:
            continue
    names: dict[str, str] = {}
    for length in _CODE_LENGTHS:
        groups: dict[str, list] = {}
        for guid, name in pending:
            groups.setdefault(user_dir_code(guid, length), []).append((guid, name))
        pending = []
        for code, group in groups.items():
            # 独占一个归属码的用户落表；已是完整摘要时同码即同 GUID，也直接落表
            if len(group) == 1 or length == _CODE_LENGTHS[-1]:
                for guid, name in group:
                    names[guid] = f"{sanitize(name, 'user')}.{code}"
            else:
                pending.extend(group)
    return names


def set_user_dir_names(names: dict[str, str]) -> None:
    """载入并冻结目录名表，运行期间不再变化。"""
    global _USER_DIR_NAMES
    _USER_DIR_NAMES = dict(names or {})


def auto_user_dir_name(user_guid: str) -> str:
    """自动目录的期望名；表里没有该用户时回退「user.归属码」，仍可按码找回。"""
    guid = _guid(user_guid)
    return _USER_DIR_NAMES.get(guid) or f"user.{user_dir_code(guid)}"


def is_auto_user_dir_name(name: str) -> bool:
    """是否为本扩展生成的自动用户目录名：旧版完整摘要，或「用户名.归属码」。"""
    if len(name) == 64 and all(char in _HEX_DIGITS for char in name):
        return True
    head, _, tail = name.rpartition(".")
    return bool(head) and _is_code(tail)


def owned_user_dirs(users_dir: str, user_guid: str, code: str) -> list[str]:
    """users 容器下归属该用户的现存目录；符号链接不算，避免把链接当成目录。"""
    digest = user_storage_key(user_guid)
    try:
        names = os.listdir(users_dir)
    except OSError:
        return []
    found = []
    for name in names:
        head, _, tail = name.rpartition(".")
        owned = name == digest or (bool(head) and tail == code and _is_code(tail))
        path = os.path.join(users_dir, name)
        if owned and os.path.isdir(path) and not os.path.islink(path):
            found.append(path)
    return sorted(found)


def auto_user_root(user_guid: str, global_root: str) -> str:
    """该用户当前应使用的自动目录，不创建目录。

    期望名目录存在就用它；不存在时按归属码沿用已有目录（启动时官方库读不到，
    或用户改名后），已保存的歌曲不会失联；都没有才返回期望名，交调用方创建。
    """
    users_dir = os.path.join(_absolute_path(global_root, "下载目录"), "users")
    expected = auto_user_dir_name(user_guid)
    if os.path.isdir(os.path.join(users_dir, expected)):
        return os.path.join(users_dir, expected)
    existing = owned_user_dirs(users_dir, user_guid, expected.rpartition(".")[2])
    return existing[0] if len(existing) == 1 else os.path.join(users_dir, expected)


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
