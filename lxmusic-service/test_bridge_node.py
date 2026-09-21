"""bridge.js 的 Node 行为测试包装（js/test_bridge.js 的 pytest 入口）。

本机没装 node 时自动跳过（CI 会显式安装 node）；
js/test_bridge.js 用 node 原生 assert + 本地 http 服务器，零 npm 依赖。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
HERE = Path(__file__).resolve().parent
TEST_JS = HERE / "js" / "test_bridge.js"

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node 不在 PATH（CI 显式安装 node；本地可借容器内 node 手动跑）"
)


def test_bridge_node_suite():
    result = subprocess.run(
        [NODE, str(TEST_JS)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(HERE),
    )
    assert result.returncode == 0, (
        f"bridge.js Node 测试失败:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    # 摘要行形如 "12/12 passed"
    assert "0 failed" not in result.stdout
