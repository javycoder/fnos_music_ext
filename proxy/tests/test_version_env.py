"""版本号读取与 .env 安全合并（防覆盖/平滑升级）单元测试。"""
import os
import stat
from pathlib import Path

import pytest

from proxy import env_merge
from proxy.version import FALLBACK_VERSION, get_version

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------- version ----

def test_version_file_exists_with_semver():
    version_file = REPO_ROOT / "VERSION"
    assert version_file.is_file(), "仓库根目录必须存在 VERSION 文件"
    ver = version_file.read_text(encoding="utf-8").strip()
    assert ver == "1.1.1"
    parts = ver.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_get_version_reads_file(monkeypatch):
    monkeypatch.delenv("FNMUSIC_VERSION", raising=False)
    assert get_version() == "1.1.1"


def test_get_version_env_override(monkeypatch):
    monkeypatch.setenv("FNMUSIC_VERSION", "2.3.4")
    assert get_version() == "2.3.4"


def test_get_version_fallback(monkeypatch):
    monkeypatch.delenv("FNMUSIC_VERSION", raising=False)
    import proxy.version as v

    monkeypatch.setattr(v, "_read_version_file", lambda p: "")
    assert v.get_version() == FALLBACK_VERSION


# ------------------------------------------------------------- env parsing ---

def test_parse_and_roundtrip_quoting(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n"
        "A='hello'\n"
        'B="world"\n'
        "C=bare\n"
        "D='it'\\''s'\n"
        "export E='exported'\n",
        encoding="utf-8",
    )
    kv, others = env_merge.parse_env_file(p)
    assert kv == [
        ("A", "hello"),
        ("B", "world"),
        ("C", "bare"),
        ("D", "it's"),
        ("E", "exported"),
    ]
    assert others == ["# comment"]
    rendered = env_merge.render_env(kv)
    out = tmp_path / "roundtrip.env"
    out.write_text(rendered, encoding="utf-8")
    kv3, _ = env_merge.parse_env_file(out)
    assert kv3 == kv


# ---------------------------------------------------------------- merging ----

EXISTING = [
    ("FNMUSIC_HOME", "/custom/home"),
    ("FNMUSIC_LLM_API_KEY", "sk-user-secret"),
    ("FNMUSIC_LLM_BASE_URL", "https://user.example.com/v1"),
    ("FNMUSIC_LLM_MODEL", "deepseek-chat"),
    ("FNMUSIC_ONLINE_SOURCES", "UserCustomClient"),
    ("FNMUSIC_MY_EXTRA", "keep-me"),
]

DESIRED = [
    ("FNMUSIC_HOME", "/default/home"),
    ("FNMUSIC_CACHE_DIR", "/default/home/cache"),
    ("FNMUSIC_MUSICDL_ENABLED", "true"),
    ("FNMUSIC_NETEASE_ENABLED", "false"),
    ("FNMUSIC_ONLINE_SOURCES", "MiguMusicClient,KuwoMusicClient"),
    ("FNMUSIC_LLM_BASE_URL", ""),
    ("FNMUSIC_LLM_API_KEY", ""),
    ("FNMUSIC_LLM_MODEL", ""),
    ("FNMUSIC_VERSION", "1.1.0"),
]


def test_merge_preserves_user_config_on_upgrade():
    merged, summary = env_merge.merge_env(EXISTING, DESIRED, explicit={"FNMUSIC_VERSION"})
    m = dict(merged)
    # 用户密钥/自定义路径/自定义音源必须原样保留
    assert m["FNMUSIC_LLM_API_KEY"] == "sk-user-secret"
    assert m["FNMUSIC_LLM_BASE_URL"] == "https://user.example.com/v1"
    assert m["FNMUSIC_LLM_MODEL"] == "deepseek-chat"
    assert m["FNMUSIC_HOME"] == "/custom/home"
    assert m["FNMUSIC_ONLINE_SOURCES"] == "UserCustomClient"
    assert m["FNMUSIC_MY_EXTRA"] == "keep-me"
    # 新增配置项被补齐
    assert m["FNMUSIC_CACHE_DIR"] == "/default/home/cache"
    assert m["FNMUSIC_VERSION"] == "1.1.0"
    assert "FNMUSIC_CACHE_DIR" in summary["added"]
    assert "FNMUSIC_MY_EXTRA" in summary["custom_kept"]
    assert "FNMUSIC_LLM_API_KEY" in summary["preserved"]


def test_merge_explicit_override_only_when_user_provides():
    merged, _ = env_merge.merge_env(
        EXISTING, DESIRED, explicit={"FNMUSIC_VERSION", "FNMUSIC_LLM_API_KEY"}
    )
    m = dict(merged)
    assert m["FNMUSIC_LLM_API_KEY"] == ""  # 用户明确提供新值（置空）才覆盖
    assert m["FNMUSIC_LLM_BASE_URL"] == "https://user.example.com/v1"


def test_merge_same_version_reinstall_keeps_everything():
    existing = EXISTING + [("FNMUSIC_VERSION", "1.1.0")]
    merged, summary = env_merge.merge_env(existing, DESIRED, explicit={"FNMUSIC_VERSION"})
    m = dict(merged)
    assert m["FNMUSIC_VERSION"] == "1.1.0"
    assert m["FNMUSIC_LLM_API_KEY"] == "sk-user-secret"
    assert m["FNMUSIC_HOME"] == "/custom/home"


def test_write_env_atomic_permissions_and_content(tmp_path):
    out = tmp_path / ".env"
    env_merge.write_env_atomic(out, env_merge.render_env(DESIRED))
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    kv, _ = env_merge.parse_env_file(out)
    assert dict(kv)["FNMUSIC_VERSION"] == "1.1.0"


def test_read_installed_version(tmp_path):
    p = tmp_path / ".env"
    env_merge.write_env_atomic(p, env_merge.render_env(DESIRED))
    assert env_merge.read_installed_version(p) == "1.1.0"
    assert env_merge.read_installed_version(tmp_path / "missing.env") == ""


# ------------------------------------------------------------ lxmusic merge ---

def test_ensure_prefix_defaults_adds_missing_lx_keys():
    kv, added = env_merge.ensure_prefix_defaults([("FNMUSIC_HOME", "/x")])
    m = dict(kv)
    assert m["FNMUSIC_LX_ENABLED"] == "true"
    assert m["FNMUSIC_LX_URL"] == "http://127.0.0.1:8772"
    assert added == ["FNMUSIC_LX_ENABLED", "FNMUSIC_LX_URL"]


def test_ensure_prefix_defaults_preserves_existing_lx_values():
    existing = [("FNMUSIC_LX_ENABLED", "false"), ("FNMUSIC_LX_URL", "http://nas:8772")]
    kv, added = env_merge.ensure_prefix_defaults(existing)
    assert added == []
    assert dict(kv) == dict(existing)


def test_render_env_includes_lx_comment():
    text = env_merge.render_env(
        env_merge.LX_DEFAULTS,
        comments={k: env_merge.LX_COMMENT for k, _ in env_merge.LX_DEFAULTS},
    )
    assert env_merge.LX_COMMENT in text
    # 直接用 render -> 再 parse 验证注释不干扰解析
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write(text)
        path = f.name
    kv2, _ = env_merge.parse_env_file(Path(path))
    assert dict(kv2)["FNMUSIC_LX_URL"] == "http://127.0.0.1:8772"


def test_cli_merge_auto_adds_lx_config(tmp_path):
    """CLI 合并时自动识别并补齐 FNMUSIC_LX_* 配置，且带注释。"""
    existing = tmp_path / ".env"
    existing.write_text(
        "FNMUSIC_LX_ENABLED='false'\nFNMUSIC_HOME='/custom'\n",
        encoding="utf-8",
    )
    desired = tmp_path / "desired.env"
    env_merge.write_env_atomic(desired, env_merge.render_env(DESIRED))
    rc = env_merge.main(
        [
            "--existing", str(existing),
            "--desired", str(desired),
            "--output", str(existing),
            "--explicit", "FNMUSIC_VERSION",
        ]
    )
    assert rc == 0
    text = existing.read_text(encoding="utf-8")
    kv, _ = env_merge.parse_env_file(existing)
    m = dict(kv)
    # 用户已显式关闭 lx -> 不被覆盖
    assert m["FNMUSIC_LX_ENABLED"] == "false"
    # 缺失的 FNMUSIC_LX_URL 被安全补齐，且带注释
    assert m["FNMUSIC_LX_URL"] == "http://127.0.0.1:8772"
    assert env_merge.LX_COMMENT in text


def test_cli_end_to_end_merge(tmp_path, capsys):
    existing = tmp_path / ".env"
    existing.write_text(
        "FNMUSIC_LLM_API_KEY='sk-old'\n"
        "FNMUSIC_HOME='/custom'\n"
        "FNMUSIC_MY_EXTRA='x'\n",
        encoding="utf-8",
    )
    desired = tmp_path / "desired.env"
    env_merge.write_env_atomic(desired, env_merge.render_env(DESIRED))
    rc = env_merge.main(
        [
            "--existing", str(existing),
            "--desired", str(desired),
            "--output", str(existing),
            "--explicit", "FNMUSIC_VERSION",
        ]
    )
    assert rc == 0
    kv, _ = env_merge.parse_env_file(existing)
    m = dict(kv)
    assert m["FNMUSIC_LLM_API_KEY"] == "sk-old"
    assert m["FNMUSIC_HOME"] == "/custom"
    assert m["FNMUSIC_MY_EXTRA"] == "x"
    assert m["FNMUSIC_CACHE_DIR"] == "/default/home/cache"
    assert m["FNMUSIC_VERSION"] == "1.1.0"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    summary = capsys.readouterr().err
    assert "preserved" in summary and "added" in summary
