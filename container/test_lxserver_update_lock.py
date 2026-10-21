from pathlib import Path
import re
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/update_lxserver.sh"


def test_update_script_preserves_arm_native_lock():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "LXSERVER_NATIVE_ARM64_SHA256=" in text
    assert text.count("LXSERVER_NATIVE_ARM64_SHA256=${LXSERVER_NATIVE_ARM64_SHA256}") == 1
    assert "LXSERVER_NATIVE_VERSION=${LXSERVER_NATIVE_VERSION}" in text
