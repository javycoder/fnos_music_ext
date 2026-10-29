"""LLM usage API reads saved limits and supports bounded history pagination."""
import importlib.util
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def load_webui():
    name = "webui_usage_api_test"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("app.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_usage_saved_limit_and_history(tmp_path, monkeypatch):
    webui = load_webui()
    ledger = tmp_path / "usage.json"
    ledger.write_text(json.dumps({"version": 2, "timezone": "Asia/Shanghai",
        "historyIncomplete": False, "statsStartedAt": "20261027", "days": {
            "20261027": {"prompt": 10, "completion": 5, "total": 15, "requests": 1},
            "20261028": {"prompt": 20, "completion": 6, "total": 26, "requests": 2},
        }}), encoding="utf-8")
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(ledger))
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "1000000")
    monkeypatch.setattr(webui.llm_usage, "_today", lambda: "20261028")
    env = tmp_path / ".env"
    env.write_text("FNMUSIC_LLM_DAILY_TOKEN_LIMIT=10000000\n", encoding="utf-8")
    monkeypatch.setattr(webui, "ENV_PATH", env)
    original = ledger.read_bytes()
    with TestClient(webui.app) as client:
        client.headers["X-Trim-Isadmin"] = "true"
        result = client.get("/api/llm/usage?limit=1&offset=1")
        assert result.status_code == 200
        usage = result.json()["usage"]
        assert usage["dailyLimit"] == 10000000
        assert usage["total"] == 26
        assert usage["allTime"]["total"] == 41
        assert [day["date"] for day in usage["history"]] == ["20261027"]
        env.write_text("FNMUSIC_LLM_DAILY_TOKEN_LIMIT=0\n", encoding="utf-8")
        assert client.get("/api/llm/usage").json()["usage"]["dailyLimit"] == 0
        for query in ["limit=0", "limit=101", "offset=-1", "limit=bad"]:
            assert client.get("/api/llm/usage?" + query).status_code in (400, 422)
    assert ledger.read_bytes() == original


def test_usage_requires_admin():
    with TestClient(load_webui().app) as client:
        assert client.get("/api/llm/usage").status_code == 403


def test_usage_legacy_is_read_only_and_fresh_even_same_file_size(tmp_path, monkeypatch):
    webui = load_webui()
    ledger = tmp_path / "usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(ledger))
    monkeypatch.setattr(webui.llm_usage, "_today", lambda: "20261028")
    monkeypatch.setattr(webui, "ENV_PATH", tmp_path / ".env")
    legacy = '{"date":"20261028","prompt":20,"completion":10,"total":30,"requests":1}'
    ledger.write_text(legacy, encoding="utf-8")
    with TestClient(webui.app) as client:
        client.headers["X-Trim-Isadmin"] = "true"
        assert client.get('/api/llm/usage').json()['usage']['total'] == 30
        assert ledger.read_text(encoding="utf-8") == legacy
        newer = legacy.replace(':20', ':21').replace(':30', ':31')
        assert len(newer) == len(legacy)
        ledger.write_text(newer, encoding="utf-8")
        assert client.get('/api/llm/usage').json()['usage']['total'] == 31
        assert ledger.read_text(encoding="utf-8") == newer
        ledger.write_text('{broken', encoding="utf-8")
        assert client.get('/api/llm/usage').json()['usage']['status'] == 'corrupt'
        assert ledger.read_text(encoding="utf-8") == '{broken'
