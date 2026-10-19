"""Deterministic LLM-liked contracts; all network and clocks are controlled."""
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

import httpx
import pytest

from proxy import localrecommend as lr
from proxy import recommend as rec
from proxy.tests.test_recommend import _make_local_db


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = tmp_path / "music.db"
    _make_local_db(db, n_tracks=150)
    for key, value in {
        "FNMUSIC_RECOMMEND_DIR": str(tmp_path / "cache"),
        "FNMUSIC_PLAY_HISTORY_DIR": str(tmp_path / "history"),
        "FNMUSIC_MUSIC_DB": str(db),
        "FNMUSIC_LLM_BASE_URL": "https://model.invalid/v1",
        "FNMUSIC_LLM_API_KEY": "secret",
        "FNMUSIC_LLM_LIKED_ENABLED": "true",
        "FNMUSIC_LIKED_SIZE": "20",
        "FNMUSIC_LIKED_REFRESH_HOURS": "0.5",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("FNMUSIC_MUSICDL_ENABLED", "FNMUSIC_NETEASE_ENABLED", "FNMUSIC_LX_ENABLED",
                "FNMUSIC_ONLINE_SOURCES", "LX_SOURCES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(lr, "_BUILD_TOKENS", {})
    monkeypatch.setattr(lr, "_GENERATION", 0)
    clock = [100000.0]
    monkeypatch.setattr(lr.time, "time", lambda: clock[0])

    async def model(client, prompt):
        groups = prompt_groups(prompt)
        return {segment: list(reversed(ids)) for segment, ids in groups.items()}

    monkeypatch.setattr(rec, "call_llm_json", model)
    return db, clock


def prompt_groups(prompt):
    groups = {"preference": [], "new": [], "explore": []}
    for line in prompt.splitlines():
        if line.startswith('{"id":'):
            candidate = json.loads(line)
            groups[candidate["segment"]].append(candidate["id"])
    return groups


def edit(db, sql, parameters=()):
    with sqlite3.connect(db) as con:
        con.execute(sql, parameters)


def online(item):
    return {"guid": f"online:{item['id']}", "title": item["title"], "artist": item["artist"]}


async def build(setup, **kwargs):
    db, _ = setup
    return await lr.build_llm_bundle("u1", str(db), None, online, **kwargs)


def inject_items(monkeypatch, count=30, extra=None):
    items = [{"id": f"netease:{i}", "source": "netease", "title": f"New {i}",
              "artist": f"Singer {i}", "cover_url": f"https://cover.invalid/{i}",
              "duration_s": 213.5, "ext": "flac", "needs_vip": True}
             for i in range(count)]
    items.extend(extra or [])

    async def fetch(*args):
        return items

    monkeypatch.setattr(lr, "_fetch_new_candidates", fetch)
    return items


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 1, 2, 4, 5, 7, 19, 20, 21, 99, 100])
async def test_sizes_and_reader_roundtrip(setup, monkeypatch, size):
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", str(size))
    result = await build(setup)
    assert len(result["tracks"]) == size
    if size:
        assert lr.load_llm_bundle("u1", db_path=str(setup[0])) == result
        assert await lr.get_bundle("u1", str(setup[0])) == result
    else:
        assert not Path(lr._liked_path("u1")).exists()
        assert lr.load_llm_bundle("u1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1, 7, 20, 100])
@pytest.mark.parametrize("available", [19, 20])
async def test_threshold_is_independent_of_target(setup, monkeypatch, size, available):
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", str(size))
    edit(setup[0], "UPDATE track SET is_admin_deleted = 1 WHERE id > ?", (available,))
    calls = []

    async def model(*args):
        calls.append(True)
        return None

    monkeypatch.setattr(rec, "call_llm_json", model)
    result = await build(setup)
    assert len(result["tracks"]) == (min(size, available) if available >= 20 else 0)
    assert bool(calls) == (available >= 20)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("FNMUSIC_LLM_LIKED_ENABLED", "false"),
    ("FNMUSIC_LLM_API_KEY", ""), ("FNMUSIC_LLM_BASE_URL", ""),
])
async def test_explicit_gate_never_calls_model(setup, monkeypatch, key, value):
    monkeypatch.setenv(key, value)

    async def forbidden(*args):
        pytest.fail("disabled LLM called")

    monkeypatch.setattr(rec, "call_llm_json", forbidden)
    assert not (await build(setup))["tracks"]
    local = await lr.get_bundle("u1", str(setup[0]))
    assert len(local["tracks"]) == 20
    assert not local.get("configFingerprint")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [5, 7, 20, 21, 100])
async def test_soft_floor_quotas_and_real_model_choices(setup, monkeypatch, size):
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", str(size))
    inject_items(monkeypatch)
    prompts = []

    async def model(client, prompt):
        groups = prompt_groups(prompt)
        prompts.append(groups)
        # Specific alternatives late in the pool must actually win, not just reorder a fixed set.
        return {"preference": groups["preference"][-(size * 3 // 5):],
                "new": groups["new"][-(size // 5):],
                "explore": [i for i in groups["explore"]
                            if i not in groups["preference"][-(size * 3 // 5):]]}

    monkeypatch.setattr(rec, "call_llm_json", model)
    result = await build(setup, netease_enabled=True, verify_playable=lambda track: True)
    dimensions = Counter(track["recommendDimension"] for track in result["tracks"])
    assert len(result["tracks"]) == size
    assert dimensions["new"] == size // 5
    assert dimensions["explore"] == size // 5
    assert dimensions["preference"] == size - 2 * (size // 5)
    assert len(prompts[0]["preference"]) > size * 3 // 5
    wanted = prompts[0]["preference"][-(size * 3 // 5):]
    assert [track["guid"] for track in result["tracks"][:size * 3 // 5]] == [i[6:] for i in wanted]
    for track in result["tracks"]:
        if track["guid"].startswith("online:"):
            assert track["cover_url"].startswith("https://cover.invalid/")
            assert track["duration_s"] == 213.5
            assert track["needs_vip"] is True


@pytest.mark.asyncio
async def test_new_shortfall_backfills_without_duplicates(setup, monkeypatch):
    inject_items(monkeypatch, count=1)
    result = await build(setup, netease_enabled=True, verify_playable=lambda track: True)
    assert len(result["tracks"]) == 20
    assert Counter(t["recommendDimension"] for t in result["tracks"])["new"] == 1
    assert len({t["guid"] for t in result["tracks"]}) == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, {}, {"new": ["new:netease:invented"]}, "bad", RuntimeError("down")])
async def test_model_failure_uses_local_fallback(setup, monkeypatch, failure):
    inject_items(monkeypatch)

    async def model(*args):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(rec, "call_llm_json", model)
    result = await build(setup, netease_enabled=True, verify_playable=lambda track: True)
    assert len(result["tracks"]) == 20
    assert all(not t["guid"].startswith("online:") for t in result["tracks"])


@pytest.mark.asyncio
async def test_bad_candidates_dedup_source_build_and_playability(setup, monkeypatch):
    extras = [
        {"id": "musicdl:bad", "title": "Forbidden", "artist": "A"},
        {"id": "netease:duplicate", "title": "New 0", "artist": "Singer 0"},
        {"id": "netease:local", "title": "本地歌1", "artist": "歌手1"},
        {"id": "netease:favorite", "title": "My Favorite", "artist": "A"},
        {"id": "lx:kw:one", "title": "Not permitted", "artist": "A"},
        {"id": "netease:wrong-build", "title": "Wrong build", "artist": "A"},
    ]
    inject_items(monkeypatch, count=12, extra=extras)
    seen = []

    def builder(item):
        if item["id"].endswith("wrong-build"):
            return {"guid": "online:musicdl:smuggled"}
        if item["id"] == "netease:1":
            raise ValueError("invalid build")
        return online(item)

    async def verify(track):
        seen.append(track["guid"])
        return track["guid"] != "online:netease:2"

    result = await lr.build_llm_bundle(
        "u1", str(setup[0]), None, builder, netease_enabled=True,
        lx_enabled=True, lx_sources=["kg"], verify_playable=verify,
        source_enabled=lambda track: track["guid"] != "online:netease:3",
        online_favorites=lambda user: [{"guid": "online:netease:favorite",
                                       "track": {"title": "My Favorite", "artist": "A"}}],
    )
    guids = {track["guid"] for track in result["tracks"]}
    assert len(guids) == 20
    assert not guids.intersection({"online:netease:1", "online:netease:2", "online:netease:3",
                                  "online:netease:favorite", "online:netease:local",
                                  "online:lx:kw:one", "online:musicdl:smuggled"})
    assert "online:netease:duplicate" not in seen
    assert "online:netease:3" not in seen


@pytest.mark.asyncio
async def test_default_playable_verifier_is_used(setup, monkeypatch):
    inject_items(monkeypatch, count=10)
    client = object()
    seen = []

    async def verify(track, musicdl, musicbox, lx):
        assert musicdl is None and musicbox is client and lx is None
        seen.append(track["guid"])
        return False

    monkeypatch.setattr(rec, "verify_track_playable", verify)
    result = await build(setup, musicbox_client=client, netease_enabled=True)
    assert len(seen) == 10
    assert len(result["tracks"]) == 20
    assert all(not track["guid"].startswith("online:") for track in result["tracks"])


@pytest.mark.asyncio
async def test_profiles_include_local_history_favorites_and_optional_online(setup, monkeypatch):
    edit(setup[0], "INSERT INTO favorite_track(user_id,track_id) VALUES(1,1)")
    edit(setup[0], "INSERT INTO play_history(user_id,track_id,play_count,updated_at) VALUES(1,2,5,100000)")
    captured = []
    original = lr.build_profile

    def profile(rows, seeds, now):
        result = original(rows, seeds, now)
        captured.append(result)
        return result

    async def favorites(user):
        assert user == "u1"
        return [{"track": {"title": "Favorite", "artist": "Online Favorite"}}]

    monkeypatch.setattr(lr, "build_profile", profile)
    result = await build(setup, online_favorites=favorites,
                         online_history=[{"title": "History", "artist": "Online History", "playedAt": 99999}])
    assert {"歌手1", "歌手2", "online favorite", "online history"} <= set(captured[0]["artist"])
    assert "lt1" not in {track["guid"] for track in result["tracks"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("board_key", ["id", "bangid", "both", "prefixed_id"])
async def test_dynamic_lx_new_charts_live_permitted_and_canonical(setup, monkeypatch, board_key):
    requests = []
    canonical = []
    original = rec._lx_recommend_item

    def normalize(row):
        canonical.append(row)
        return original(row)

    monkeypatch.setattr(rec, "_lx_recommend_item", normalize)

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True, "sources": ["kw", "kg", "tx", "wy"],
                "charts": ["kw", "kg", "tx", "wy"], "capabilities": {
                    "kw": {"playback_available": False}, "kg": {"playback_available": True},
                    "tx": {"playback_available": True}, "wy": {"playback_available": True}}})
        assert request.url.params["source"] in ("kg", "tx")
        if path.endswith("categories"):
            source = request.url.params["source"]
            board = {"name": "新歌榜"}
            if board_key == "both":
                board.update(id=f"{source}__wrong-display-id", bangid="new/board")
            elif board_key == "prefixed_id":
                board["id"] = f"{source}__new/board"
            else:
                board[board_key] = "new/board"
            return httpx.Response(200, json={"tags": [board, {"id": "hot", "name": "热歌榜"}]})
        assert path.endswith("/new/board/tracks")
        source = request.url.params["source"]
        return httpx.Response(200, json={"data": [{"id": f"{source}:song", "title": f"New {source}",
            "artist": "A", "duration_s": 123, "cover_url": "https://cover", "ext": "flac", "custom": "kept"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lx") as client:
        items = await lr._fetch_new_candidates(None, False, client, True, ["kw", "kg", "tx"])
        assert {item["id"] for item in items} == {"lx:kg:song", "lx:tx:song"}
        assert all(item["source"] == "lx" and item["custom"] == "kept" and item["duration_s"] == 123 for item in items)
        assert len(canonical) == 2
        requests.clear()
        assert await lr._fetch_new_candidates(None, False, client, True, []) == []
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_netease_new_chart_and_lx_failure_isolated(setup):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/healthz":
            raise httpx.ConnectError("unavailable")
        assert request.url.path == "/api/v1/toplist"
        assert request.url.params["index"] == "1"
        return httpx.Response(200, json={"ok": True, "data": [{"id": "n1", "name": "New",
            "artist": "Singer", "album_pic_url": "https://cover", "duration_ms": 120000}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://service") as client:
        items = await lr._fetch_new_candidates(client, True, client, True, None)
    assert len(items) == 1 and items[0]["id"] == "netease:n1"
    assert items[0]["cover_url"] == "https://cover"


@pytest.mark.asyncio
async def test_persistent_first_fetch_refresh_retry_and_stale_ack(setup, monkeypatch):
    db, clock = setup
    first = await build(setup)
    fingerprint = lr.liked_config_fingerprint()
    clock[0] += 100000
    assert not lr.llm_build_due("u1", fingerprint, clock[0])  # never fetched, no refresh
    assert not lr.mark_llm_bundle_fetched("u1")
    assert lr.mark_llm_bundle_fetched("u1", first["batchId"])
    fetched = clock[0]
    clock[0] += 1799
    assert not lr.mark_llm_bundle_fetched("u1", first["batchId"])
    assert not lr.llm_build_due("u1", fingerprint, clock[0])
    clock[0] += 1
    assert lr.llm_build_due("u1", fingerprint, clock[0])
    lr.record_llm_failure("u1", clock[0], fingerprint)
    lr._LOCKS.clear()  # process memory is irrelevant to persisted deadlines
    assert not lr.llm_build_due("u1", fingerprint, clock[0] + 299)
    assert lr.llm_build_due("u1", fingerprint, clock[0] + 300)
    second = await build(setup)
    assert second["batchId"] != first["batchId"]
    assert not Path(lr._liked_state_path("u1")).exists()
    assert not lr.mark_llm_bundle_fetched("u1", first["batchId"])
    assert lr.load_llm_bundle("u1")["firstFetchedAt"] is None
    assert lr.mark_llm_bundle_fetched("u1", second["batchId"], now=0)
    assert lr.load_llm_bundle("u1")["firstFetchedAt"] == 0
    assert lr.llm_build_due("u1", fingerprint, fetched)
    assert Path(lr._liked_path("u1")).parent.name == hashlib.sha256(b"u1").hexdigest()
    assert "secret" not in Path(lr._liked_path("u1")).read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("FNMUSIC_LLM_MODEL", "other"), ("FNMUSIC_LLM_API_KEY", "other-key"),
    ("FNMUSIC_LLM_BASE_URL", "https://other.invalid"), ("FNMUSIC_LIKED_SIZE", "7"),
    ("LX_SOURCES", "kw"), ("FNMUSIC_NETEASE_ENABLED", "true"),
])
async def test_fingerprint_changes_invalidate_reader_and_old_retry(setup, monkeypatch, key, value):
    await build(setup)
    old = lr.liked_config_fingerprint()
    lr.record_llm_failure("u1", setup[1][0], old)
    monkeypatch.setenv(key, value)
    assert lr.liked_config_fingerprint() != old
    assert lr.load_llm_bundle("u1") is None
    assert lr.llm_build_due("u1", lr.liked_config_fingerprint(), setup[1][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["favorite", "deleted", "threshold", "account"])
async def test_reads_revalidate_local_rows_and_independent_threshold(setup, monkeypatch, mutation):
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", "1")
    result = await build(setup)
    guid = result["tracks"][0]["guid"]
    if mutation == "favorite":
        edit(setup[0], "INSERT INTO favorite_track(user_id,track_id) SELECT 1,id FROM track WHERE guid=?", (guid,))
    elif mutation == "deleted":
        edit(setup[0], "UPDATE audio_file SET is_physical_file_deleted=1 WHERE id=(SELECT audio_file_id FROM track WHERE guid=?)", (guid,))
    elif mutation == "threshold":
        edit(setup[0], "UPDATE track SET is_admin_deleted=1 WHERE guid!=? AND id>1", (guid,))
    else:
        edit(setup[0], "DELETE FROM user WHERE guid='u1'")
    assert lr.load_llm_bundle("u1", db_path=str(setup[0])) is None
    assert not Path(lr._liked_path("u1")).exists()
    if mutation in ("threshold", "account"):
        assert not lr.llm_build_due("u1", lr.liked_config_fingerprint(), setup[1][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "zero", "size", "source", "model", "epoch", "invalidate", "account", "threshold"])
async def test_publication_rejects_state_changed_during_await(setup, monkeypatch, change):
    generation = [1]
    original_source = lr.current_source_fingerprint()

    async def model(*args):
        if change == "disabled":
            monkeypatch.setenv("FNMUSIC_LLM_LIKED_ENABLED", "false")
        elif change == "zero":
            monkeypatch.setenv("FNMUSIC_LIKED_SIZE", "0")
        elif change == "size":
            monkeypatch.setenv("FNMUSIC_LIKED_SIZE", "7")
        elif change == "source":
            monkeypatch.setenv("LX_SOURCES", "kw")
        elif change == "model":
            monkeypatch.setenv("FNMUSIC_LLM_MODEL", "other")
        elif change == "epoch":
            generation[0] += 1
        elif change == "invalidate":
            lr.invalidate_llm_bundles()
        elif change == "account":
            edit(setup[0], "DELETE FROM user WHERE guid='u1'")
        elif change == "threshold":
            edit(setup[0], "UPDATE track SET is_admin_deleted=1 WHERE id>19")
        return {"preference": ["local:lt1"]}

    monkeypatch.setattr(rec, "call_llm_json", model)
    result = await build(setup, source_fingerprint=original_source,
                         publication_guard=lambda: generation[0] == 1)
    assert not result["tracks"]
    assert not Path(lr._liked_path("u1")).exists()


@pytest.mark.asyncio
async def test_app_live_source_callback_rejects_custom_fingerprint_change(setup, monkeypatch):
    live = ["initial"]

    async def model(*args):
        live[0] = "replacement"
        return ["local:lt1"]

    monkeypatch.setattr(rec, "call_llm_json", model)
    assert not (await build(setup, source_fingerprint="initial",
                            get_source_fingerprint=lambda: live[0]))["tracks"]


@pytest.mark.asyncio
async def test_local_favorited_during_model_is_replaced_not_published(setup, monkeypatch):
    chosen = []

    async def model(client, prompt):
        first, second = prompt_groups(prompt)["preference"][:2]
        chosen.extend([first[6:], second[6:]])
        edit(setup[0], "INSERT INTO favorite_track(user_id,track_id) SELECT 1,id FROM track WHERE guid=?", (chosen[0],))
        edit(setup[0], "UPDATE track SET title='Updated title' WHERE guid=?", (chosen[1],))
        return {"preference": [first, second, "unknown", second]}

    monkeypatch.setattr(rec, "call_llm_json", model)
    result = await build(setup)
    assert len(result["tracks"]) == 20
    assert chosen[0] not in {t["guid"] for t in result["tracks"]}
    assert result["tracks"][0]["guid"] == chosen[1]
    assert result["tracks"][0]["title"] == "Updated title"


@pytest.mark.asyncio
async def test_overlapping_old_builder_cannot_replace_new_batch(setup, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = [0]

    async def model(*args):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            await release.wait()
        return ["local:lt1"]

    monkeypatch.setattr(rec, "call_llm_json", model)
    older = asyncio.create_task(build(setup))
    await entered.wait()
    newer = await build(setup)
    release.set()
    assert not (await older)["tracks"]
    assert lr.load_llm_bundle("u1") == newer


@pytest.mark.asyncio
async def test_cancelled_builder_never_publishes(setup, monkeypatch):
    entered = asyncio.Event()

    async def model(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(rec, "call_llm_json", model)
    task = asyncio.create_task(build(setup))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not Path(lr._liked_path("u1")).exists()


@pytest.mark.asyncio
async def test_engines_never_interpret_each_others_cache(setup, monkeypatch):
    local = lr._load_or_build("u1", str(setup[0]))
    assert local["tracks"]
    assert lr.load_llm_bundle("u1") is None
    llm = await build(setup)
    assert llm["engine"] == lr._LLM_ENGINE
    monkeypatch.setenv("FNMUSIC_LLM_LIKED_ENABLED", "false")
    rebuilt = lr._load_or_build("u1", str(setup[0]))
    assert not rebuilt.get("batchId")
    assert not rebuilt.get("configFingerprint")
    monkeypatch.setenv("FNMUSIC_LLM_LIKED_ENABLED", "true")
    assert lr.load_llm_bundle("u1") is None


@pytest.mark.asyncio
async def test_atomic_failure_keeps_previous_batch(setup, monkeypatch):
    first = await build(setup)
    original = rec.os.replace

    def fail(source, destination):
        if destination == lr._liked_path("u1"):
            raise OSError("full disk")
        return original(source, destination)

    monkeypatch.setattr(rec.os, "replace", fail)
    assert not (await build(setup))["tracks"]
    assert lr.load_llm_bundle("u1") == first
    assert not list(Path(lr._liked_path("u1")).parent.glob("*.part"))


@pytest.mark.asyncio
async def test_all_database_connections_are_read_only(setup, monkeypatch):
    original = sqlite3.connect
    connections = []

    def connect(path, **kwargs):
        assert path.endswith("?mode=ro") and kwargs["uri"] is True
        connections.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(lr.sqlite3, "connect", connect)
    result = await build(setup)
    assert lr.load_llm_bundle("u1") == result
    assert lr.mark_llm_bundle_fetched("u1", result["batchId"])
    assert not lr.llm_build_due("u1", lr.liked_config_fingerprint(), setup[1][0])
    assert lr.list_user_guids(str(setup[0])) == ["u1", "u2"]
    assert len(connections) >= 7


@pytest.mark.asyncio
async def test_unknown_shared_accounts_and_account_isolation(setup):
    for user in ("", "shared", "unknown"):
        assert not (await lr.build_llm_bundle(user, str(setup[0]), None, online))["tracks"]
        assert lr.load_llm_bundle(user) is None
    first = await build(setup)
    second = await lr.build_llm_bundle("u2", str(setup[0]), None, online)
    assert first["guid"] != second["guid"]
    assert lr.load_llm_bundle("u1") == first
    assert lr.load_llm_bundle("u2") == second


@pytest.mark.asyncio
async def test_batch_and_retry_survive_fresh_python_process(setup):
    result = await build(setup)
    assert lr.mark_llm_bundle_fetched("u1", result["batchId"])
    lr.record_llm_failure("u1", setup[1][0])
    script = """
import json
from proxy import localrecommend as lr
bundle = lr.load_llm_bundle('u1')
print(json.dumps({'batch': bundle['batchId'], 'first': bundle['firstFetchedAt'],
                  'retry': lr.llm_build_due('u1', lr.liked_config_fingerprint(), 100001),
                  'due': lr.llm_build_due('u1', lr.liked_config_fingerprint(), 101800)}))
"""
    output = subprocess.check_output([sys.executable, "-c", script],
                                     cwd=str(Path(__file__).resolve().parents[2]), text=True)
    state = json.loads(output)
    assert state == {"batch": result["batchId"], "first": setup[1][0], "retry": False, "due": True}


@pytest.mark.asyncio
async def test_unknown_model_ids_and_duplicates_do_not_invent_tracks(setup, monkeypatch):
    groups_seen = []

    async def model(client, prompt):
        groups = prompt_groups(prompt)
        groups_seen.append(groups)
        chosen = groups["preference"][-1]
        return {"preference": ["local:imaginary", chosen, chosen, {"id": chosen}, None],
                "explore": ["new:invented"], "new": ["new:invented"]}

    monkeypatch.setattr(rec, "call_llm_json", model)
    result = await build(setup)
    assert len(result["tracks"]) == len({track["guid"] for track in result["tracks"]}) == 20
    assert result["tracks"][0]["guid"] == groups_seen[0]["preference"][-1][6:]
    assert all(track["guid"].startswith("lt") for track in result["tracks"])


@pytest.mark.asyncio
async def test_quotes_in_real_candidate_metadata_are_valid_json_lines(setup, monkeypatch):
    edit(setup[0], "UPDATE track SET title=?", ('A "quoted" title\\nwith newline',))
    seen = []

    async def model(client, prompt):
        groups = prompt_groups(prompt)
        seen.append(groups)
        return {"preference": groups["preference"]}

    monkeypatch.setattr(rec, "call_llm_json", model)
    result = await build(setup)
    assert seen[0]["preference"]
    assert result["tracks"]


@pytest.mark.asyncio
async def test_disabled_after_online_validation_never_publishes(setup, monkeypatch):
    inject_items(monkeypatch, count=1)
    live = [True]

    async def model(client, prompt):
        live[0] = False
        return {"preference": prompt_groups(prompt)["preference"], "new": ["new:netease:0"]}

    monkeypatch.setattr(rec, "call_llm_json", model)
    result = await build(setup, netease_enabled=True, verify_playable=lambda track: True,
                         source_enabled=lambda track: live[0])
    assert len(result["tracks"]) == 20
    assert all(not track["guid"].startswith("online:") for track in result["tracks"])


@pytest.mark.asyncio
async def test_source_field_cannot_smuggle_musicdl_and_bad_metadata_falls_back(setup, monkeypatch):
    inject_items(monkeypatch, count=0, extra=[
        {"id": "netease:smuggled", "source": "musicdl", "title": "External", "artist": "A"},
        {"id": "netease:renamed", "source": "netease", "title": "Renamed", "artist": "A"},
        {"id": "netease:blank", "source": "netease", "title": "Blanked", "artist": "A"},
    ])
    verified = []

    def builder(item):
        track = online(item)
        if item["id"] == "netease:renamed":
            track.update(title="本地歌1", artist="歌手1")
        elif item["id"] == "netease:blank":
            track["title"] = ""
        return track

    def verify(track):
        verified.append(track)
        return True

    result = await lr.build_llm_bundle("u1", str(setup[0]), None, builder,
                                       netease_enabled=True, verify_playable=verify)
    assert len(result["tracks"]) == 20
    assert not verified


@pytest.mark.asyncio
async def test_twenty_gate_counts_nonfavorites_per_account(setup, monkeypatch):
    monkeypatch.setenv("FNMUSIC_LIKED_SIZE", "100")
    edit(setup[0], "UPDATE track SET is_admin_deleted=1 WHERE id>21")
    edit(setup[0], "INSERT INTO favorite_track(user_id,track_id) VALUES(1,1)")
    edit(setup[0], "INSERT INTO favorite_track(user_id,track_id) VALUES(1,2)")
    assert not (await build(setup))["tracks"]
    other = await lr.build_llm_bundle("u2", str(setup[0]), None, online)
    assert len(other["tracks"]) == 21
    edit(setup[0], "DELETE FROM favorite_track WHERE track_id=2")
    assert len((await build(setup))["tracks"]) == 20


@pytest.mark.asyncio
async def test_ineligible_account_invalidates_even_stale_fingerprint_cache(setup, monkeypatch):
    await build(setup)
    lr.record_llm_failure("u1", setup[1][0])
    monkeypatch.setenv("FNMUSIC_LLM_MODEL", "replacement")
    edit(setup[0], "DELETE FROM user WHERE guid='u1'")
    assert lr.load_llm_bundle("u1") is None
    assert not Path(lr._liked_path("u1")).exists()
    assert not Path(lr._liked_state_path("u1")).exists()


@pytest.mark.asyncio
async def test_real_builder_preserves_canonical_metadata(setup, monkeypatch):
    from proxy.app import build_online_track

    inject_items(monkeypatch, count=8)
    result = await lr.build_llm_bundle("u1", str(setup[0]), None, build_online_track,
                                       netease_enabled=True, verify_playable=lambda track: True)
    selected = [track for track in result["tracks"] if track["guid"].startswith("online:")]
    assert len(selected) == 4
    assert all(track["album"]["coverId"] == track["guid"] and track["audioSpec"]["duration"] == 213500
               and track["needs_vip"] and track["cover_url"] for track in selected)
