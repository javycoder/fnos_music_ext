"""Run independent token-ledger frontend behavior checks."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_llm_usage_js():
    result = subprocess.run([shutil.which("node"), str(Path(__file__).with_suffix(".js"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
