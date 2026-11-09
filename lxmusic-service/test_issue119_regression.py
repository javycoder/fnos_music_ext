"""Issue #119: regressions for slow-but-valid LX sources and cross-track suppression."""
import asyncio

import pytest

from conftest import lxapp
from test_playback_recovery import _item, _media_client, _ready, _write_env


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """Use isolated per-test policy/cache state, matching playback-recovery coverage."""
    monkeypatch.setitem(lxapp.CONF, "data_dir", str(tmp_path / "data"))
    for key in ("FNMUSIC_LX_AUTO_SWITCH", "FNMUSIC_LX_PLATFORM_PREFERRED",
                "FNMUSIC_LX_SOURCE_ORDER", "LX_ENV_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(lxapp, "_POLICY_SIGNATURE", None, raising=False)
    monkeypatch.setattr(lxapp, "_PERSISTED_ENV_CACHE", None, raising=False)
    monkeypatch.setattr(lxapp, "_LAST_UPSTREAM_ORDER", None, raising=False)
    monkeypatch.setattr(lxapp, "_RESOLVE_TIMEOUT_COOLDOWN", {})
    for cache in (lxapp._SONG_CACHE, lxapp._URL_CACHE, lxapp._URL_INFLIGHT, lxapp._CHAIN_HEALTH):
        cache.clear()
    lxapp._CANDIDATES.invalidate()
    yield
    for cache in (lxapp._SONG_CACHE, lxapp._URL_CACHE, lxapp._URL_INFLIGHT, lxapp._CHAIN_HEALTH):
        cache.clear()
    lxapp._CANDIDATES.invalidate()


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_switch", ["false", "true"])
async def test_one_slow_valid_source_is_not_cut_short_by_lower_quality_steps(
    fake_lx, runtime, monkeypatch, tmp_path, auto_switch,
):
    """A single source must get its configured resolve window before the quality ladder splits it."""
    fake_lx.sources = [_ready("slow.js", ["kw"])]
    from test_playback_recovery import _write_env
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_AUTO_SWITCH": auto_switch})
    calls = []

    async def resolve(song_info, quality="128k", **kwargs):
        calls.append(quality)
        if quality == "flac":
            await asyncio.sleep(1.1)
            return {
                "url": "https://media.test/slow-valid.flac",
                "type": "flac",
                "sourceId": "slow.js",
            }
        return None

    monkeypatch.setattr(fake_lx, "get_music_url", resolve)
    client = _media_client()
    try:
        result = await lxapp.resolve_and_probe(
            client, "kw", _item(), "lossless", budget=4.0,
            resolver_timeout=2.0, probe_timeout=0.1,
        )
    finally:
        await client.aclose()

    assert result is not None
    assert result["source_script"] == "slow.js"
    assert calls == ["flac"]


@pytest.mark.asyncio
async def test_resolver_timeout_cooldown_is_candidate_scoped_and_only_for_full_timeout(
    fake_lx, runtime, monkeypatch,
):
    fake_lx.sources = [_ready("a.js", ["kw"]), _ready("b.js", ["kw"])]
    calls = []
    first = _item()
    second = {**_item(), "id": "lx:kw:second"}
    clipped = {**_item(), "id": "lx:kw:clipped"}

    async def attempt(*args, **kwargs):
        script_id = kwargs["script"].id
        track_id = kwargs["resolved_track_id"]
        calls.append((track_id, script_id))
        if script_id == "a.js" and track_id == first["id"]:
            return lxapp._AttemptResult(timeout=True, resolve_timeout=True)
        if script_id == "a.js" and track_id == clipped["id"] and calls.count((track_id, script_id)) == 1:
            return lxapp._AttemptResult(timeout=True, resolve_timeout=False)
        return lxapp._AttemptResult(data={
            "url": "https://media.test/good.flac",
            "ext": "flac",
            "resolution_quality": kwargs["quality"],
        })

    monkeypatch.setattr(lxapp, "_attempt_pinned", attempt)
    client = _media_client()
    try:
        assert await lxapp.resolve_and_probe(client, "kw", first, "lossless")
        assert await lxapp.resolve_and_probe(client, "kw", second, "lossless")
        assert await lxapp.resolve_and_probe(client, "kw", clipped, "lossless")
        assert await lxapp.resolve_and_probe(client, "kw", clipped, "lossless")
    finally:
        await client.aclose()

    # A complete timeout cools down only that exact track/script/platform/tier.
    assert (first["id"], "a.js") in calls
    assert (second["id"], "a.js") in calls
    # A budget-clipped timeout is retried immediately rather than poisoning the candidate.
    assert calls.count((clipped["id"], "a.js")) == 2
    key = lxapp._timeout_cooldown_key("a.js", "kw", lxapp._URL_CACHE_GEN,
                                      "lossless", first["id"])
    assert key in lxapp._RESOLVE_TIMEOUT_COOLDOWN
    clipped_key = lxapp._timeout_cooldown_key("a.js", "kw", lxapp._URL_CACHE_GEN,
                                              "lossless", clipped["id"])
    assert clipped_key not in lxapp._RESOLVE_TIMEOUT_COOLDOWN


@pytest.mark.asyncio
async def test_timeout_on_one_cross_platform_candidate_tries_next(fake_lx, runtime, monkeypatch, tmp_path):
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_PLATFORM_PREFERRED": "true"})
    fake_lx.sources = [_ready("a.js", ["kw", "kg"])]
    calls = []

    async def candidates(*args, **kwargs):
        return {"kg": [{"id": "lx:kg:first"}, {"id": "lx:kg:second"}]}

    async def attempt(*args, **kwargs):
        track_id = kwargs["resolved_track_id"]
        calls.append(track_id)
        if kwargs["platform"] == "kw":
            return lxapp._AttemptResult()
        if track_id.endswith("first"):
            return lxapp._AttemptResult(timeout=True, resolve_timeout=True)
        return lxapp._AttemptResult(data={"url": "https://media.test/good.mp3", "ext": "mp3",
                                         "resolution_quality": kwargs["quality"]})

    monkeypatch.setattr(lxapp._CANDIDATES, "find_many", candidates)
    monkeypatch.setattr(lxapp, "_attempt_pinned", attempt)
    client = _media_client()
    try:
        result = await lxapp.resolve_and_probe(client, "kw", _item(), "standard", budget=5.0)
    finally:
        await client.aclose()
    assert result is not None
    assert calls.index("lx:kg:first") < calls.index("lx:kg:second")


@pytest.mark.asyncio
async def test_media_probe_timeout_is_incomplete_and_not_negative_cached(
    fake_lx, runtime, monkeypatch,
):
    fake_lx.sources = [_ready("slow.js", ["kw"])]
    fake_lx.url_result = {
        "url": "https://media.test/slow.mp3", "type": "128k", "sourceId": "slow.js",
    }

    async def timed_out_probe(*args, **kwargs):
        raise TimeoutError("synthetic media probe timeout")

    monkeypatch.setattr(lxapp, "probe_url", timed_out_probe)
    settings = {"url_timeout": 1.0, "resolver_timeout": 0.2, "probe_timeout": 0.1}
    async with _media_client() as client:
        with pytest.raises(lxapp._ResolutionIncomplete):
            await lxapp._resolve_url_cached(
                client, "kw", _item(), "standard", budget=1.0, timeout_settings=settings,
            )
    assert lxapp._URL_CACHE == {}
    assert not lxapp._RESOLVE_TIMEOUT_COOLDOWN


def test_incomplete_resolution_returns_504_and_is_not_negative_cached(
    test_app_client, fake_lx, runtime, monkeypatch,
):
    fake_lx.sources = [_ready("slow.js", ["kw"])]
    settings = {"url_timeout": 1.0, "resolver_timeout": 0.2, "probe_timeout": 0.1}
    monkeypatch.setattr(lxapp, "_reload_timeout_settings", lambda: dict(settings))
    calls = []

    async def incomplete(*args, **kwargs):
        calls.append(1)
        error_type = getattr(lxapp, "_ResolutionIncomplete", TimeoutError)
        raise error_type("deadline reached before all candidates were attempted")

    monkeypatch.setattr(lxapp, "resolve_and_probe", incomplete)
    url = "/api/v1/track/url?id=lx:kw:119&quality=lossless"
    first = test_app_client.get(url)
    second = test_app_client.get(url)

    assert first.status_code == 504
    assert first.json()["category"] == "resolution_incomplete"
    assert second.status_code == 504
    assert len(calls) == 2
    assert lxapp._URL_CACHE == {}
