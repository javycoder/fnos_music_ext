"""Validation and naming helpers for per-user download directories."""
from __future__ import annotations

import hashlib
import json
import os
import uuid


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
    if os.path.islink(users_dir):
        return []
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
    if os.path.islink(users_dir):
        raise ValueError("自动用户目录的父目录不能是符号链接")
    expected = auto_user_dir_name(user_guid)
    target = os.path.join(users_dir, expected)
    linked = os.path.islink(target)
    if not linked and os.path.isdir(target):
        return target
    existing = owned_user_dirs(users_dir, user_guid, expected.rpartition(".")[2])
    if len(existing) == 1:
        return existing[0]
    if linked:
        raise ValueError("自动用户目录不能是符号链接")
    return target


def _remove_quiet(path: str) -> None:
    """尽力删除临时文件；失败不影响主流程。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _ref_owner(stem: str, users_dir: str) -> str:
    """引用内容若指向 users 容器的下一级目录，返回该目录名；否则返回空串。"""
    prefix = users_dir + os.sep
    if not stem.startswith(prefix):
        return ""
    name, separator, _rest = stem[len(prefix):].partition(os.sep)
    return name if separator else ""


def _current_dir_name(owner: str, users: list[dict], users_dir: str) -> str:
    """旧目录名 owner 属于哪个在用用户，返回该用户的当前目录名；判断不了返回空串。

    当前目录必须真实存在：改名没有完成时，不能把引用指到不存在的路径。
    """
    tail = owner.rpartition(".")[2]
    for user in users or []:
        guid = str((user or {}).get("guid") or "")
        named = _USER_DIR_NAMES.get(guid)
        if not named or named == owner:
            continue
        try:
            belongs = owner == user_storage_key(guid) or (
                "." in owner and tail == named.rpartition(".")[2] and _is_code(tail))
        except ValueError:
            continue
        if belongs:
            target = os.path.join(users_dir, named)
            return named if not os.path.islink(target) and os.path.isdir(target) else ""
    return ""


def repair_user_refs(cache_dir: str, global_root: str, users: list[dict]) -> int:
    """把指向用户旧目录的歌曲引用改写到该用户的当前目录，返回改写条数。

    引用文件内容是歌曲词干的绝对路径。目录改名后不改写，扩展会认为这首歌没保存过
    而重复下载。只改能证明归属的引用：内容以「users 容器/某目录名/」开头，且该目录名
    属于一个在用用户。逐个原子写入并读回校验，单条失败只跳过该条，留到下次启动重试。
    """
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0
    try:
        users_dir = os.path.join(_absolute_path(global_root, "下载目录"), "users")
        if os.path.islink(users_dir):
            return 0
        names = os.listdir(cache_dir)
    except (OSError, ValueError):
        return 0
    rewritten = 0
    for name in names:
        if not name.endswith(".ref"):
            continue
        ref = os.path.join(cache_dir, name)
        try:
            with open(ref, encoding="utf-8") as handle:
                stem = handle.read(16384).strip()
        except (OSError, ValueError):
            continue
        owner = _ref_owner(stem, users_dir)
        # 旧目录还在，说明改名没有发生或已回退；同名目标可能包含别的歌曲。
        if not owner or os.path.lexists(os.path.join(users_dir, owner)):
            continue
        current = _current_dir_name(owner, users, users_dir)
        if not current:
            continue
        old_prefix = users_dir + os.sep + owner + os.sep
        new_stem = users_dir + os.sep + current + os.sep + stem[len(old_prefix):]
        temporary = f"{ref}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(new_stem)
            os.replace(temporary, ref)
            with open(ref, encoding="utf-8") as handle:
                if handle.read(16384).strip() != new_stem:
                    raise OSError("read-back mismatch")
            rewritten += 1
        except (OSError, ValueError):
            _remove_quiet(temporary)
    return rewritten


def _confirmed(check) -> bool:
    """调用官方曲库的确认回调；回调抛出异常时按未确认处理，宁可不改名。"""
    try:
        return bool(check())
    except Exception:
        return False


def migrate_user_dirs(global_root: str, cache_dir: str, users: list[dict], official=None) -> dict:
    """把用户的旧目录改名为当前期望名，再修复歌曲引用。幂等，可重复执行。

    只改能确认归属的目录：目录名等于该用户的完整摘要，或以该用户的归属码结尾。
    目标名已被占用、同一用户有多个归属目录、改名失败时一律不动文件，记入 conflicts
    交管理员处理。已删除用户的目录找不到对应用户，保持原样。目录内的文件只随目录
    整体改名，内容不变。

    official 是可选的官方曲库保护对象，提供两个方法：
    can_rename(旧路径) -> bool 为 False 时本次不改名，记入 deferred；
    followed(旧路径, 新路径) -> bool 为 False 时把目录改回原名，记入 conflicts。
    飞牛音乐按文件路径登记歌曲，收藏和歌单再按曲目关联。旧目录的歌有登记时，
    改名必须让官方库跟着更新路径，否则收藏和歌单会对不上。
    """
    result: dict = {"migrated": [], "conflicts": [], "deferred": [], "rewritten_refs": 0}
    try:
        users_dir = os.path.join(_absolute_path(global_root, "下载目录"), "users")
    except ValueError:
        return result
    if os.path.islink(users_dir) or not os.path.isdir(users_dir):
        return result
    for user in users or []:
        try:
            guid = _guid(str((user or {}).get("guid") or ""))
        except ValueError:
            continue
        target_name = _USER_DIR_NAMES.get(guid)
        if not target_name:
            continue
        try:
            # 排除当前名自身，剩下的才是需要处理的旧目录
            owned = [path for path in owned_user_dirs(users_dir, guid, target_name.rpartition(".")[2])
                     if os.path.basename(path) != target_name]
        except ValueError:
            continue
        if not owned:
            continue
        target = os.path.join(users_dir, target_name)
        if len(owned) > 1 or os.path.lexists(target):
            result["conflicts"].extend(owned)
            continue
        if official is not None and not _confirmed(lambda: official.can_rename(owned[0])):
            result["deferred"].append(owned[0])
            continue
        try:
            os.rename(owned[0], target)
        except OSError:
            result["conflicts"].append(owned[0])
            continue
        if official is not None and not _confirmed(lambda: official.followed(owned[0], target)):
            # 官方库没有跟着更新路径：立刻改回原名，歌曲与收藏保持原状，下次启动再试。
            # 极少数情况下改不回去，目录留在新名，由后面的引用修复兜底，并记入冲突。
            old_path = owned[0]
            try:
                os.rename(target, old_path)
            except OSError:
                pass
            result["conflicts"].append(old_path)
            continue
        result["migrated"].append({"guid": guid, "from": owned[0], "to": target})
    # 引用修复独立于改名：上次中途退出时目录已是新名而引用还指旧路径，这里补齐
    result["rewritten_refs"] = repair_user_refs(cache_dir, global_root, users)
    return result


def user_storage_key(user_guid: str) -> str:
    return hashlib.sha256(_guid(user_guid).encode("utf-8")).hexdigest()


def effective_user_root(conf: dict, user_guid: str, global_root: str) -> str:
    mapping = parse_user_map(conf.get("tee_save_dir_by_user"), kind="下载目录映射")
    configured = mapping.get(_guid(user_guid))
    if configured:
        return str(configured)
    return auto_user_root(user_guid, global_root)


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
