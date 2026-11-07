import httpx
import pytest
from fastapi.testclient import TestClient
import proxy.app as p
from proxy import recommend

GUID = "online:kuwo:123"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setitem(p.CONF, "fav_dir", str(tmp_path / "favorites"))
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "history"))
    p._FAKE_GUID_REVERSE.clear()
    fake = p.fake_official_guid(GUID)
    for user in ("u1", "u2"):
        assert p.save_online_favorites(user, [{"guid": GUID}, {"guid": "keep"}])
        assert recommend.save_online_play_history(user, [{"guid": GUID}, {"guid": "keep"}])
    yield fake
    p._FAKE_GUID_REVERSE.clear()


def handler_for(calls, *, role="admin", status=0):
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"guid": "u1", "role": role}})
        return httpx.Response(200, json={"code": status, "data": None})
    return handler


def client_for(handler):
    p.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://unix")
    return TestClient(p.app)


def test_online_delete_only_changes_current_user_data(state):
    calls = []
    with client_for(handler_for(calls)) as client:
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": [], "trackGUIDs": [state], "isPermanent": True})
    assert result.json()["code"] == 0
    assert [x["guid"] for x in p.load_online_favorites("u1")] == ["keep"]
    assert [x["guid"] for x in recommend.load_online_play_history("u1")] == ["keep"]
    assert len(p.load_online_favorites("u2")) == 2
    assert len(recommend.load_online_play_history("u2")) == 2
    assert [r.url.path for r in calls] == ["/music/api/v1/user/me"]


def test_mixed_delete_forwards_only_official(state):
    calls = []
    with client_for(handler_for(calls)) as client:
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": ["/library/sub"], "trackGUIDs": [state, "official-id"], "isPermanent": False})
    assert result.json()["code"] == 0
    import json
    sent = json.loads(calls[-1].content)
    assert sent == {"folderPaths": ["/library/sub"], "trackGUIDs": ["official-id"], "isPermanent": False}


@pytest.mark.parametrize("role,status", [("member", 0), ("admin", 100005)])
def test_permission_and_upstream_failure_do_not_modify_online_data(state, role, status):
    calls = []
    with client_for(handler_for(calls, role=role, status=status)) as client:
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": [], "trackGUIDs": [state, "official-id"], "isPermanent": False})
    assert result.status_code == 403 if role == "member" else result.json()["code"] == status
    assert len(p.load_online_favorites("u1")) == 2
    assert len(recommend.load_online_play_history("u1")) == 2


def test_official_only_request_preserves_official_validation(state):
    calls = []
    with client_for(handler_for(calls, status=123)) as client:
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": ["/library"]})
    assert result.json()["code"] == 123
    assert [r.url.path for r in calls] == ["/music/api/v1/folder-view/delete"]


@pytest.mark.parametrize("corrupt", ["favorites", "history"])
def test_corrupt_user_state_prevents_all_deletion(state, corrupt):
    from pathlib import Path
    path = (p.user_fav_path("u1") if corrupt == "favorites" else
            str(Path(recommend.play_history_dir()) / "u1.json"))
    Path(path).write_text("{invalid", encoding="utf-8")
    calls = []
    with client_for(handler_for(calls)) as client:
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": [], "trackGUIDs": [state, "official-id"], "isPermanent": False})
    assert result.json()["code"] != 0
    assert Path(path).read_text(encoding="utf-8") == "{invalid"
    assert [r.url.path for r in calls] == ["/music/api/v1/user/me"]


def test_broken_state_symlink_fails_closed(state):
    from pathlib import Path
    path = Path(p.user_fav_path("u1"))
    path.unlink()
    try:
        path.symlink_to(path.parent / "missing-target")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    calls = []
    with client_for(handler_for(calls)) as client:
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": [], "trackGUIDs": [state, "official-id"], "isPermanent": False})
    assert result.json()["code"] != 0
    assert path.is_symlink()
    assert len(calls) == 1


def test_history_cleanup_keeps_large_history_and_metadata(state):
    import json
    from pathlib import Path
    path = Path(recommend.play_history_dir()) / "u1.json"
    items = [{"guid": GUID}] + [{"guid": f"keep-{i}"} for i in range(501)]
    path.write_text(json.dumps({"items": items, "extra": "preserve"}), encoding="utf-8")
    with client_for(handler_for([])) as client:
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": [], "trackGUIDs": [state], "isPermanent": False})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert result.json()["code"] == 0
    assert len(data["items"]) == 501
    assert data["extra"] == "preserve"


def test_favorite_write_failure_reports_error(state, monkeypatch):
    calls = []
    with client_for(handler_for(calls)) as client:
        monkeypatch.setattr(recommend, "_atomic_write_json", lambda *args: False)
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": [], "trackGUIDs": [state], "isPermanent": False})
    assert result.json()["code"] != 0
    assert len(recommend.load_online_play_history("u1")) == 2


@pytest.mark.parametrize("corrupt", ["favorites", "history"])
@pytest.mark.parametrize("bad_guid", [[], {}, None, 123, ""])
def test_invalid_entry_guid_prevents_official_delete(state, corrupt, bad_guid):
    import json
    from pathlib import Path

    path = (Path(p.user_fav_path("u1")) if corrupt == "favorites" else
            Path(recommend.play_history_dir()) / "u1.json")
    original = json.dumps({"items": [{"guid": bad_guid}]})
    path.write_text(original, encoding="utf-8")
    calls = []
    with client_for(handler_for(calls)) as client:
        result = client.post("/music/api/v1/folder-view/delete", json={
            "folderPaths": [], "trackGUIDs": [state, "official-id"], "isPermanent": False,
        })
    assert result.json()["code"] != 0
    assert path.read_text(encoding="utf-8") == original
    assert [request.url.path for request in calls] == ["/music/api/v1/user/me"]
