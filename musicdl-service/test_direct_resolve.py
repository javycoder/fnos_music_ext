from fastapi.testclient import TestClient
import asyncio
import pytest

import app as service


def test_internal_resolve_fails_closed_without_token(monkeypatch):
    monkeypatch.setattr(service, "_INTERNAL_TOKEN", "")
    with TestClient(service.app, raise_server_exceptions=False) as client:
        response = client.get("/internal/resolve", params={"id": "kuwo:1"})
    assert response.status_code == 503


def test_internal_resolve_returns_description_without_stream(monkeypatch):
    monkeypatch.setattr(service, "_INTERNAL_TOKEN", "secret")
    async def resolve(song_id):
        return {"ts": service.time.time(), "item": {
            "id": "kuwo:1", "source": "kuwo", "download_url": "https://cdn.example/a.flac",
            "ext": "flac", "file_size": 123,
        }}
    monkeypatch.setattr(service, "_direct_resolve_entry", resolve)
    with TestClient(service.app) as client:
        response = client.get(
            "/internal/resolve", params={"id": "kuwo:1"}, headers={"X-Internal-Token": "secret"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] == "https://cdn.example/a.flac"
    assert payload["server_headers"] == {}
    assert payload["expiry_source"] == "policy"


def test_internal_resolve_rejects_bad_token(monkeypatch):
    monkeypatch.setattr(service, "_INTERNAL_TOKEN", "secret")
    async def resolve(_song_id):
        raise AssertionError("must not resolve")
    monkeypatch.setattr(service, "_resolve_entry", resolve)
    with TestClient(service.app, raise_server_exceptions=False) as client:
        response = client.get(
            "/internal/resolve", params={"id": "kuwo:1"}, headers={"X-Internal-Token": "bad"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_expired_direct_resolution_refreshes_once_without_decode_probe(monkeypatch):
    monkeypatch.setattr(service, "_DIRECT_RESOLVE_INFLIGHT", {})
    monkeypatch.setattr(service, "_cache_get", lambda _song: {
        "ts": 0, "item": {"download_url": "https://expired.example/a.flac"}})
    def forbidden(*_args):
        raise AssertionError("direct resolution must not decode or open a stream")
    monkeypatch.setattr(service, "_head_probe_sync", forbidden)
    monkeypatch.setattr(service, "_fetch_upstream_stream", forbidden)
    calls = []
    selected = {"ts": service.time.time(), "item": {"download_url": "https://cdn.example/a.flac"}}
    async def refresh(song):
        calls.append(song)
        await asyncio.sleep(0)
        return selected
    monkeypatch.setattr(service, "_refresh_by_keyword", refresh)
    results = await asyncio.gather(*(service._direct_resolve_entry("kuwo:1") for _ in range(5)))
    assert calls == ["kuwo:1"]
    assert all(item is selected for item in results)


@pytest.mark.asyncio
async def test_expired_resolution_failure_never_returns_old_link(monkeypatch):
    monkeypatch.setattr(service, "_DIRECT_RESOLVE_INFLIGHT", {})
    monkeypatch.setattr(service, "_cache_get", lambda _song: {
        "ts": 0, "item": {"download_url": "https://expired.example/a.flac"}})
    async def failed(_song):
        return None
    monkeypatch.setattr(service, "_refresh_by_keyword", failed)
    with pytest.raises(service.HTTPException) as error:
        await service._direct_resolve_entry("kuwo:1")
    assert error.value.status_code == 502


def test_selected_background_stream_never_resolves_or_changes_encoding(monkeypatch):
    monkeypatch.setattr(service, "_INTERNAL_TOKEN", "fixture-secret")
    monkeypatch.setattr(service, "_DIRECT_SELECTIONS", {})
    async def resolve(_song):
        return {"ts": service.time.time(), "item": {
            "id": "kuwo:1", "download_url": "https://cdn.example/original.flac", "ext": "flac",
        }, "download_headers": {"Referer": "https://source.example"}}
    monkeypatch.setattr(service, "_direct_resolve_entry", resolve)
    opened = []
    class Upstream:
        status_code = 200
        headers = {"Content-Type": "audio/flac"}
        def iter_content(self, _size):
            yield b"selected bytes"
        def close(self):
            pass
    async def fetch(url, headers):
        opened.append((url, headers))
        return Upstream()
    monkeypatch.setattr(service, "_fetch_upstream_stream", fetch)
    with TestClient(service.app) as client:
        headers = {"X-Internal-Token": "fixture-secret"}
        selected = client.get("/internal/resolve", params={"id": "kuwo:1"}, headers=headers).json()
        async def forbidden(*_args):
            raise AssertionError("selected stream must not resolve again")
        monkeypatch.setattr(service, "_direct_resolve_entry", forbidden)
        stream = client.get("/internal/stream", params={"selection": selected["selection_id"]}, headers=headers)
        assert stream.content == b"selected bytes"
        assert opened == [("https://cdn.example/original.flac", {
            "Referer": "https://source.example", "Accept-Encoding": "identity"})]
        denied = client.get("/internal/stream", params={"selection": selected["selection_id"]})
        assert denied.status_code == 503
        expired = client.get("/internal/stream", params={"selection": "unknown"}, headers=headers)
        assert expired.status_code == 409
