"""WebUI config merge and credential redaction for per-user storage."""
from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent


def _load_webui():
    cached = sys.modules.get("webui_service_app")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("webui_service_app", HERE / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webui_service_app"] = mod
    spec.loader.exec_module(mod)
    return mod


webui = _load_webui()


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "FNMUSIC_NETEASE_ENABLED=true\n"
        "FNMUSIC_MUSICDL_ENABLED=false\n"
        "FNMUSIC_LX_ENABLED=false\n"
        "FNMUSIC_LIBRARY_SCAN_BY_USER={\"user-b\":\"\"}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "ENV_PATH", env)
    return env


@contextmanager
def authed_client():
    with TestClient(webui.app) as client:
        client.headers.update({"X-Trim-Isadmin": "true"})
        yield client


def test_user_directory_patch_keeps_other_user_and_scan_disable(env_file):
    webui.write_env({"FNMUSIC_TEE_SAVE_DIR_BY_USER": json.dumps({"user-a": "/vol1/music/a"})})
    webui.write_env({"FNMUSIC_TEE_SAVE_DIR_BY_USER": json.dumps({"user-b": "/vol1/music/b"})})
    stored = json.loads(webui.read_env()["FNMUSIC_TEE_SAVE_DIR_BY_USER"])
    assert set(stored) == {"user-a", "user-b"}
    webui.write_env({"FNMUSIC_TEE_SAVE_DIR_BY_USER": json.dumps({"user-a": None})})
    stored = json.loads(webui.read_env()["FNMUSIC_TEE_SAVE_DIR_BY_USER"])
    assert set(stored) == {"user-b"}
    assert json.loads(webui.read_env()["FNMUSIC_LIBRARY_SCAN_BY_USER"]) == {"user-b": ""}
    with pytest.raises(ValueError):
        webui.validate_updates({
            "FNMUSIC_TEE_SAVE_DIR_BY_USER": {"user-a": "/vol1/music/a", "user-c": "/vol1/music/a/child"},
        })


def test_scan_admin_password_is_not_returned(env_file):
    webui.write_env({
        "FNMUSIC_SCAN_ADMIN_USERNAME": "admin",
        "FNMUSIC_SCAN_ADMIN_PASSWORD": "secret-value",
    })
    with authed_client() as client:
        payload = client.get("/api/config").json()
    assert payload["values"]["FNMUSIC_SCAN_ADMIN_PASSWORD"] == ""
    assert payload["scan_admin_password_configured"] is True
    assert "secret-value" not in json.dumps(payload)
    assert webui.read_env()["FNMUSIC_SCAN_ADMIN_PASSWORD"] == "secret-value"


def test_scan_admin_credentials_must_be_paired(env_file):
    with pytest.raises(ValueError, match="同时"):
        webui.validate_updates({"FNMUSIC_SCAN_ADMIN_USERNAME": "admin"})


def test_maintenance_routes_reject_bad_bodies_without_claiming_success(env_file):
    with authed_client() as client:
        rejected = client.post("/api/maintenance/scan-admin-test", json={"password": "secret-value"})
        missing = client.post("/api/maintenance/scan-user", json={})
    assert rejected.status_code == 400
    assert missing.status_code == 400
    assert "secret-value" not in rejected.text
