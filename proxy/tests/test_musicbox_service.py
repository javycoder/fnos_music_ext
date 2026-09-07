import io
import os
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

# Ensure musicbox-service directory is in sys.path
MUSICBOX_SERVICE_DIR = str(Path(__file__).resolve().parent.parent.parent / "musicbox-service")
if MUSICBOX_SERVICE_DIR not in sys.path:
    sys.path.insert(0, MUSICBOX_SERVICE_DIR)

import runner
from app import app, UpstreamException


def test_ensure_xdg_dirs_creates_all_directories(tmp_path, monkeypatch):
    cache_dir = tmp_path / "custom_cache"
    config_dir = tmp_path / "custom_config"
    data_dir = tmp_path / "custom_data"
    runtime_dir = tmp_path / "custom_runtime"

    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    runner.ensure_xdg_dirs()

    # All base directories and netease-musicbox subdirectories must exist
    assert cache_dir.is_dir()
    assert (cache_dir / "netease-musicbox").is_dir()
    assert config_dir.is_dir()
    assert (config_dir / "netease-musicbox").is_dir()
    assert data_dir.is_dir()
    assert (data_dir / "netease-musicbox").is_dir()
    assert runtime_dir.is_dir()
    assert (runtime_dir / "netease-musicbox").is_dir()


def test_auth_login_qr_with_upstream_qr_ascii(monkeypatch):
    test_ascii = "█▀▀▀▀▀▀▀█\n█ █▀▀▀█ █\n▀▀▀▀▀▀▀▀▀"

    def mock_run_musicbox(args, timeout=30.0):
        assert args == ["auth", "login", "--no-wait", "--json"]
        stdout = (
            '{"ok": true, "data": {"unikey": "test-key-123", "qr_ascii": "'
            + test_ascii.replace("\n", "\\n")
            + '"}}'
        )
        return 0, stdout, ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        # Test main endpoint /api/v1/auth/login/qr
        resp1 = client.get("/api/v1/auth/login/qr")
        assert resp1.status_code == 200
        assert "text/plain" in resp1.headers["content-type"]
        assert test_ascii in resp1.text
        assert resp1.text.endswith("\n")

        # Test alias endpoint /api/v1/auth/qr
        resp2 = client.get("/api/v1/auth/qr")
        assert resp2.status_code == 200
        assert "text/plain" in resp2.headers["content-type"]
        assert test_ascii in resp2.text
        assert resp2.text.endswith("\n")


def test_auth_login_qr_fallback_to_qrcode_generation(monkeypatch):
    def mock_run_musicbox(args, timeout=30.0):
        assert args == ["auth", "login", "--no-wait", "--json"]
        # No qr_ascii in payload, only unikey
        stdout = '{"ok": true, "data": {"unikey": "test-fallback-key"}}'
        return 0, stdout, ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/login/qr")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        # Generated QR should contain black/white blocks
        assert len(resp.text) > 50
        assert resp.text.endswith("\n")


def test_auth_login_qr_missing_unikey_and_ascii(monkeypatch):
    def mock_run_musicbox(args, timeout=30.0):
        return 0, '{"ok": true, "data": {}}', ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/login/qr")
        assert resp.status_code == 502
        rj = resp.json()
        assert rj["error"] == "upstream_error"
        assert "Missing unikey or qr_ascii" in rj["stderr"]


def test_auth_qr_png_endpoint_and_alias(monkeypatch):
    def mock_run_musicbox(args, timeout=30.0):
        assert args == ["auth", "login", "--no-wait", "--json"]
        stdout = '{"ok": true, "data": {"unikey": "png-test-key"}}'
        return 0, stdout, ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)

    with TestClient(app) as client:
        # Test original endpoint /api/v1/auth/login/qr.png
        resp1 = client.get("/api/v1/auth/login/qr.png")
        assert resp1.status_code == 200
        assert resp1.headers["content-type"] == "image/png"
        assert resp1.content[:8] == b"\x89PNG\r\n\x1a\n"

        # Test alias endpoint /api/v1/auth/qr.png
        resp2 = client.get("/api/v1/auth/qr.png")
        assert resp2.status_code == 200
        assert resp2.headers["content-type"] == "image/png"
        assert resp2.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_run_musicbox_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "")
    code, stdout, stderr = runner.run_musicbox(["health"])
    assert code == 127
    assert "musicbox executable not found" in stderr


def test_run_musicbox_calls_ensure_xdg_dirs(monkeypatch):
    called = []
    monkeypatch.setattr(runner, "ensure_xdg_dirs", lambda: called.append(True))
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: runner.subprocess.CompletedProcess([], 0, "ok", ""))
    runner.run_musicbox(["version"])
    assert len(called) == 1

