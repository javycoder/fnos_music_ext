from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from lxserver_client import UnsafeVmRequired
from managed_client import ManagedLxServerClient
from source_runtime import (
    SourceNotFound,
    SourceProcessBusy,
    SourceResolveTimeout,
)


class _StubManager:
    def __init__(self):
        self.records = [
            {
                "id": "a.js", "name": "A", "version": "1.2.3", "enabled": True,
                "supportedSources": ["kw"], "runtime_id": "run-a",
                "resource_usage": {"monitoring_status": "ok"},
            }
        ]
        self.calls = []

    async def list_sources(self):
        return list(self.records)

    async def import_source(self, url, *, deadline):
        self.calls.append(("import", url, deadline))
        return dict(self.records[0], sourceUrl=url)

    async def upload_source(self, filename, content, *, deadline, source_url=""):
        self.calls.append(("upload", filename, content, deadline, source_url))
        return dict(self.records[0])

    async def resolve_source_id(self, target):
        if target in ("a.js", "A"):
            return "a.js"
        raise SourceNotFound(target)

    async def set_enabled(self, source_id, enabled, *, deadline):
        self.calls.append(("set_enabled", source_id, enabled, deadline))
        return source_id == "a.js"

    async def delete_source(self, source_id, *, deadline):
        self.calls.append(("delete", source_id, deadline))
        return source_id == "a.js"

    async def reorder_sources(self, source_ids):
        self.calls.append(("reorder", list(source_ids)))
        return {"success": True, "sourceIds": list(source_ids)}

    async def resolve(self, source_id, song_info, quality, *, deadline):
        self.calls.append(("resolve", source_id, song_info, quality, deadline))
        if source_id == "timeout.js":
            raise SourceResolveTimeout()
        if source_id == "busy.js":
            raise SourceProcessBusy()
        return {
            "url": "https://media.test/a.flac",
            "type": quality,
            "sourceId": source_id,
        }


def _client(manager):
    return ManagedLxServerClient(base_url="http://test", manager=manager)


def test_source_management_uses_manager_and_keeps_payload_contract():
    manager = _StubManager()
    client = _client(manager)

    async def run():
        rows = await client.list_custom_sources()
        assert rows[0]["id"] == "a.js"
        imported = await client.import_custom_source("https://example.test/a.js")
        assert imported["success"] is True
        assert imported["id"] == "a.js"
        assert imported["metadata"]["version"] == "1.2.3"
        assert imported["allowUnsafeVM"] is False
        uploaded = await client.upload_custom_source("a.js", "/*! @name A */")
        assert uploaded["supportedSources"] == ["kw"]
        assert await client.set_source_enabled("A", True) is True
        assert await client.set_source_enabled("missing.js", True) is False
        assert await client.delete_custom_source("a.js") is True
        assert await client.reorder_custom_sources(["a.js"]) == {
            "success": True, "sourceIds": ["a.js"]
        }

    asyncio.run(run())
    assert [call[0] for call in manager.calls] == [
        "import", "upload", "set_enabled", "delete", "reorder"
    ]


def test_unsafe_vm_is_never_requested_from_manager():
    manager = _StubManager()
    client = _client(manager)

    async def run():
        with pytest.raises(UnsafeVmRequired):
            await client.upload_custom_source("a.js", "script", allow_unsafe_vm=True)
        with pytest.raises(UnsafeVmRequired):
            await client.import_custom_source("https://example.test/a.js", allow_unsafe_vm=True)

    asyncio.run(run())
    assert manager.calls == []


def test_pinned_resolution_passes_exact_source_id_and_preserves_budget():
    manager = _StubManager()
    client = _client(manager)
    song = {"source": "kw", "meta": {"hash": "abc"}}

    async def run():
        started = time.monotonic()
        result = await client.get_music_url(
            song, "flac", ["b.js"], 2.5, False, source_id="a.js"
        )
        assert result["sourceId"] == "a.js"
        assert result["url"].startswith("https://")
        assert time.monotonic() - started < 1.0

    asyncio.run(run())
    name, source_id, passed_song, quality, deadline = manager.calls[-1]
    assert (name, source_id, passed_song, quality) == ("resolve", "a.js", song, "flac")
    assert 0 < deadline - time.monotonic() <= 2.5


def test_pinned_resolution_maps_timeout_and_busy_to_timeout():
    manager = _StubManager()
    client = _client(manager)

    async def run():
        with pytest.raises(TimeoutError):
            await client.get_music_url({}, "128k", source_id="timeout.js")
        with pytest.raises(TimeoutError):
            await client.get_music_url({}, "128k", source_id="busy.js")

    asyncio.run(run())
