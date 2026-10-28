"""Pytest wrapper for standalone node frontend test suite."""
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
JS_TEST = HERE / "test_public_playlists_frontend.js"


def test_public_playlists_frontend_js():
    """在 CI/pytest 运行环境中驱动执行独立的 Node 前端歌单详情测试。"""
    res = subprocess.run(["node", str(JS_TEST)], capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0, f"Node test suite failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    assert "7/7 通过" in res.stdout
