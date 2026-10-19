"""Passive LLM liked integration: scheduling, reload boundaries and consumption."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from proxy import app as pa
from proxy import localrecommend as localrec
from proxy import recommend as rec
from proxy.tests.test_recommend import _make_local_db


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CONF", dict(pa.CONF))
    monkeypatch.setattr(pa, "_LIKED_TASKS", {})
    monkeypatch.setattr(pa, "_LIKED_BUILD_LOCK", None)
    monkeypatch.setattr(pa, "_LIKED_WAKEUP", None)
    monkeypatch.setattr(pa, "_LIKED_EPOCH", 0)
    monkeypatch.setattr(pa, "_DAILY_TASKS", {})
    monkeypatch.setattr(pa, "_DAILY_EPOCHS", {"daily": 0, "hot": 0})
    monkeypatch.setattr(pa, "_EMPTY_BUILD_COOLDOWN", {})
    monkeypatch.setenv("FNMUSIC_RECOMMEND_DIR", str(tmp_path / "recommend"))
    monkeypatch.setenv("FNMUSIC_PLAY_HISTORY_DIR", str(tmp_path / "history"))
    for key, value in {
        "FNMUSIC_LLM_BASE_URL": "https://llm.invalid/v1",
        "FNMUSIC_LLM_API_KEY": "secret",
        "FNMUSIC_LLM_MODEL": "test-model",
        "FNMUSIC_LLM_DAILY_ENABLED": "false",
        "FNMUSIC_LLM_LIKED_ENABLED": "true",
        "FNMUSIC_LIKED_SIZE": "20",
        "FNMUSIC_LIKED_REFRESH_HOURS": "0.5",
    }.items():
        monkeypatch.setenv(key, value)
    pa.CONF.update(llm_base_url=rec.llm_base_url(), llm_model=rec.llm_model(),
                   llm_daily_enabled=False, llm_liked_enabled=True,
                   liked_size=20, liked_refresh_hours=0.5,
                   musicdl_enabled=False, netease_enabled=False, lx_enabled=False,
                   recommend_daily=False, recommend_hot=False,
                   netease_my_playlists=False, public_playlists_enabled=False)
    for name in ("get_llm_client", "get_musicbox_client", "get_lx_client"):
        monkeypatch.setattr(pa, name, lambda _app: None)


def request(path="/music/api/v1/playlist/detail", query=""):
    return Request({"type": "http", "method": "GET", "path": path,
                    "query_string": query.encode(), "headers": [],
                    "app": pa.app})


def liked_bundle(batch="batch-a"):
    guid = rec.recommend_playlist_guid("liked", user_guid="u1")
    return {"guid": guid, "batchId": batch, "tracks": [{"guid": "local-1", "title": "one"}],
            "playlist": {"guid": guid, "name": "猜你喜欢"}}


def test_discovery_off_loop_deduplicates_and_serializes_accounts(monkeypatch):
    main_thread = threading.get_ident()
    discoveries = []
    rows = [{"guid": str(i), "valid": True, "favorite": False} for i in range(20)]

    def users(_db):
        discoveries.append(threading.get_ident())
        return ["u1", "u2", "u1", "shared", "", "too-small"]

    monkeypatch.setattr(localrec, "list_user_guids", users)
    monkeypatch.setattr(localrec, "read_library", lambda _db, user: rows[:19] if user == "too-small" else rows)
    monkeypatch.setattr(localrec, "llm_build_due", lambda *_args, **_kwargs: True)

    async def scenario():
        entered = []
        release = asyncio.Event()
        first = asyncio.Event()
        active = 0
        max_active = 0

        async def build(**kwargs):
            nonlocal active, max_active
            assert kwargs["publication_guard"]()
            active += 1
            max_active = max(active, max_active)
            entered.append(kwargs["user_guid"])
            first.set()
            await release.wait()
            active -= 1
            return liked_bundle()

        monkeypatch.setattr(localrec, "build_llm_bundle", build)
        await pa._schedule_liked_builds(pa.app)
        await asyncio.wait_for(first.wait(), 2)
        assert set(pa._LIKED_TASKS) == {"u1", "u2"}
        original = dict(pa._LIKED_TASKS)
        await pa._schedule_liked_builds(pa.app)
        assert pa._LIKED_TASKS == original
        assert len(entered) == 1
        release.set()
        await asyncio.gather(*original.values())
        assert entered == ["u1", "u2"]
        assert max_active == 1
        assert not pa._LIKED_TASKS

    asyncio.run(scenario())
    assert discoveries and all(thread != main_thread for thread in discoveries)


def test_queued_stale_discovery_cannot_rebuild_fresh_batch(monkeypatch):
    monkeypatch.setattr(pa, "_due_liked_accounts", lambda *_: ["u1"])
    monkeypatch.setattr(localrec, "llm_build_due", lambda *_, **_kwargs: False)
    build = Mock(side_effect=AssertionError("fresh batch must not rebuild"))
    monkeypatch.setattr(localrec, "build_llm_bundle", build)

    async def scenario():
        await pa._schedule_liked_builds(pa.app)
        await asyncio.gather(*list(pa._LIKED_TASKS.values()))
        assert not pa._LIKED_TASKS

    asyncio.run(scenario())
    build.assert_not_called()


def test_config_change_during_discovery_cannot_enqueue_old_epoch(monkeypatch):
    async def scenario():
        started = threading.Event()
        release = threading.Event()

        def due(*_args):
            started.set()
            assert release.wait(3)
            return ["u1"]

        monkeypatch.setattr(pa, "_due_liked_accounts", due)
        monkeypatch.setattr(localrec, "invalidate_llm_bundles", lambda: 0)
        scheduling = asyncio.create_task(pa._schedule_liked_builds(pa.app))
        await asyncio.to_thread(started.wait, 2)
        pa._refresh_recommend_config({"liked_size"})
        release.set()
        await scheduling
        assert not pa._LIKED_TASKS

    asyncio.run(scenario())


def test_cancelled_old_finally_keeps_replacement_and_epoch_rejects_stale(monkeypatch):
    monkeypatch.setattr(localrec, "invalidate_llm_bundles", lambda: 0)
    monkeypatch.setattr(localrec, "llm_build_due", lambda *_, **_kwargs: True)

    async def scenario():
        entered = asyncio.Event()
        finish_old = asyncio.Event()
        validity = []

        async def build(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await finish_old.wait()
                validity.append(kwargs["publication_guard"]())
                return liked_bundle()

        monkeypatch.setattr(localrec, "build_llm_bundle", build)
        fingerprint = localrec.liked_config_fingerprint(pa._liked_source_fingerprint())
        old = asyncio.create_task(pa._run_liked_build(pa.app, "u1", fingerprint, pa._LIKED_EPOCH))
        pa._LIKED_TASKS["u1"] = old
        await entered.wait()
        pa._refresh_recommend_config({"liked_size"})
        replacement = asyncio.create_task(asyncio.Event().wait())
        pa._LIKED_TASKS["u1"] = replacement
        finish_old.set()
        await old
        assert validity == [False]
        assert pa._LIKED_TASKS["u1"] is replacement
        replacement.cancel()
        await asyncio.gather(replacement, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["llm_liked_enabled", "liked_size", "llm_model", "llm_base_url",
                                     "FNMUSIC_LLM_API_KEY", "lx_source_url", "lx_source_list"])
def test_relevant_reload_cancels_invalidates_and_wakes(changed, monkeypatch):
    invalidated = Mock(return_value=0)
    monkeypatch.setattr(localrec, "invalidate_llm_bundles", invalidated)
    monkeypatch.setattr(rec, "invalidate_today_cache_all_users", Mock(return_value=0))
    pending = Mock()
    pa._LIKED_TASKS["u1"] = pending
    wake = Mock()
    monkeypatch.setattr(pa, "_LIKED_WAKEUP", wake)
    pa._refresh_recommend_config({changed})
    assert pa._LIKED_EPOCH == 1
    assert not pa._LIKED_TASKS
    pending.cancel.assert_called_once()
    invalidated.assert_called_once()
    wake.set.assert_called_once()


def test_interval_only_keeps_batch_task_and_epoch(monkeypatch):
    invalidated = Mock()
    monkeypatch.setattr(localrec, "invalidate_llm_bundles", invalidated)
    task = Mock()
    pa._LIKED_TASKS["u1"] = task
    wake = Mock()
    monkeypatch.setattr(pa, "_LIKED_WAKEUP", wake)
    pa._refresh_recommend_config({"liked_refresh_hours"})
    invalidated.assert_not_called()
    task.cancel.assert_not_called()
    assert pa._LIKED_TASKS == {"u1": task}
    assert pa._LIKED_EPOCH == 0
    wake.set.assert_called_once()


def test_api_key_only_reload_and_no_change_save(tmp_path, monkeypatch):
    env = tmp_path / "settings.env"
    env.write_text("FNMUSIC_LLM_API_KEY=new-key\n")
    invalidated = Mock(return_value=0)
    monkeypatch.setattr(localrec, "invalidate_llm_bundles", invalidated)
    old_conf = dict(pa.CONF)
    changes = pa.apply_env_hot_reload(str(env))
    assert changes == ["FNMUSIC_LLM_API_KEY"]
    assert pa.CONF == old_conf
    pa._refresh_recommend_config(changes)
    invalidated.assert_called_once()
    assert os.environ["FNMUSIC_LLM_API_KEY"] == "new-key"
    changes = pa.apply_env_hot_reload(str(env))
    assert changes == []
    pa._refresh_recommend_config(changes)
    invalidated.assert_called_once()


@pytest.mark.parametrize("key,value", [
    ("FNMUSIC_LIKED_SIZE", "bad"), ("FNMUSIC_LIKED_SIZE", "101"),
    ("FNMUSIC_LIKED_SIZE", "-1"), ("FNMUSIC_LIKED_SIZE", "inf"),
    ("FNMUSIC_LIKED_REFRESH_HOURS", "nan"), ("FNMUSIC_LIKED_REFRESH_HOURS", "inf"),
    ("FNMUSIC_LIKED_REFRESH_HOURS", "-inf"), ("FNMUSIC_LIKED_REFRESH_HOURS", "0.1"),
    ("FNMUSIC_LLM_LIKED_ENABLED", "typo"), ("FNMUSIC_SEARCH_TIMEOUT", "nan"),
    ("FNMUSIC_LLM_TIMEOUT_S", "inf"), ("FNMUSIC_REC_SEARCH_CONCURRENCY", "nan"),
    ("FNMUSIC_LIKED_REFRESH_HOURS", "8761"),
])
def test_invalid_reload_does_not_poison_environment(tmp_path, key, value):
    before_env = os.environ.get(key)
    before_conf = dict(pa.CONF)
    env = tmp_path / "settings.env"
    env.write_text(f"{key}={value}\n")
    assert pa.apply_env_hot_reload(str(env)) == []
    assert os.environ.get(key) == before_env
    assert pa.CONF == before_conf


@pytest.mark.parametrize("raw", ["bad", "nan", "inf", "-inf", "1e999"])
def test_invalid_startup_liked_numbers_do_not_crash(raw):
    env = dict(os.environ, FNMUSIC_LIKED_SIZE=raw, FNMUSIC_LIKED_REFRESH_HOURS=raw)
    root = str(Path(pa.__file__).resolve().parents[1])
    env["PYTHONPATH"] = root
    run = subprocess.run([sys.executable, "-c", "from proxy.app import CONF; assert CONF['liked_size'] == 20; assert CONF['liked_refresh_hours'] == 0.5"],
                         cwd=root, env=env, capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr


def test_daily_gate_and_connection_preserve_hot_and_source_slot(monkeypatch):
    day = rec.today_key()
    rec.save_daily_cache("u1", day, {"marker": "daily"}, "daily")
    rec.save_daily_cache("u1", day, {"marker": "hot"}, "hot")
    slot = Path(rec.source_slot_path(day))
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text('{"user":"u1"}')
    daily = Mock()
    daily.done.return_value = False
    hot = Mock()
    hot.done.return_value = False
    pa._DAILY_TASKS.update({f"u1:daily:{day}": daily, f"u1:hot:{day}": hot})
    pa._EMPTY_BUILD_COOLDOWN.update({f"u1:daily:{day}": 1, f"u1:hot:{day}": 1})
    monkeypatch.setattr(localrec, "invalidate_llm_bundles", Mock(return_value=0))
    pa._refresh_recommend_config({"llm_daily_enabled"})
    assert not Path(rec.cache_path("u1", day, "daily")).exists()
    assert Path(rec.cache_path("u1", day, "hot")).exists()
    assert slot.read_text() == '{"user":"u1"}'
    daily.cancel.assert_called_once()
    hot.cancel.assert_not_called()
    assert pa._EMPTY_BUILD_COOLDOWN == {f"u1:hot:{day}": 1}
    assert pa._LIKED_EPOCH == 0
    assert pa._DAILY_EPOCHS == {"daily": 1, "hot": 0}
    rec.save_daily_cache("u1", day, {"marker": "daily-again"}, "daily")
    pa.CONF["llm_daily_enabled"] = True
    pa._refresh_recommend_config({"FNMUSIC_LLM_API_KEY"})
    assert not Path(rec.cache_path("u1", day, "daily")).exists()
    assert slot.exists()
    assert Path(rec.cache_path("u1", day, "hot")).exists()


@pytest.mark.parametrize("phase", ["completion", "emission"])
@pytest.mark.parametrize("change", ["llm_daily_enabled", "llm_model", "lx_source_url"])
def test_daily_reload_rejects_stale_tier_even_when_cancel_races_completion(monkeypatch, phase, change):
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", "true")
    pa.CONF.update(llm_daily_enabled=True, recommend_daily=True)
    monkeypatch.setattr(pa, "get_llm_client", lambda _app: object())
    monkeypatch.setattr(pa, "load_online_favorites", lambda _user: [])
    for name in ("read_local_recent_tracks", "seeds_from_online_history", "read_local_favorite_tracks"):
        monkeypatch.setattr(rec, name, lambda *_: [])
    monkeypatch.setattr(rec, "source_slot_claimed", lambda *_: True)
    writes = Mock()
    fallback = Mock(return_value=[])
    remembered = Mock()
    monkeypatch.setattr(rec, "save_daily_cache", writes)
    monkeypatch.setattr(rec, "read_local_random_tracks", fallback)
    monkeypatch.setattr(rec, "_remember_daily_result", remembered)
    reloaded = []

    def reload():
        if change == "llm_daily_enabled":
            monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", "false")
            pa.CONF[change] = False
        elif change == "llm_model":
            monkeypatch.setenv("FNMUSIC_LLM_MODEL", "replacement")
            pa.CONF[change] = "replacement"
        else:
            pa.CONF[change] = "https://replacement.invalid/source.js"
        pa._refresh_recommend_config({change})
        reloaded.append(True)

    async def llm(*_args):
        return [{"title": "old model candidate"}]

    async def resolve(*_args, **kwargs):
        track = {"guid": "online:lx:kg:old", "title": "stale model track", "artist": "old"}
        if phase == "completion":
            # The child finishes before outer Task.cancel is delivered. Python's
            # wait_for may return this result instead of raising CancelledError.
            asyncio.get_running_loop().call_soon(reload)
        else:
            reload()
            kwargs["on_track"](track)
        return [track]

    monkeypatch.setattr(rec, "call_llm", llm)
    monkeypatch.setattr(rec, "resolve_recommendations", resolve)

    async def scenario():
        task = await pa._ensure_daily_task(request(), "u1", "daily")
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError) or result["tracks"] == []

    asyncio.run(scenario())
    assert reloaded == [True]
    writes.assert_not_called()
    fallback.assert_not_called()
    remembered.assert_not_called()
    assert not pa._DAILY_TASKS


def test_size_zero_and_shared_never_read_or_build(monkeypatch):
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", "0")
    loader = Mock(side_effect=AssertionError("disabled card must not load"))
    monkeypatch.setattr(localrec, "load_llm_bundle", loader)
    assert "liked" not in pa._recommend_injectable_kinds("u1")
    assert asyncio.run(pa._peek_daily_bundle(request(), "u1", "liked"))["tracks"] == []
    assert asyncio.run(pa._schedule_liked_builds(pa.app)) is None
    assert not pa._LIKED_TASKS
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", "20")
    assert asyncio.run(pa._peek_daily_bundle(request(), "shared", "liked"))["tracks"] == []
    loader.assert_not_called()


def test_passive_reads_keep_old_batch_while_build_pending_and_never_mark(monkeypatch):
    bundle = liked_bundle()
    loaded = []

    def load(user, source, **_kwargs):
        loaded.append((user, source))
        return bundle

    monkeypatch.setattr(localrec, "load_llm_bundle", load)
    mark = Mock()
    monkeypatch.setattr(localrec, "mark_llm_bundle_fetched", mark)
    monkeypatch.setattr(localrec, "build_llm_bundle", Mock(side_effect=AssertionError("request must not build")))
    pa._LIKED_TASKS["u1"] = object()
    assert asyncio.run(pa._peek_daily_bundle(request(), "u1", "liked")) is bundle
    assert asyncio.run(pa._load_daily_bundle(request(), "u1", "liked")) is bundle
    assert loaded == [("u1", pa._liked_source_fingerprint())] * 2
    mark.assert_not_called()


@pytest.mark.parametrize("endpoint", ["detail", "batch", "tracks", "card"])
def test_returned_liked_responses_mark_exact_batch(endpoint, monkeypatch):
    bundle = liked_bundle()
    guid = bundle["guid"]
    mark = Mock()
    monkeypatch.setattr(localrec, "mark_llm_bundle_fetched", mark)
    monkeypatch.setattr(localrec, "load_llm_bundle", lambda *_, **_kwargs: bundle)
    monkeypatch.setattr(pa, "get_upstream_client", lambda _app: None)

    async def auth(*_args):
        return True, "u1", None

    async def envelope(*_args):
        return {"code": 0, "data": {"list": [], "total": 0}}

    monkeypatch.setattr(pa, "_probe_upstream_auth", auth)
    monkeypatch.setattr(pa, "fetch_upstream_envelope", envelope)
    if endpoint == "detail":
        response = asyncio.run(pa.playlist_detail(request(query=f"guid={guid}")))
    elif endpoint == "batch":
        response = asyncio.run(pa.playlist_batch_detail(request(query=f"guids={guid}")))
    elif endpoint == "tracks":
        response = asyncio.run(pa.playlist_track_list(request(query=f"guid={guid}")))
    else:
        response = asyncio.run(pa.playlist_list(request()))
    assert response.status_code == 200
    assert json.loads(response.body)["code"] == 0
    mark.assert_called_once_with("u1", "batch-a", source_fingerprint=pa._liked_source_fingerprint(),
                                 db_path=str(pa.CONF.get("music_db") or rec.music_db_path()))


def test_empty_tracks_page_is_not_consumption(monkeypatch):
    bundle = liked_bundle()
    monkeypatch.setattr(localrec, "load_llm_bundle", lambda *_, **_kwargs: bundle)
    mark = Mock()
    monkeypatch.setattr(localrec, "mark_llm_bundle_fetched", mark)
    monkeypatch.setattr(pa, "get_upstream_client", lambda _app: None)

    async def auth(*_):
        return True, "u1", None

    monkeypatch.setattr(pa, "_probe_upstream_auth", auth)
    response = asyncio.run(pa.playlist_track_list(request(query=f"guid={bundle['guid']}&page=99&size=1")))
    assert json.loads(response.body)["data"]["list"] == []
    mark.assert_not_called()


def test_cover_read_does_not_consume_or_schedule(monkeypatch):
    bundle = liked_bundle()
    monkeypatch.setattr(localrec, "load_llm_bundle", lambda *_, **_kwargs: bundle)
    mark = Mock()
    monkeypatch.setattr(localrec, "mark_llm_bundle_fetched", mark)
    monkeypatch.setattr(localrec, "build_llm_bundle", Mock(side_effect=AssertionError("cover must not build")))
    monkeypatch.setattr(pa, "get_upstream_client", lambda _app: None)

    async def auth(*_):
        return True, "u1", None

    async def cover(_request, guid, tracks):
        assert guid == bundle["guid"]
        assert tracks == bundle["tracks"]
        return pa.Response(content=b"image", media_type="image/png")

    monkeypatch.setattr(pa, "_probe_upstream_auth", auth)
    monkeypatch.setattr(pa, "_playlist_cover_response", cover)
    response = asyncio.run(pa.static_cover(request(query=f"guid={bundle['guid']}")))
    assert response.status_code == 200
    mark.assert_not_called()
    assert not pa._LIKED_TASKS


def test_source_fingerprint_matches_conf_and_environment(monkeypatch):
    defaults = dict(pa.CONF, online_sources="KuwoMusicClient,MiguMusicClient", lx_sources=[],
                    lx_source_url="", lx_source_list="[]")
    for env_key, (conf_key, _) in pa._ENV_WATCH_KEYS.items():
        if conf_key in pa._SOURCE_CONF_KEYS:
            monkeypatch.delenv(env_key, raising=False)
    assert localrec.current_source_fingerprint(defaults) == localrec.current_source_fingerprint()
    monkeypatch.setenv("LX_SOURCES", "kugou,wy")
    defaults["lx_sources"] = ["kg", "wy"]
    assert localrec.current_source_fingerprint(defaults) == localrec.current_source_fingerprint()
    previous = localrec.current_source_fingerprint(defaults)
    defaults["lx_source_list"] = '[{"url":"https://script.invalid/new.js","active":true}]'
    assert localrec.current_source_fingerprint(defaults) != previous


def test_startup_restores_persisted_unread_batch_without_requests(tmp_path, monkeypatch):
    db = tmp_path / "music.db"
    _make_local_db(db, n_tracks=30)
    pa.CONF["music_db"] = str(db)
    monkeypatch.setenv("FNMUSIC_MUSIC_DB", str(db))
    model_calls = []

    async def llm(_client, _prompt):
        model_calls.append(True)
        return {"preference": ["local:lt1"]}

    monkeypatch.setattr(rec, "call_llm_json", llm)

    async def scenario():
        await pa._schedule_liked_builds(pa.app)
        await asyncio.gather(*list(pa._LIKED_TASKS.values()))
        first = localrec.load_llm_bundle("u1", pa._liked_source_fingerprint(), db_path=str(db))
        assert first and first["tracks"]
        assert first["firstFetchedAt"] is None
        before = len(model_calls)
        # A newly started scheduler must discover persisted state with no request
        # warmup. Even a very old publication remains unread and never expires.
        monkeypatch.setattr(pa.time, "time", lambda: first["publishedAt"] + 90 * 86400)
        await pa._schedule_liked_builds(pa.app)
        assert not pa._LIKED_TASKS
        assert len(model_calls) == before
        loaded = await pa._peek_daily_bundle(request(), "u1", "liked")
        assert loaded["batchId"] == first["batchId"]
        assert loaded["firstFetchedAt"] is None
        pa._mark_liked_response_fetched("u1", loaded)
        fetched = localrec.load_llm_bundle("u1", pa._liked_source_fingerprint(), db_path=str(db))
        assert fetched["firstFetchedAt"] == first["publishedAt"] + 90 * 86400
        monkeypatch.setattr(pa.time, "time", lambda: fetched["firstFetchedAt"] + 1801)
        await pa._schedule_liked_builds(pa.app)
        assert "u1" in pa._LIKED_TASKS
        await asyncio.gather(*list(pa._LIKED_TASKS.values()))
        refreshed = localrec.load_llm_bundle("u1", pa._liked_source_fingerprint(), db_path=str(db))
        assert refreshed["batchId"] != first["batchId"]
        assert refreshed["firstFetchedAt"] is None

    asyncio.run(scenario())


def test_enabled_reload_wakes_running_scheduler_without_a_request(tmp_path, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_LIKED_ENABLED", "false")
    pa.CONF["llm_liked_enabled"] = False
    env = tmp_path / "settings.env"
    env.write_text("FNMUSIC_LLM_LIKED_ENABLED=true\n")
    monkeypatch.setattr(pa, "_due_liked_accounts", lambda *_: ["u1"])
    monkeypatch.setattr(localrec, "llm_build_due", lambda *_, **_kwargs: True)
    monkeypatch.setattr(localrec, "invalidate_llm_bundles", lambda: 0)

    async def scenario():
        built = asyncio.Event()

        async def build(**kwargs):
            assert kwargs["publication_guard"]()
            built.set()
            return liked_bundle()

        monkeypatch.setattr(localrec, "build_llm_bundle", build)
        scheduler = asyncio.create_task(pa._liked_scheduler_loop(pa.app))
        await asyncio.sleep(0)
        assert not built.is_set()
        changes = pa.apply_env_hot_reload(str(env))
        assert changes == ["llm_liked_enabled"]
        pa._refresh_recommend_config(changes)
        await asyncio.wait_for(built.wait(), 2)
        scheduler.cancel()
        await asyncio.gather(scheduler, *list(pa._LIKED_TASKS.values()), return_exceptions=True)

    asyncio.run(scenario())


def test_lifespan_starts_and_stops_scheduler_even_when_env_watch_off(monkeypatch):
    monkeypatch.setenv("FNMUSIC_BACKGROUND_JOBS", "true")
    pa.CONF["env_watch"] = False
    monkeypatch.setattr(pa, "_sync_recommend_version_marker", lambda: None)
    monkeypatch.setattr(pa, "write_env_snapshot", lambda *_: None)

    async def scenario():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def scheduler(_app):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def idle():
            await asyncio.Event().wait()

        monkeypatch.setattr(pa, "_liked_scheduler_loop", scheduler)
        monkeypatch.setattr(pa, "_lyric_orphan_sweeper", idle)
        monkeypatch.setattr(pa, "_log_retention_loop", idle)
        instance = FastAPI()
        instance.state.upstream_client = object()
        instance.state.musicdl_client = object()
        instance.state.musicbox_client = object()
        instance.state.lx_client = object()
        instance.state.llm_client = object()
        async with pa.lifespan(instance):
            await asyncio.wait_for(started.wait(), 2)
        assert stopped.is_set()
        assert not pa._LIKED_TASKS

    asyncio.run(scenario())
