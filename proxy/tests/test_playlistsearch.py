"""在线歌单搜索的来源选择、时限、分页和发现授权。"""
import asyncio
import json
import time

import pytest

from proxy import playlistsearch
from proxy import publicplaylists as pubpl


def _conf(**extra):
    base = {
        "netease_enabled": False,
        "netease_playlist_resident": False,
        "lx_enabled": False,
        "lx_sources": [],
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    playlistsearch.reset_for_test()
    pubpl.reset_for_test()
    monkeypatch.setenv("FNMUSIC_PLAYLIST_SEARCH_DIR", str(tmp_path / "grants"))
    monkeypatch.setenv("FNMUSIC_PUBLIC_PLAYLISTS_DIR", str(tmp_path / "public"))
    yield
    playlistsearch.reset_for_test()
    pubpl.reset_for_test()


def _item(identifier, name, track_count=1):
    return {"id": identifier, "name": name, "track_count": track_count, "cover_url": ""}


@pytest.mark.anyio
async def test_sources_follow_enabled_platforms_and_resident_netease():
    seen = []

    async def fetch(target, keyword, page, timeout):
        seen.append((target.provider, target.source, page))
        return {"items": [_item(target.source, target.source)], "raw_count": 1, "has_more": False}

    async def platforms(timeout):
        return ["wy", "kw"], True

    result = await playlistsearch.load_page(
        conf=_conf(lx_enabled=True, lx_sources=["kw"], netease_playlist_resident=True),
        user_guid="user-a", keyword="华语", page=1, size=24, official_total=0,
        deadline=time.monotonic() + 2, resolve_platforms=platforms, fetch_page=fetch,
    )
    assert seen == [("netease", "netease", 1), ("lx", "kw", 1)]
    assert [card["name"] for card in result.cards] == ["[网易] netease", "[酷我] kw"]
    assert result.interrupted is False


@pytest.mark.anyio
async def test_disabled_sources_make_no_requests():
    async def fetch(*_args):
        raise AssertionError("disabled source was queried")

    async def platforms(*_args):
        raise AssertionError("lx capabilities were queried")

    result = await playlistsearch.load_page(
        conf=_conf(), user_guid="user-a", keyword="华语", page=1, size=24, official_total=0,
        deadline=time.monotonic() + 1, resolve_platforms=platforms, fetch_page=fetch,
    )
    assert result.cards == []
    assert result.open_more is False


@pytest.mark.anyio
async def test_slow_source_does_not_hold_the_fast_result(monkeypatch):
    monkeypatch.setattr(playlistsearch, "GRACE_AFTER_FIRST_S", 0.2)

    async def fetch(target, keyword, page, timeout):
        if target.source == "wy":
            await asyncio.sleep(5)
        return {"items": [_item(target.source, target.source)], "raw_count": 1, "has_more": False}

    async def platforms(_timeout):
        return ["kw", "wy"], True

    started = time.monotonic()
    result = await playlistsearch.load_page(
        conf=_conf(lx_enabled=True, lx_sources=["kw", "wy"]),
        user_guid="user-a", keyword="华语", page=1, size=24, official_total=0,
        deadline=started + 2, resolve_platforms=platforms, fetch_page=fetch,
    )
    assert time.monotonic() - started < 1.2
    assert [card["name"] for card in result.cards] == ["[酷我] kw"]
    assert result.interrupted is True


@pytest.mark.anyio
async def test_failed_page_keeps_cursor_retriable():
    calls = []

    async def fetch(target, keyword, page, timeout):
        calls.append(page)
        if len(calls) == 1:
            raise RuntimeError("timeout")
        return {"items": [_item("1", "重试成功")], "raw_count": 1, "has_more": False}

    async def platforms(_timeout):
        return ["kw"], True

    kwargs = dict(
        conf=_conf(lx_enabled=True, lx_sources=["kw"]), user_guid="user-a", keyword="华语",
        page=1, size=24, official_total=0, resolve_platforms=platforms, fetch_page=fetch,
    )
    first = await playlistsearch.load_page(deadline=time.monotonic() + 1, **kwargs)
    assert first.cards == [] and first.interrupted is True
    second = await playlistsearch.load_page(deadline=time.monotonic() + 1, **kwargs)
    assert calls == [1, 1]
    assert second.cards[0]["name"] == "[酷我] 重试成功"


@pytest.mark.anyio
async def test_later_page_continues_and_dedups_netease_identity():
    async def fetch(target, keyword, page, timeout):
        if target.provider == "netease":
            return {
                "items": [_item("9", "网易同单"), _item("10", "网易第二首")],
                "raw_count": 2, "has_more": False,
            }
        return {"items": [_item("9", "洛雪同单"), _item("11", "酷我另一份")], "raw_count": 2, "has_more": False}

    async def platforms(_timeout):
        return ["wy"], True

    conf = _conf(netease_enabled=True, lx_enabled=True, lx_sources=["wy"])
    first = await playlistsearch.load_page(
        conf=conf, user_guid="user-a", keyword="同名", page=1, size=2, official_total=0,
        deadline=time.monotonic() + 1, resolve_platforms=platforms, fetch_page=fetch,
    )
    assert [card["name"] for card in first.cards] == ["[网易] 网易同单", "[网易] 网易第二首"]
    second = await playlistsearch.load_page(
        conf=conf, user_guid="user-a", keyword="同名", page=2, size=2, official_total=0,
        deadline=time.monotonic() + 1, resolve_platforms=platforms, fetch_page=fetch,
    )
    assert [card["name"] for card in second.cards] == ["[网易] 酷我另一份"]
    assert second.open_more is False


@pytest.mark.anyio
async def test_discovery_grant_is_per_user_and_survives_restart():
    async def fetch(target, keyword, page, timeout):
        return {"items": [_item("55", "发现歌单", 3)], "raw_count": 1, "has_more": False}

    async def platforms(_timeout):
        return ["kw"], True

    conf = _conf(lx_enabled=True, lx_sources=["kw"])
    result = await playlistsearch.load_page(
        conf=conf, user_guid="user-a", keyword="发现", page=1, size=24, official_total=0,
        deadline=time.monotonic() + 1, resolve_platforms=platforms, fetch_page=fetch,
    )
    await playlistsearch.grant_delivered("user-a", result.founds)
    entry = result.founds[0].entry
    assert playlistsearch.discovery_allows("user-a", entry, conf) is True
    assert playlistsearch.discovery_allows("user-b", entry, conf) is False
    assert playlistsearch.discovery_allows("shared", entry, conf) is False
    assert playlistsearch.discovery_allows("user-a", entry, _conf()) is False

    registered = []
    playlistsearch.reset_for_test()
    restored = playlistsearch.restore_discovered(registered.append)
    assert restored and registered[0].startswith("online:playlist:public:lx:kw:playlist:")
    assert playlistsearch.discovery_allows("user-a", entry, conf) is True
    assert "cover_url" not in json.dumps(playlistsearch._export_grants())


def test_corrupt_grant_file_does_not_authorize(tmp_path, monkeypatch):
    path = tmp_path / "grants" / "discovery-grants.json"
    path.parent.mkdir()
    path.write_text("{", encoding="utf-8")
    assert playlistsearch.restore_discovered(lambda _guid: None) == []
    entry = {"provider": "lx", "source": "kw", "kind": "playlist", "id": "1"}
    assert playlistsearch.discovery_allows("user-a", entry, _conf(lx_enabled=True, lx_sources=["kw"])) is False
