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


def test_favorite_write_failure_reports_error(state, monkeypatch):
    calls = []
    with client_for(handler_for(calls)) as client:
        monkeypatch.setattr(p, "save_online_favorites", lambda *args: False)
        result = client.post("/music/api/v1/folder-view/delete", json={"folderPaths": [], "trackGUIDs": [state], "isPermanent": False})
    assert result.json()["code"] != 0
    assert len(recommend.load_online_play_history("u1")) == 2
