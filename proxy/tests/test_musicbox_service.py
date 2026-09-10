import io
import os
import sys
import importlib.util
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

# musicbox-service 内部使用裸导入（import runner / from netease_ext import ...），
# 仍需把服务目录加入 sys.path；但 app 本体以独立模块名加载，
# 避免与其他服务的顶层 `app` 模块在同一 pytest 会话中冲突
MUSICBOX_SERVICE_DIR = Path(__file__).resolve().parent.parent.parent / "musicbox-service"
if str(MUSICBOX_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(MUSICBOX_SERVICE_DIR))

import runner

_spec = importlib.util.spec_from_file_location("musicbox_service_app", MUSICBOX_SERVICE_DIR / "app.py")
musicbox_app = importlib.util.module_from_spec(_spec)
sys.modules["musicbox_service_app"] = musicbox_app
_spec.loader.exec_module(musicbox_app)

app = musicbox_app.app
UpstreamException = musicbox_app.UpstreamException


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


def test_musicbox_search_filters_unplayable_songs(monkeypatch):
    import netease_ext

    # Mock CLI search output: 4 items (free, vip-no-url, trial, empty-url)
    mock_search_data = {
        "ok": True,
        "data": [
            {"song_id": 101, "song_name": "Free Song", "artist": "Singer", "quality": "exhigh"},
            {"song_id": 102, "song_name": "VIP Song No URL", "artist": "Singer", "quality": "lossless"},
            {"song_id": 103, "song_name": "Trial Snippet Song", "artist": "Singer", "quality": "standard"},
            {"song_id": 104, "song_name": "Dead Song", "artist": "Singer", "quality": "standard"},
        ],
    }

    def mock_run_musicbox(args, timeout=30.0):
        import json
        return 0, json.dumps(mock_search_data), ""

    # Mock songs_url responses
    def mock_songs_url(ids):
        return [
            {"id": 101, "url": "http://audio.126.net/101.mp3", "code": 200, "fee": 0, "freeTrialInfo": None},
            {"id": 102, "url": None, "code": 404, "fee": 1, "freeTrialInfo": None},
            {"id": 103, "url": "http://audio.126.net/103_trial.mp3", "code": 200, "fee": 1, "freeTrialInfo": {"start": 0, "end": 30}},
            {"id": 104, "url": "", "code": 200, "fee": 0, "freeTrialInfo": None},
        ]

    class MockApi:
        def songs_url(self, ids):
            return mock_songs_url(ids)

        def get_account_info(self):
            return {"code": 200, "account": None, "profile": None}

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    monkeypatch.setattr(netease_ext, "_get_api", lambda: MockApi())

    with TestClient(app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "test", "type": "song", "limit": 20})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Only 101 is playable
        assert len(data["data"]) == 1
        assert data["data"][0]["song_id"] == 101


def test_musicbox_songs_detail_filters_unplayable(monkeypatch):
    import netease_ext

    def mock_songs_detail(ids):
        return [
            {"id": 101, "name": "Free Song", "ar": [{"name": "A"}], "al": {"name": "Album", "picUrl": "http://img/1.jpg"}, "dt": 200000},
            {"id": 102, "name": "VIP Song", "ar": [{"name": "A"}], "al": {"name": "Album", "picUrl": "http://img/2.jpg"}, "dt": 200000},
            {"id": 103, "name": "Trial Song", "ar": [{"name": "A"}], "al": {"name": "Album", "picUrl": "http://img/3.jpg"}, "dt": 30000},
        ]

    def mock_songs_url(ids):
        return [
            {"id": 101, "url": "http://audio.126.net/101.mp3", "code": 200, "fee": 0, "freeTrialInfo": None},
            {"id": 102, "url": None, "code": 404, "fee": 1, "freeTrialInfo": None},
            {"id": 103, "url": "http://audio.126.net/103_trial.mp3", "code": 200, "fee": 1, "freeTrialInfo": {"start": 0, "end": 30}},
        ]

    class MockApi:
        def songs_detail(self, ids):
            return mock_songs_detail(ids)

        def songs_url(self, ids):
            return mock_songs_url(ids)

        def get_account_info(self):
            return {"code": 200, "account": None, "profile": None}

    monkeypatch.setattr(netease_ext, "_get_api", lambda: MockApi())

    with TestClient(app) as client:
        resp = client.get("/api/v1/songs/detail", params={"ids": "101,102,103"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Only 101 is returned
        assert len(data["data"]) == 1
        assert data["data"][0]["song_id"] == 101


def test_musicbox_search_logged_in_vip_playable(monkeypatch):
    import netease_ext

    mock_search_data = {
        "ok": True,
        "data": [
            {"song_id": 201, "song_name": "VIP Song With Perm", "artist": "Singer", "quality": "lossless"},
            {"song_id": 202, "song_name": "Paid Album Without Perm", "artist": "Singer", "quality": "lossless"},
        ],
    }

    def mock_run_musicbox(args, timeout=30.0):
        import json
        return 0, json.dumps(mock_search_data), ""

    def mock_songs_url(ids):
        return [
            # 201 has full valid url and no trial
            {"id": 201, "url": "http://audio.126.net/vip_full.mp3", "code": 200, "fee": 1, "freeTrialInfo": None},
            # 202 has no url (account didn't buy album)
            {"id": 202, "url": None, "code": 404, "fee": 4, "freeTrialInfo": None},
        ]

    class MockApi:
        def songs_url(self, ids):
            return mock_songs_url(ids)

        def get_account_info(self):
            # Logged in
            return {"code": 200, "account": {"id": 12345}, "profile": {"nickname": "VIPUser"}}

    monkeypatch.setattr(runner, "run_musicbox", mock_run_musicbox)
    monkeypatch.setattr(netease_ext, "_get_api", lambda: MockApi())

    with TestClient(app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "test", "type": "song", "limit": 20})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["song_id"] == 201




# ------------------------------------------------ 未覆盖端点与错误信封补测 ---

def test_healthz_endpoint():
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    rj = resp.json()
    assert rj["status"] == "ok"
    assert "musicbox" in rj["source"]


def test_song_url_invalid_quality_rejected():
    with TestClient(app) as client:
        resp = client.get("/api/v1/song/123/url", params={"quality": "ultra"})
    assert resp.status_code == 400
    assert "Invalid quality" in resp.json()["detail"]


def test_song_url_passes_quality_to_cli(monkeypatch):
    captured = {}

    def mock_run(args, timeout=30.0):
        captured["args"] = args
        return 0, '{"ok": true, "data": {"code": 200, "url": "http://m.test/a.flac"}}', ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run)
    with TestClient(app) as client:
        resp = client.get("/api/v1/song/123/url", params={"quality": "lossless"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["data"]["code"] == 200
    assert resp.json()["data"]["url"].endswith(".flac")
    assert captured["args"] == ["song", "url", "123", "--quality", "lossless", "--json"]


def test_song_info_artist_album_playlist_cli_args(monkeypatch):
    captured = []

    def mock_run(args, timeout=30.0):
        captured.append(args)
        return 0, '{"ok": true, "data": {"id": 1}}', ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run)
    with TestClient(app) as client:
        assert client.get("/api/v1/song/123/info").status_code == 200
        assert client.get("/api/v1/artist/456", params={"limit": 50}).status_code == 200
        assert client.get("/api/v1/album/789").status_code == 200
        assert client.get("/api/v1/playlist/1000").status_code == 200
    assert captured == [
        ["song", "info", "123", "--json"],
        ["artist", "456", "--limit", "50", "--json"],
        ["album", "789", "--json"],
        ["playlist", "show", "1000", "--json"],
    ]


def test_song_lyric_ok_and_upstream_error(monkeypatch):
    import netease_ext

    class FakeApi:
        def song_lyric(self, sid):
            assert sid == 123
            return ["[00:01]晴天"]

        def song_tlyric(self, sid):
            return []

    monkeypatch.setattr(netease_ext, "_get_api", lambda: FakeApi())
    with TestClient(app) as client:
        resp = client.get("/api/v1/song/123/lyric")
    assert resp.status_code == 200
    rj = resp.json()
    assert rj["ok"] is True
    assert rj["data"]["lyric"] == "[00:01]晴天"
    assert rj["data"]["tlyric"] == ""

    def _boom(sid):
        raise RuntimeError("lyric upstream down")

    monkeypatch.setattr(musicbox_app, "song_lyric_pair", _boom)
    with TestClient(app) as client:
        resp = client.get("/api/v1/song/123/lyric")
    assert resp.status_code == 200
    rj = resp.json()
    assert rj["ok"] is False
    assert "lyric upstream down" in rj["error"]


def test_auth_status_and_login_check(monkeypatch):
    captured = {}

    def mock_run(args, timeout=30.0):
        captured["args"] = args
        return 0, '{"ok": true, "data": {"logged_in": true}}', ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")
        assert resp.status_code == 200
        assert resp.json()["data"]["logged_in"] is True
        assert captured["args"] == ["auth", "status", "--json"]

        # 空 unikey 直接 400，不触达上游
        resp2 = client.get("/api/v1/auth/login/check", params={"unikey": "   "})
        assert resp2.status_code == 400

        resp3 = client.get("/api/v1/auth/login/check", params={"unikey": "key-1"})
        assert resp3.status_code == 200
        assert captured["args"] == ["auth", "login", "--check", "key-1", "--json"]


def test_auth_login_post_builds_qr_url(monkeypatch):
    def mock_run(args, timeout=30.0):
        return 0, '{"ok": true, "data": {"unikey": "key-abc", "qr_ascii": "x"}}', ""

    monkeypatch.setattr(runner, "run_musicbox", mock_run)
    with TestClient(app) as client:
        resp = client.post("/api/v1/auth/login")
    assert resp.status_code == 200
    payload = resp.json()["data"]
    assert payload["qr_url"] == "https://music.163.com/login?codekey=key-abc"


def test_upstream_failure_maps_to_502(monkeypatch):
    def mock_run(args, timeout=30.0):
        return 3, "", "musicbox exploded"

    monkeypatch.setattr(runner, "run_musicbox", mock_run)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")
    assert resp.status_code == 502
    rj = resp.json()
    assert rj["error"] == "upstream_error"
    assert rj["exit_code"] == 3
    assert "musicbox exploded" in rj["stderr"]


def test_upstream_timeout_maps_to_504(monkeypatch):
    def mock_run(args, timeout=30.0):
        raise runner.MusicboxTimeoutError("musicbox timed out after 30s")

    monkeypatch.setattr(runner, "run_musicbox", mock_run)
    with TestClient(app) as client:
        resp = client.get("/api/v1/auth/status")
    assert resp.status_code == 504
