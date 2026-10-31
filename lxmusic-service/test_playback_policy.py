"""lxmusic-service/test_playback_policy.py

本组（计划 B）新增行为的失败优先测试：
1. 共享七档音质契约的本地兼容层（主智能体最终接入 proxy/quality_policy.py）；
2. 播放顺序：音质 -> 脚本顺序 -> 同脚本原平台/其他平台；
3. 自动切换关闭时只用原平台首个就绪兼容脚本；
4. 稳定脚本 ID 排序、停用保留位置、新源追加、删除清理；
5. 跨平台候选的可信匹配（歌名/歌手/版本/时长）、每平台 3 个候选、
   搜索并发上限 2、60 秒缓存与独立取消域；
6. POST /api/v1/source/reorder 的管理锁、409、上游 sourceIds、失败回滚与缓存代数；
7. #99 媒体格式识别：M4A/AAC/Ogg/WAV 不得硬写 mp3，探活与播放共用请求头，
   响应返回实际来源与实际音质。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import httpx
import pytest

from conftest import lxapp, mock_client
import playback_policy as policy
from lxserver_client import LxServerClient, SourceOperationBusy, management_operation


# ---------------------------------------------------------------- 测试辅助 --


def _script(sid: str, platforms, *, ready: bool = True, name: str = "") -> policy.ScriptInfo:
    return policy.ScriptInfo(
        id=sid,
        name=name or sid,
        ready=ready,
        platforms=tuple(platforms),
        enabled=ready,
        status="success" if ready else "failed",
    )


def _target(**overrides) -> dict:
    item = {
        "id": "lx:kw:5886682",
        "lx_source": "kw",
        "title": "夜曲",
        "artist": "周杰伦",
        "duration_s": 227.0,
    }
    item.update(overrides)
    return item


def _candidate(**overrides) -> dict:
    item = {
        "id": "lx:wy:wy9",
        "lx_source": "wy",
        "title": "夜曲",
        "artist": "周杰伦",
        "duration_s": 227.0,
    }
    item.update(overrides)
    return item


def _flac_response(request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(
        206,
        headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
        content=b"fLaC" + b"\x00" * 4092,
    )


def _mp3_response(request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(
        206,
        headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 0-4099/50000"},
        content=b"\xff\xfb\x90\x44" + b"\x00" * 4096,
    )


def _m4a_response(request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(
        206,
        headers={"Content-Type": "audio/mp4", "Content-Range": "bytes 0-4099/50000"},
        content=b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 4096,
    )


def _write_env(tmp_path: Path, monkeypatch, values: dict[str, str]) -> Path:
    path = tmp_path / "policy.env"
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    monkeypatch.setenv("LX_ENV_PATH", str(path))
    return path


@pytest.fixture
def policy_runtime(tmp_path, monkeypatch):
    """隔离直链缓存、在途、熔断、策略签名、候选缓存与排序持久目录。"""
    monkeypatch.setitem(lxapp.CONF, "data_dir", str(tmp_path / "data"))
    for key in (
        "FNMUSIC_LX_AUTO_SWITCH",
        "FNMUSIC_LX_PLATFORM_PREFERRED",
        "FNMUSIC_LX_SOURCE_ORDER",
        "LX_ENV_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(lxapp, "_POLICY_SIGNATURE", None, raising=False)
    monkeypatch.setattr(lxapp, "_PERSISTED_ENV_CACHE", None, raising=False)
    lxapp._SONG_CACHE.clear()
    lxapp._URL_CACHE.clear()
    lxapp._URL_INFLIGHT.clear()
    lxapp._CHAIN_HEALTH.clear()
    finder = getattr(lxapp, "_CANDIDATES", None)
    if finder is not None:
        finder.invalidate()
    yield
    lxapp._SONG_CACHE.clear()
    lxapp._URL_CACHE.clear()
    lxapp._URL_INFLIGHT.clear()
    lxapp._CHAIN_HEALTH.clear()
    if finder is not None:
        finder.invalidate()


# ------------------------------------------------------- 共享质量契约兼容层 --


def test_quality_contract_matches_shared_module():
    assert tuple(policy.QUALITY_LEVELS) == (
        "standard", "higher", "exhigh", "lossless", "hires", "jymaster", "sky",
    )
    assert policy.quality_ladder("standard") == ["standard"]
    assert policy.quality_ladder("exhigh") == ["exhigh", "higher", "standard"]
    assert policy.quality_ladder("lossless") == ["lossless", "exhigh", "higher", "standard"]
    assert policy.quality_ladder("sky") == [
        "sky", "jymaster", "hires", "lossless", "exhigh", "higher", "standard",
    ]
    assert [policy.to_lx_quality(q) for q in policy.QUALITY_LEVELS] == [
        "128k", "192k", "320k", "flac", "flac24bit", "master", "atmos",
    ]
    # 洛雪旧 high 语义固定为 exhigh（320k），不能误当 192k
    assert policy.normalize_quality("high") == "exhigh"
    assert policy.normalize_quality("320k") == "exhigh"
    assert policy.normalize_quality("flac") == "lossless"
    assert policy.from_lx_quality("flac24bit") == "hires"
    assert policy.from_lx_quality("master") == "jymaster"


# --------------------------------------------------------------- 脚本顺序 --


def test_source_catalog_normalizes_platforms_and_readiness():
    catalog = policy.build_source_catalog([
        {"id": "a.js", "name": "A", "enabled": True, "status": "success",
         "supportedSources": ["kugou", "wy"]},
        {"id": "b.js", "name": "B", "enabled": False, "supportedSources": []},
        {"id": "c.js", "name": "C", "enabled": True, "status": "loading"},
    ])
    assert [s.id for s in catalog] == ["a.js", "b.js", "c.js"]
    assert catalog[0].ready is True
    assert catalog[0].platforms == ("kg", "wy")
    assert catalog[1].ready is False
    assert catalog[1].platforms == tuple(policy.SUPPORTED_PLATFORMS)
    assert catalog[2].ready is False  # 缺失/加载中不能被当成可播


def test_merge_source_order_keeps_disabled_and_appends_new():
    assert policy.merge_source_order(
        ["a.js", "b.js", "c.js", "d.js"],
        ["c.js", "deleted.js", "a.js"],
    ) == ["c.js", "a.js", "b.js", "d.js"]


def test_merge_reorder_preserves_unsubmitted_slots():
    # WebUI 只展示/提交激活脚本；停用脚本的历史位置不能被挤走
    assert policy.merge_reorder(["a.js", "b.js", "c.js"], ["c.js", "b.js"]) == [
        "a.js", "c.js", "b.js",
    ]
    assert policy.merge_reorder(["a.js", "b.js", "c.js"], ["c.js", "b.js", "new.js"]) == [
        "a.js", "c.js", "b.js", "new.js",
    ]


# --------------------------------------------------------------- 播放顺序 --


def test_plan_quality_then_script_then_platform():
    catalog = [
        _script("a.js", ["kw", "wy"]),
        _script("b.js", ["kw", "kg"], ready=False),
        _script("c.js", ["kw"]),
    ]
    scripts = policy.ordered_scripts(catalog, ["c.js", "a.js"])
    steps = policy.plan_steps(
        ["lossless", "exhigh"], scripts, "kw",
        auto_switch=True, platform_preferred=True,
    )
    assert [(s.script_id, s.platform, s.quality) for s in steps] == [
        ("c.js", "kw", "lossless"),
        ("a.js", "kw", "lossless"),
        ("a.js", "wy", "lossless"),
        ("c.js", "kw", "exhigh"),
        ("a.js", "kw", "exhigh"),
        ("a.js", "wy", "exhigh"),
    ]


def test_plan_without_platform_preference_stays_on_original_platform():
    scripts = policy.ordered_scripts([_script("a.js", ["kw", "wy"]), _script("b.js", ["kw"])])
    steps = policy.plan_steps(["standard"], scripts, "kw",
                              auto_switch=True, platform_preferred=False)
    assert [(s.script_id, s.platform) for s in steps] == [("a.js", "kw"), ("b.js", "kw")]


def test_plan_prefers_declared_other_platform_when_original_unsupported():
    scripts = policy.ordered_scripts([_script("a.js", ["wy", "kg"])])
    steps = policy.plan_steps(["standard"], scripts, "kw",
                              auto_switch=True, platform_preferred=True)
    assert [(s.script_id, s.platform) for s in steps] == [("a.js", "wy"), ("a.js", "kg")]


def test_auto_switch_off_uses_first_ready_compatible_script_only():
    catalog = [
        _script("a.js", ["kw"], ready=False),
        _script("b.js", ["wy"]),
        _script("c.js", ["kw"]),
    ]
    scripts = policy.ordered_scripts(catalog)
    steps = policy.plan_steps(["lossless", "exhigh"], scripts, "kw",
                              auto_switch=False, platform_preferred=True)
    assert [(s.script_id, s.platform, s.quality) for s in steps] == [
        ("c.js", "kw", "lossless"),
        ("c.js", "kw", "exhigh"),
    ]


def test_plan_does_not_cap_script_count_at_three():
    scripts = policy.ordered_scripts([_script(f"{i}.js", ["kw"]) for i in range(5)])
    steps = policy.plan_steps(["standard"], scripts, "kw",
                              auto_switch=True, platform_preferred=False)
    assert [s.script_id for s in steps] == [f"{i}.js" for i in range(5)]


# --------------------------------------------------------- 候选可信匹配 --


def test_trusted_candidate_requires_title_artist_version_and_duration():
    target = _target(title="夜曲 (Live)", duration_s=227.0)
    assert policy.is_trusted_candidate(target, _candidate(title="夜曲（现场版）"))
    assert policy.is_trusted_candidate(
        target, _candidate(title="夜曲（现场版）", artist="周杰伦/袁咏琳"))
    assert policy.is_trusted_candidate(
        target, _candidate(title="夜曲（现场版）", artist="周杰伦 feat. 袁咏琳"))
    assert policy.is_trusted_candidate(
        target, _candidate(title="夜曲（现场版）", duration_s=232.0))  # 227*3% < 5s
    assert not policy.is_trusted_candidate(target, _candidate(title="夜曲"))
    assert not policy.is_trusted_candidate(target, _candidate(title="夜曲（伴奏）"))
    assert not policy.is_trusted_candidate(target, _candidate(artist="其他歌手"))
    assert not policy.is_trusted_candidate(target, _candidate(artist=""))
    assert not policy.is_trusted_candidate(target, _candidate(title="夜曲（现场版）", duration_s=240.0))
    # 双方时长已知才比较；未知时长不能据此否定其他身份一致
    assert policy.is_trusted_candidate(target, _candidate(title="夜曲（现场版）", duration_s=0))
    # 缺歌名的曲目不能做模糊替换
    assert not policy.is_trusted_candidate(_target(title="未知曲目"), _candidate(title="夜曲"))


def test_version_tags_normalize_live_and_accompaniment():
    assert policy.version_tags("夜曲 (Live)") == {"live"}
    assert policy.version_tags("夜曲（现场版）") == {"live"}
    assert policy.version_tags("夜曲（伴奏）") == {"instrumental"}
    assert policy.version_tags("夜曲") == set()


def test_duration_threshold_uses_max_five_seconds_or_three_percent():
    assert policy.durations_compatible(300, 309) is True
    assert policy.durations_compatible(300, 311) is False
    assert policy.durations_compatible(100, 105) is True
    assert policy.durations_compatible(0, 300) is True


# --------------------------------------------------------- 候选搜索缓存 --


@pytest.mark.asyncio
async def test_candidate_finder_caps_three_and_caches_for_sixty_seconds():
    calls: list[tuple[str, str, int]] = []
    rows = [
        _candidate(id=f"lx:wy:{i}", songmid=str(i)) for i in range(4)
    ] + [_candidate(artist="其他歌手")]

    async def searcher(platform: str, keyword: str, limit: int) -> list[dict]:
        calls.append((platform, keyword, limit))
        return [dict(row) for row in rows]

    now = [100.0]
    finder = policy.CandidateFinder(ttl=60.0, max_candidates=3, clock=lambda: now[0])
    deadline = now[0] + 5.0

    first = await finder.find(searcher, _target(), "wy", deadline, 0)
    assert len(first) == 3
    assert calls == [("wy", "夜曲", 20)]

    second = await finder.find(searcher, _target(), "wy", deadline, 0)
    assert [c["id"] for c in second] == [c["id"] for c in first]
    assert len(calls) == 1  # 60 秒内命中缓存

    now[0] += 61.0
    await finder.find(searcher, _target(), "wy", now[0] + 5.0, 0)
    assert len(calls) == 2  # 过期后重新搜索

    finder.invalidate()
    await finder.find(searcher, _target(), "wy", now[0] + 5.0, 0)
    assert len(calls) == 3  # 排序/配置变化可主动失效


@pytest.mark.asyncio
async def test_candidate_cache_separates_versions():
    calls: list[tuple[str, str]] = []
    rows = [
        _candidate(id="lx:wy:live", title="夜曲（现场版）"),
        _candidate(id="lx:wy:plain", title="夜曲"),
    ]

    async def searcher(platform: str, keyword: str, limit: int) -> list[dict]:
        calls.append((platform, keyword))
        return [dict(row) for row in rows]

    finder = policy.CandidateFinder()
    deadline = time.monotonic() + 5.0
    live = await finder.find(searcher, _target(title="夜曲 (Live)"), "wy", deadline, 0)
    plain = await finder.find(searcher, _target(title="夜曲"), "wy", deadline, 0)
    assert [item["id"] for item in live] == ["lx:wy:live"]
    assert [item["id"] for item in plain] == ["lx:wy:plain"]
    assert len(calls) == 2  # 现场版与普通版不能共用同一份候选缓存


@pytest.mark.asyncio
async def test_candidate_finder_caps_search_concurrency_at_two():
    active = 0
    peak = 0

    async def searcher(platform: str, keyword: str, limit: int) -> list[dict]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return []

    finder = policy.CandidateFinder(concurrency=2)
    platforms = ["wy", "kg", "tx", "kw", "mg"]
    found = await finder.find_many(
        searcher, _target(), platforms, time.monotonic() + 5.0, 0,
    )
    assert set(found) == set(platforms)
    assert peak <= 2


@pytest.mark.asyncio
async def test_candidate_follower_respects_its_own_deadline():
    started = asyncio.Event()
    release = asyncio.Event()

    async def searcher(platform: str, keyword: str, limit: int) -> list[dict]:
        started.set()
        await release.wait()
        return [_candidate()]

    finder = policy.CandidateFinder()
    leader = asyncio.create_task(
        finder.find(searcher, _target(), "wy", time.monotonic() + 2.0, 0)
    )
    await started.wait()
    begin = time.monotonic()
    follower = await finder.find(
        searcher, _target(), "wy", time.monotonic() + 0.05, 0,
    )
    elapsed = time.monotonic() - begin
    assert follower == []
    assert elapsed < 0.5  # 跟随者不能陪领队等满 2 秒总预算
    release.set()
    assert await leader == [_candidate()]


@pytest.mark.asyncio
async def test_candidate_leader_cancellation_does_not_abort_follower():
    """首个请求取消后，仍有预算的跟随者独立重试，不接收领队异常或负缓存。"""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def searcher(platform, keyword, limit):
        calls.append(platform)
        started.set()
        await release.wait()
        return [_candidate()]

    finder = policy.CandidateFinder()
    deadline = time.monotonic() + 2
    leader = asyncio.create_task(finder.find(searcher, _target(), "wy", deadline))
    await started.wait()
    follower = asyncio.create_task(finder.find(searcher, _target(), "wy", deadline))
    # 让跟随者进入真实的在途等待分支，再取消原请求。
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    release.set()
    assert await follower == [_candidate()]
    assert calls == ["wy", "wy"]
    assert finder._inflight == {}


@pytest.mark.asyncio
async def test_candidate_search_deadline_cancels_without_leaking_inflight():
    started = asyncio.Event()
    release = asyncio.Event()

    async def searcher(platform: str, keyword: str, limit: int) -> list[dict]:
        started.set()
        await release.wait()
        return [_candidate()]

    finder = policy.CandidateFinder()
    deadline = time.monotonic() + 0.05
    result = await finder.find_many(searcher, _target(), ["wy"], deadline, 0)
    assert result == {"wy": []}
    assert finder._inflight == {}
    release.set()
    await asyncio.sleep(0)


# ------------------------------------------------------------- #99 媒体格式 --


@pytest.mark.parametrize(("content_type", "expected"), [
    ("audio/mpeg", "mp3"),
    ("audio/flac", "flac"),
    ("audio/mp4", "m4a"),
    ("audio/x-m4a", "m4a"),
    ("audio/aac", "aac"),
    ("audio/ogg", "ogg"),
    ("audio/wav", "wav"),
    ("application/octet-stream; charset=binary", ""),
])
def test_media_ext_from_content_type(content_type, expected):
    assert policy.media_ext_from_content_type(content_type) == expected


@pytest.mark.parametrize(("url", "expected"), [
    ("https://cdn.test/a.M4A?token=1", "m4a"),
    ("https://cdn.test/a.aac", "aac"),
    ("https://cdn.test/a.ogg", "ogg"),
    ("https://cdn.test/a.wav", "wav"),
    ("https://cdn.test/a.flac", "flac"),
    ("https://cdn.test/a.mp3", "mp3"),
    ("https://cdn.test/stream", ""),
])
def test_media_ext_from_url(url, expected):
    assert policy.media_ext_from_url(url) == expected


def test_track_url_reports_m4a_instead_of_mp3(test_app_client, fake_lx, policy_runtime):
    fake_lx.url_result = {
        "url": "https://media.test/song.m4a", "type": "128k", "sourceName": "test",
    }
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_m4a_response)
    try:
        response = test_app_client.get(
            "/api/v1/track/url?id=lx:kw:5886682&quality=standard",
        )
    finally:
        lxapp.app.state.client = orig_client
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ext"] == "m4a"
    assert data["headers"]["User-Agent"] == lxapp.UA_PC
    # type 只是请求/解析档回显：只能写 resolution_quality，实际档保持未知
    assert data["resolution_quality"] == "standard"
    assert data["requested_quality"] == "standard"
    assert data["resolution_lx_quality"] == "128k"
    assert data["actual_quality"] == ""
    assert data["actual_tier"] == ""
    assert data["source_platform"] == "kw"
    assert data["source_script"] == "source1"
    assert data["quality_evidence"]["probe"] == "m4a"
    assert data["resolved_track_id"] == "lx:kw:5886682"


def test_track_url_does_not_claim_lossless_for_lower_upstream_quality(
    test_app_client, fake_lx, policy_runtime,
):
    fake_lx.url_result = {
        "url": "https://media.test/song.mp3", "type": "128k", "sourceName": "test",
    }
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_mp3_response)
    try:
        response = test_app_client.get(
            "/api/v1/track/url?id=lx:kw:5886682&quality=lossless",
        )
    finally:
        lxapp.app.state.client = orig_client
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ext"] == "mp3"
    assert data["resolution_quality"] == "standard"
    # 上游解析档降低，但实际档没有任何实测证据，不能回填请求/解析标签
    assert data["actual_quality"] == ""
    assert data["actual_tier"] == ""
    assert "lossless" in data["fallback_reason"]
    assert "standard" in data["fallback_reason"]


def test_track_url_probe_and_playback_share_headers(test_app_client, fake_lx, policy_runtime):
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return _flac_response(request)

    fake_lx.url_result = {
        "url": "https://media.test/song.flac", "type": "flac", "sourceName": "test",
    }
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(handler)
    try:
        response = test_app_client.get(
            "/api/v1/track/url?id=lx:kg:abc123&quality=lossless",
        )
    finally:
        lxapp.app.state.client = orig_client
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert seen and seen[0].get("user-agent") == lxapp.UA_PC
    assert seen[0].get("referer") == "https://www.kugou.com/"
    assert data["headers"]["User-Agent"] == seen[0].get("user-agent")
    assert data["headers"]["Referer"] == seen[0].get("referer")


# ---------------------------------------------------- 策略接线与总预算 --


def test_source_info_exposes_policy_and_merges_order(
    test_app_client, fake_lx, policy_runtime, tmp_path, monkeypatch,
):
    fake_lx.sources = [
        {"id": "a.js", "name": "A", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "b.js", "name": "B", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "c.js", "name": "C", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
    ]
    _write_env(tmp_path, monkeypatch, {
        "FNMUSIC_LX_SOURCE_ORDER": json.dumps(["c.js", "a.js"]),
    })
    response = test_app_client.get("/api/v1/source")
    assert response.status_code == 200
    payload = response.json()["policy"]
    assert payload["auto_switch"] is True
    assert payload["platform_preferred"] is False
    # env 只列部分激活脚本时，未被提交的 b.js 保留原槽位，而不是被挤到末尾
    assert payload["source_order"] == ["c.js", "b.js", "a.js"]
    assert payload["active_source_order"] == ["c.js", "b.js", "a.js"]
    assert payload["generation"] == lxapp._URL_CACHE_GEN


def test_track_url_uses_script_order_and_stays_on_target_tier(
    test_app_client, fake_lx, policy_runtime, tmp_path, monkeypatch,
):
    fake_lx.sources = [
        {"id": "a.js", "name": "A", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "b.js", "name": "B", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
    ]
    _write_env(tmp_path, monkeypatch, {
        "FNMUSIC_LX_SOURCE_ORDER": json.dumps(["b.js", "a.js"]),
    })
    fake_lx.url_script = [
        None,
        {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "A",
         "sourceId": "a.js"},
    ]
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_response)
    try:
        response = test_app_client.get(
            "/api/v1/track/url?id=lx:kw:5886682&quality=lossless",
        )
    finally:
        lxapp.app.state.client = orig_client
    assert response.status_code == 200, response.text
    # 首个脚本失败后仍在本档位换脚本，而不是先降到 320k
    assert [quality for _, quality in fake_lx.url_calls] == ["flac", "flac"]
    assert fake_lx.url_song_sources == ["kw", "kw"]
    assert fake_lx.url_excludes[0] == ["a.js"]
    assert fake_lx.url_excludes[1] == ["b.js"]


def test_track_url_auto_switch_off_uses_one_script_and_ladders(
    test_app_client, fake_lx, policy_runtime, tmp_path, monkeypatch,
):
    fake_lx.sources = [
        {"id": "a.js", "name": "A", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "b.js", "name": "B", "enabled": True, "status": "success",
         "supportedSources": ["kw", "wy"]},
    ]
    _write_env(tmp_path, monkeypatch, {
        "FNMUSIC_LX_AUTO_SWITCH": "false",
        "FNMUSIC_LX_PLATFORM_PREFERRED": "true",
    })
    fake_lx.url_script = [
        None,
        {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "A",
         "sourceId": "a.js"},
    ]
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_response)
    try:
        response = test_app_client.get(
            "/api/v1/track/url?id=lx:kw:5886682&quality=lossless",
        )
    finally:
        lxapp.app.state.client = orig_client
    assert response.status_code == 200, response.text
    # 同一脚本逐级降档，不跨脚本、不跨平台搜索
    assert [quality for _, quality in fake_lx.url_calls] == ["flac", "320k"]
    assert fake_lx.url_song_sources == ["kw", "kw"]
    assert all("a.js" not in (excludes or []) for excludes in fake_lx.url_excludes)
    assert fake_lx.search_calls == []


def test_track_url_platform_preferred_resolves_trusted_other_platform(
    test_app_client, fake_lx, policy_runtime, tmp_path, monkeypatch,
):
    fake_lx.sources = [
        {"id": "only.js", "name": "Only", "enabled": True, "status": "success",
         "supportedSources": ["kw", "wy"]},
    ]
    fake_lx.search_results_by_source = {
        "wy": [{
            "name": "夜曲", "singer": "周杰伦", "source": "wy", "songmid": "wy9",
            "albumName": "十一月的萧邦", "interval": "03:47",
            "types": [{"type": "128k"}, {"type": "flac"}],
        }],
    }
    _write_env(tmp_path, monkeypatch, {
        "FNMUSIC_LX_PLATFORM_PREFERRED": "true",
    })

    async def by_platform(song_info, quality="128k", exclude_api_sources=None,
                          request_timeout=None, auto_switch_api_source=True):
        fake_lx.url_song_sources.append(str(song_info.get("source") or ""))
        fake_lx.url_excludes.append(list(exclude_api_sources) if exclude_api_sources else None)
        if song_info.get("source") == "wy":
            # 真实契约：固定脚本解析成功时上游回报被执行脚本的 sourceId
            return {"url": "https://media.test/song.flac", "type": "flac",
                    "sourceName": "Only", "sourceId": "only.js"}
        return None

    monkeypatch.setattr(fake_lx, "get_music_url", by_platform)
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_response)
    try:
        response = test_app_client.get(
            "/api/v1/track/url",
            params={
                "id": "lx:kw:5886682", "quality": "lossless",
                "title": "夜曲", "artist": "周杰伦", "duration": 227,
            },
        )
    finally:
        lxapp.app.state.client = orig_client
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert fake_lx.url_song_sources == ["kw", "wy"]
    assert fake_lx.search_calls and fake_lx.search_calls[0][0] == "wy"
    assert data["source_platform"] == "wy"
    assert data["resolved_track_id"] == "lx:wy:wy9"
    assert data["source_script"] == "only.js"
    assert data["resolution_quality"] == "lossless"
    assert data["actual_quality"] == ""


@pytest.mark.asyncio
async def test_resolver_timeout_does_not_trip_script_circuit(fake_lx, policy_runtime, monkeypatch):
    async def slow(song_info, quality="128k", exclude_api_sources=None,
                   request_timeout=None, auto_switch_api_source=True):
        await asyncio.sleep(0.05)
        return {"url": "https://media.test/song.flac", "type": "flac",
                "sourceId": "source1"}

    monkeypatch.setattr(fake_lx, "get_music_url", slow)
    client = mock_client(_flac_response)
    try:
        for _ in range(2):
            result = await lxapp.resolve_and_probe(
                client, "kw", {"id": "lx:kw:slow", "songmid": "slow"},
                quality="standard", budget=0.2, resolver_timeout=0.02, probe_timeout=0.1,
            )
            assert result is None
    finally:
        await client.aclose()
    assert lxapp._CHAIN_HEALTH == {}


@pytest.mark.asyncio
async def test_internal_service_failure_does_not_trip_script_circuit(
    fake_lx, policy_runtime, monkeypatch,
):
    async def dead(song_info, quality="128k", exclude_api_sources=None,
                   request_timeout=None, auto_switch_api_source=True):
        raise httpx.ConnectError("lxserver down")

    monkeypatch.setattr(fake_lx, "get_music_url", dead)
    client = mock_client(_flac_response)
    try:
        with pytest.raises(lxapp.SourceStateQueryError):
            await lxapp.resolve_and_probe(
                client, "kw", {"id": "lx:kw:down"}, quality="standard", budget=1.0,
                resolver_timeout=0.2, probe_timeout=0.2,
            )
    finally:
        await client.aclose()
    assert lxapp._CHAIN_HEALTH == {}


def test_track_url_service_failure_returns_503_without_negative_cache(
    test_app_client, fake_lx, policy_runtime,
):
    async def dead():
        raise httpx.ConnectError("lxserver down")

    fake_lx.list_custom_sources = dead
    response = test_app_client.get(
        "/api/v1/track/url?id=lx:kw:5886682&quality=lossless",
    )
    assert response.status_code == 503
    body = response.json()
    assert body["category"] == "service_unavailable"
    assert body["stage"] == "lxmusic_to_lxserver"
    assert lxapp._URL_CACHE == {}  # 服务故障不能写 60 秒负缓存
    assert lxapp._CHAIN_HEALTH == {}


# --------------------------------------------------------- 排序管理接口 --


def test_source_reorder_calls_upstream_persists_and_bumps_generation(
    test_app_client, fake_lx, policy_runtime,
):
    fake_lx.sources = [
        {"id": "a.js", "name": "A", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "b.js", "name": "B", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "c.js", "name": "C", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
    ]
    before = lxapp._URL_CACHE_GEN
    response = test_app_client.post(
        "/api/v1/source/reorder", json={"source_ids": ["c.js", "a.js"]},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # 管理适配层同时消费顶层策略和排序结果，兼容两种已有读取方式。
    assert response.json()["policy"] == data["policy"]
    assert fake_lx.reorder_calls == [["c.js", "b.js", "a.js"]]
    assert data["source_ids"] == ["c.js", "b.js", "a.js"]
    assert data["active_source_order"] == ["c.js", "b.js", "a.js"]
    assert data["generation"] > before
    assert lxapp._URL_CACHE_GEN > before
    saved = json.loads(
        (Path(lxapp.CONF["data_dir"]) / "source_order.json").read_text(encoding="utf-8")
    )
    assert saved["order"] == ["c.js", "b.js", "a.js"]


def test_source_reorder_keeps_disabled_script_slot(
    test_app_client, fake_lx, policy_runtime, tmp_path, monkeypatch,
):
    fake_lx.sources = [
        {"id": "a.js", "name": "A", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "b.js", "name": "B", "enabled": False, "supportedSources": ["kw"]},
        {"id": "c.js", "name": "C", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
    ]
    _write_env(tmp_path, monkeypatch, {
        "FNMUSIC_LX_SOURCE_ORDER": json.dumps(["b.js", "a.js", "c.js"]),
    })
    response = test_app_client.post(
        "/api/v1/source/reorder", json={"source_ids": ["c.js", "a.js"]},
    )
    assert response.status_code == 200, response.text
    # 停用的 b.js 保持在原位置，提交的 c/a 只交换彼此槽位
    assert fake_lx.reorder_calls == [["b.js", "c.js", "a.js"]]


def test_source_reorder_rejects_unknown_or_duplicate_ids(
    test_app_client, fake_lx, policy_runtime,
):
    response = test_app_client.post(
        "/api/v1/source/reorder", json={"source_ids": ["missing.js"]},
    )
    assert response.status_code == 400
    response = test_app_client.post(
        "/api/v1/source/reorder", json={"source_ids": ["source1", "source1"]},
    )
    assert response.status_code == 400
    assert fake_lx.reorder_calls == []


def test_source_reorder_rolls_back_when_upstream_order_not_applied(
    test_app_client, fake_lx, policy_runtime,
):
    fake_lx.sources = [
        {"id": "a.js", "name": "A", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "b.js", "name": "B", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
    ]
    fake_lx.reorder_mutate = False
    response = test_app_client.post(
        "/api/v1/source/reorder", json={"source_ids": ["b.js", "a.js"]},
    )
    assert response.status_code == 502
    assert fake_lx.reorder_calls == [["b.js", "a.js"], ["a.js", "b.js"]]


def test_source_reorder_rolls_back_when_local_persist_fails(
    test_app_client, fake_lx, policy_runtime, monkeypatch,
):
    fake_lx.sources = [
        {"id": "a.js", "name": "A", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "b.js", "name": "B", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
    ]

    def boom(order, env_order):
        raise OSError("disk full")

    monkeypatch.setattr(lxapp, "_write_source_order_file", boom)
    response = test_app_client.post(
        "/api/v1/source/reorder", json={"source_ids": ["b.js", "a.js"]},
    )
    assert response.status_code == 500
    assert fake_lx.reorder_calls == [["b.js", "a.js"], ["a.js", "b.js"]]


@pytest.mark.asyncio
async def test_source_reorder_returns_busy_when_management_lock_held(
    fake_lx, policy_runtime,
):
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with management_operation():
            started.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await started.wait()
    try:
        with pytest.raises(SourceOperationBusy):
            await lxapp.source_reorder(lxapp.ReorderBody(source_ids=["source1"]))
        assert fake_lx.reorder_calls == []
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_source_reorder_http_maps_busy_to_409(fake_lx, policy_runtime):
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with management_operation():
            started.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await started.wait()
    try:
        transport = httpx.ASGITransport(app=lxapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/source/reorder", json={"source_ids": ["source1"]},
            )
        assert response.status_code == 409
        assert response.json()["category"] == "source_operation_busy"
        assert fake_lx.reorder_calls == []
    finally:
        release.set()
        await task


def test_policy_change_bumps_generation_and_clears_url_cache(
    policy_runtime, tmp_path, monkeypatch,
):
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_AUTO_SWITCH": "true"})
    lxapp._sync_policy_generation(lxapp._policy_settings())
    lxapp._URL_CACHE[("lx:kw:1", "lossless")] = {
        "data": {"url": "https://media.test/old.flac"}, "ts": time.monotonic(),
    }
    before = lxapp._URL_CACHE_GEN
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_AUTO_SWITCH": "false"})
    lxapp._sync_policy_generation(lxapp._policy_settings())
    assert lxapp._URL_CACHE == {}
    assert lxapp._URL_CACHE_GEN > before


def test_env_order_with_active_only_preserves_disabled_slot(
    policy_runtime, tmp_path, monkeypatch,
):
    _write_env(tmp_path, monkeypatch, {
        "FNMUSIC_LX_SOURCE_ORDER": json.dumps(["c.js", "b.js"]),
    })
    # a.js 停用且不在 env 列表：它应保留原槽位，而不是被挤到末尾。
    assert lxapp._effective_source_order(
        ["a.js", "b.js", "c.js"], ("c.js", "b.js"),
    ) == ["a.js", "c.js", "b.js"]


def test_order_file_wins_until_env_order_changes(
    policy_runtime, tmp_path, monkeypatch,
):
    _write_env(tmp_path, monkeypatch, {
        "FNMUSIC_LX_SOURCE_ORDER": json.dumps(["b.js", "a.js", "c.js"]),
    })
    # 排序接口写入的本地顺序比 env 更新；env 快照一致时文件优先。
    lxapp._write_source_order_file(
        ["c.js", "b.js", "a.js"], ["b.js", "a.js", "c.js"],
    )
    assert lxapp._effective_source_order(
        ["a.js", "b.js", "c.js"], ("b.js", "a.js", "c.js"),
    ) == ["c.js", "b.js", "a.js"]
    # 用户改写 env 后，env 覆盖旧文件。
    assert lxapp._effective_source_order(
        ["a.js", "b.js", "c.js"], ("a.js", "b.js", "c.js"),
    ) == ["a.js", "b.js", "c.js"]


def test_reorder_client_contract_sends_source_ids_and_auth():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        captured["auth"] = request.headers.get("x-frontend-auth")
        return httpx.Response(200, json={"success": True})

    async def run():
        client = LxServerClient(base_url="http://test", admin_password="pw")
        # 先确认生产 get_client() 会带管理认证头，再用离线客户端验证请求体。
        default = await client.get_client()
        assert default.headers["x-frontend-auth"] == "pw"
        await default.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://test",
            headers={"x-frontend-auth": "pw"},
        )
        try:
            return await client.reorder_custom_sources(["b.js", "a.js"])
        finally:
            await client.close()

    assert asyncio.run(run()) == {"success": True}
    assert captured["body"] == {"sourceIds": ["b.js", "a.js"]}
    assert captured["auth"] == "pw"


def test_get_music_url_pins_script_with_excludes_and_keeps_auto_switch_flag():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"url": "https://media.test/song.flac", "type": "flac"})

    async def run():
        client = LxServerClient(base_url="http://test", admin_password="pw")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://test",
        )
        try:
            return await client.get_music_url(
                {"source": "kw", "songmid": "1", "name": "夜曲", "singer": "周杰伦"},
                "flac",
                exclude_api_sources=["a.js", "A"],
                request_timeout=1.0,
            )
        finally:
            await client.close()

    assert asyncio.run(run()) is not None
    assert captured["body"]["enableAutoSwitchApiSource"] is True
    assert captured["body"]["excludeApiSources"] == ["a.js", "A"]
    assert captured["body"]["quality"] == "flac"
