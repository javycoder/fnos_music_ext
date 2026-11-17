"""Node contract tests runner in an independent process; fails if node or npm is missing."""
import os
import re
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "netease-service"


def test_netease_contract_suite():
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute netease-service contract tests"
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to execute netease-service contract tests"

    env = os.environ.copy()
    env.pop("NODE_OPTIONS", None)
    result = subprocess.run(
        [npm, "test", "--prefix", str(SERVICE_DIR)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
    )
    assert result.returncode == 0, (
        f"netease-service Node contract tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    match = re.search(r"\bpass\s+(\d+)", result.stdout)
    assert match and int(match.group(1)) >= 13
