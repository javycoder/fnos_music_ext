"""static/app.js 的 Node 行为测试包装（test_app_js.js 的 pytest 入口）。

本机没装 node 时自动跳过（CI 会显式安装 node）；
test_app_js.js 用 node 原生 assert + 最小 DOM/fetch 桩，零 npm 依赖。

覆盖重点：musicbox auth 接口返回 {ok, data:{...}} 信封结构，
app.js 的 pollQr/checkQrStatus 必须从 data.code 取扫码状态码（回归防护）。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
HERE = Path(__file__).resolve().parent
TEST_JS = HERE / "test_app_js.js"

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node 不在 PATH（CI 显式安装 node；本地可借容器内 node 手动跑）"
)


def test_app_js_suite():
    result = subprocess.run(
        [NODE, str(TEST_JS)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(HERE),
    )
    assert result.returncode == 0, (
        f"app.js Node 测试失败:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    # 摘要行形如 "7/7 passed, 0 failed"
    assert ", 0 failed" in result.stdout
