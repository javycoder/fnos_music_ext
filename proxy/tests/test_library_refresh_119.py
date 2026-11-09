import sqlite3
import httpx
import pytest
from fastapi.testclient import TestClient
import proxy.app as p


@pytest.fixture
def library_env(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = old / "child"
    new.mkdir(parents=True)
    db = tmp_path / "music.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, path TEXT)")
        con.execute("INSERT INTO shared_library VALUES (1, ?)", (str(old),))
    monkeypatch.setitem(p.CONF, "music_db", str(db))
    monkeypatch.setitem(p.CONF, "library_dir", "")
    monkeypatch.setitem(p.CONF, "cache_dir", str(tmp_path / "cache"))
    p.invalidate_library_dir()
    yield db, old, new
    p.invalidate_library_dir()


@pytest.mark.parametrize("action", ["create", "edit", "delete", "reconnect"])
def test_success_invalidates_library_cache(library_env, action):
    db, old, new = library_env
    assert p.detect_library_dir() == str(old)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shared_library SET path = ?", (str(new),))
    assert p.detect_library_dir() == str(old)
    p.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 0, "data": None})), base_url="http://unix")
    with TestClient(p.app) as client:
        assert client.post(f"/music/api/v1/shared-library/{action}", json={}).json()["code"] == 0
    assert p.detect_library_dir() == str(new)


def test_reconnect_check_does_not_invalidate(library_env):
    db, old, new = library_env
    assert p.detect_library_dir() == str(old)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shared_library SET path = ?", (str(new),))
    p.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 0, "data": None})), base_url="http://unix")
    with TestClient(p.app) as client:
        assert client.post("/music/api/v1/shared-library/reconnect/check", json={}).json()["code"] == 0
    assert p.detect_library_dir() == str(old)


def test_error_does_not_invalidate(library_env):
    db, old, new = library_env
    assert p.detect_library_dir() == str(old)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shared_library SET path = ?", (str(new),))
    p.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 100003, "data": None})), base_url="http://unix")
    with TestClient(p.app) as client:
        assert client.post("/music/api/v1/shared-library/edit", json={}).json()["code"] == 100003
    assert p.detect_library_dir() == str(old)


def test_repeated_invalidation_has_bounded_retries(library_env, monkeypatch):
    calls = []
    real_isdir = p.os.path.isdir

    def invalidate_during_query(path):
        calls.append(path)
        p.invalidate_library_dir()
        return real_isdir(path)

    monkeypatch.setattr(p.os.path, "isdir", invalidate_during_query)
    assert p.detect_library_dir() == p.CONF["cache_dir"]
    assert len(calls) == 3
    assert not p._DETECTED_LIBRARY_DIR


def test_explicit_path_takes_precedence(library_env, monkeypatch):
    _, _, new = library_env
    monkeypatch.setitem(p.CONF, "library_dir", str(new))
    assert p.detect_library_dir() == str(new)


def test_ttl_detects_external_change(library_env, monkeypatch):
    db, old, new = library_env
    clock = [100.0]
    monkeypatch.setattr(p.time, "monotonic", lambda: clock[0])
    assert p.detect_library_dir() == str(old)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE shared_library SET path = ?", (str(new),))
    clock[0] += p._LIBRARY_DIR_TTL + 1
    assert p.detect_library_dir() == str(new)
