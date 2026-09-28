"""管理密码：哈希工具，以及 install.sh 在启用 WebUI 时的密码闸门。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"


def _seal_script(base: Path) -> str:
    text = INSTALL.read_text(encoding="utf-8")
    start = text.index("webui_env_value() {")
    end = text.index("\nusage()")
    block = text[start:end]
    return f"""set -euo pipefail
BASE_DIR={base}
WEBUI_CHOICE=yes
NON_INTERACTIVE=1
WEBUI_PASSWORD_HASH=""
WEBUI_SESSION_SECRET=""
WEBUI_PASSWORD_EXPLICIT=0
log_info() {{ printf 'INFO %s\\n' "$*"; }}
log_err() {{ printf 'ERR %s\\n' "$*" >&2; }}
{block}
seal_webui_password
printf 'HASH=%s\\n' "$WEBUI_PASSWORD_HASH"
printf 'SECRET=%s\\n' "$WEBUI_SESSION_SECRET"
printf 'EXPLICIT=%s\\n' "$WEBUI_PASSWORD_EXPLICIT"
"""


def test_auth_cli_prints_hash_not_password():
    env = os.environ.copy()
    env["FNMUSIC_WEBUI_PASSWORD"] = "correct-horse"
    proc = subprocess.run(
        ["python3", str(REPO / "webui-service" / "auth.py")],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("scrypt$")
    assert len(lines[1]) == 64
    assert "correct-horse" not in proc.stdout
    assert "correct-horse" not in proc.stderr


def test_auth_cli_rejects_short_password():
    env = os.environ.copy()
    env["FNMUSIC_WEBUI_PASSWORD"] = "short"
    proc = subprocess.run(
        ["python3", str(REPO / "webui-service" / "auth.py")],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 2
    assert "至少" in proc.stderr
    assert "short" not in proc.stderr


def test_seal_requires_password_when_webui_enabled(tmp_path):
    (tmp_path / "webui-service").mkdir()
    (tmp_path / "webui-service" / "auth.py").symlink_to(REPO / "webui-service" / "auth.py")
    proc = subprocess.run(
        ["bash", "-c", _seal_script(tmp_path)],
        capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", ""), "FNMUSIC_WEBUI_PASSWORD": ""},
    )
    assert proc.returncode == 1
    assert "必须设置管理密码" in proc.stderr


def test_seal_writes_hash_not_plaintext(tmp_path):
    (tmp_path / "webui-service").mkdir()
    (tmp_path / "webui-service" / "auth.py").symlink_to(REPO / "webui-service" / "auth.py")
    env = {"PATH": os.environ.get("PATH", ""), "FNMUSIC_WEBUI_PASSWORD": "correct-horse"}
    proc = subprocess.run(
        ["bash", "-c", _seal_script(tmp_path)],
        capture_output=True, text=True, check=False, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "correct-horse" not in proc.stdout
    assert "correct-horse" not in proc.stderr
    assert "HASH=scrypt$" in proc.stdout
    assert "EXPLICIT=1" in proc.stdout


def test_seal_keeps_existing_hash(tmp_path):
    (tmp_path / "webui-service").mkdir()
    (tmp_path / "webui-service" / "auth.py").symlink_to(REPO / "webui-service" / "auth.py")
    (tmp_path / ".env").write_text(
        "FNMUSIC_WEBUI_PASSWORD_HASH='scrypt$abc$def'\n"
        "FNMUSIC_WEBUI_SESSION_SECRET='already-set'\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["bash", "-c", _seal_script(tmp_path)],
        capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert proc.returncode == 0, proc.stderr
    assert "HASH=\n" in proc.stdout or proc.stdout.endswith("HASH=\n") or "\nHASH=\n" in proc.stdout
    assert "EXPLICIT=0" in proc.stdout
    assert "沿用已保存的管理密码" in proc.stdout
