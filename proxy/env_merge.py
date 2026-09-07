"""fnmusic-ext .env 安全合并工具（防覆盖 / 平滑升级）.

install.sh 在写入 .env 前先收集“本次安装期望的配置”，再调用本模块与
已有 .env 做增量合并：

- 已存在的配置项一律保留用户现有值（密钥、自定义路径、ONLINE_SOURCES 等），
  除非该键出现在 explicit（用户本次明确提供了新值）列表中；
- 新版本引入的新配置项 / 缺失配置项自动安全补齐；
- 用户手工添加的自定义键原样保留；
- 合并结果原子写入并保持 0600 权限，合并前由调用方负责备份。

CLI:
    python3 proxy/env_merge.py --existing .env --desired desired.env \
        --output .env --explicit KEY1,KEY2 [--quiet]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# 第三音源 lxmusic（洛雪音乐源）默认配置：合并时自动识别并安全补齐
LX_COMMENT = "洛雪音乐源 lxmusic（第三音源，宿主机端口 8772 -> 容器 8000）"
LX_DEFAULTS: "list[tuple[str, str]]" = [
    ("FNMUSIC_LX_ENABLED", "true"),
    ("FNMUSIC_LX_URL", "http://127.0.0.1:8772"),
]
LX_PREFIX = "FNMUSIC_LX_"


def escape_single_quoted(value: str) -> str:
    """转义 .env 单引号包裹值中的单引号（与 install.sh dotenv_escape 一致）。"""
    return value.replace("'", "'\\''")


def unquote_env_value(raw: str) -> str:
    """去掉 KEY=VALUE 行 VALUE 部分的引号并还原转义的单引号。"""
    v = raw.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        inner = v[1:-1]
        return inner.replace("'\\''", "'")
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        inner = v[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return v


def quote_env_value(value: str) -> str:
    return "'" + escape_single_quoted(value) + "'"


def parse_env_file(path: str | Path) -> "tuple[list[tuple[str, str]], list[str]]":
    """解析 .env 文件。

    返回 (kv_list, other_lines)：kv_list 为按出现顺序的 (key, value) 元组，
    other_lines 为注释/空行等非赋值行（仅记录内容，位置信息不保留）。
    """
    kv: list[tuple[str, str]] = []
    others: list[str] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return kv, others
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            kv.append((m.group(1), unquote_env_value(m.group(2))))
        elif line.strip():
            others.append(line.rstrip())
    return kv, others


def merge_env(
    existing: "list[tuple[str, str]]",
    desired: "list[tuple[str, str]]",
    explicit: "set[str] | None" = None,
) -> "tuple[list[tuple[str, str]], dict[str, list[str]]":
    """增量合并配置。

    规则：
    1. desired 中存在、existing 中不存在的键 -> 安全补齐（added）；
    2. explicit 中的键 -> 采用 desired 新值（updated，用户本次明确提供）；
    3. 其余已有键 -> 保留用户现有值（preserved）；
    4. existing 中多出的自定义键 -> 原样保留（custom_kept）。
    """
    explicit = set(explicit or ())
    existing_map = dict(existing)
    desired_map = dict(desired)
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    summary = {"added": [], "updated": [], "preserved": [], "custom_kept": []}

    for key, _val in existing:
        if key in seen:
            continue
        seen.add(key)
        if key in desired_map:
            if key in explicit:
                result.append((key, desired_map[key]))
                summary["updated"].append(key)
            else:
                result.append((key, existing_map[key]))
                summary["preserved"].append(key)
        else:
            result.append((key, existing_map[key]))
            summary["custom_kept"].append(key)

    for key, val in desired:
        if key in seen:
            continue
        seen.add(key)
        result.append((key, val))
        summary["added"].append(key)

    return result, summary


def ensure_prefix_defaults(
    kv: "list[tuple[str, str]]",
    defaults: "list[tuple[str, str]]" = None,
    prefix: str = LX_PREFIX,
) -> "tuple[list[tuple[str, str]], list[str]]":
    """自动识别并补齐指定前缀（默认 FNMUSIC_LX_*）的缺失配置项。

    已存在的键一律不动（保留用户现有值，包括 false / 自定义 URL），
    仅追加缺失键；返回 (新列表, 追加的键列表)。
    """
    if defaults is None:
        defaults = LX_DEFAULTS
    known = {k for k, _ in kv}
    out = list(kv)
    added: list[str] = []
    for key, val in defaults:
        if key.startswith(prefix) and key not in known:
            out.append((key, val))
            added.append(key)
    return out, added


def render_env(
    kv: "list[tuple[str, str]]",
    header: str = "",
    comments: "dict[str, str] | None" = None,
) -> str:
    lines = []
    if header:
        lines.append(f"# {header}")
    for key, val in kv:
        if comments and key in comments:
            lines.append(f"# {comments[key]}")
        lines.append(f"{key}={quote_env_value(val)}")
    return "\n".join(lines) + "\n"


def write_env_atomic(path: str | Path, content: str, mode: int = 0o600) -> None:
    path = Path(path)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent or "."), prefix=".env.merge.")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(path))
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_installed_version(env_path: str | Path) -> str:
    """读取现有 .env 中记录的已安装版本（FNMUSIC_VERSION）。"""
    kv, _ = parse_env_file(env_path)
    return dict(kv).get("FNMUSIC_VERSION", "")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="fnmusic-ext .env safe merge")
    parser.add_argument("--existing", required=True, help="现有 .env 路径（可不存在）")
    parser.add_argument("--desired", required=True, help="本次安装期望的 .env 内容")
    parser.add_argument("--output", required=True, help="合并结果输出路径")
    parser.add_argument("--explicit", default="", help="用户本次明确提供新值的键，逗号分隔")
    parser.add_argument("--header", default="generated by install.sh — do not commit")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    existing_kv, _others = parse_env_file(args.existing)
    desired_kv, _ = parse_env_file(args.desired)
    explicit = {k.strip() for k in args.explicit.split(",") if k.strip()}
    merged, summary = merge_env(existing_kv, desired_kv, explicit)

    # 第三音源 FNMUSIC_LX_* 自动识别：缺失时安全补齐（带注释）
    merged, lx_added = ensure_prefix_defaults(merged)
    summary["added"].extend(lx_added)

    write_env_atomic(
        args.output,
        render_env(merged, args.header, comments={k: LX_COMMENT for k, _ in LX_DEFAULTS}),
    )
    if not args.quiet:
        for action in ("added", "updated", "preserved", "custom_kept"):
            keys = summary[action]
            if keys:
                print(f"{action}: {','.join(sorted(keys))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
