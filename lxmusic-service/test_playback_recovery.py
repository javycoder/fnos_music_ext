"""lxmusic-service/test_playback_recovery.py

恢复修复（单次恢复试探）的失败优先测试，覆盖九项必须修复的行为：

1. 音质阶梯：首个脚本返回低档时不得立即返回，目标档要遍历完全部脚本；
   低档结果暂存，阶梯走到该档时再复用；报告档高于目标档不得当成目标档成功。
2. `LxServerClient.get_music_url` 的 `enableAutoSwitchApiSource` 参数化：
   旧调用保持 true 默认，固定脚本请求显式 false；返回的 `sourceId` 必须等于
   被固定的脚本 ID，缺失/其他脚本/内置平台都不算目标脚本成功。
3. 熔断键包含音质档位，高档缺失不误伤标准档；取消、预算耗尽、内部服务不可达
   都不记作脚本失败。
4. 歌手比较禁止子串匹配（周杰伦歌迷会、王菲菲）；同版 Live 的括号/尾词归一，
   伴奏/翻唱/现场保持区分；时长从 duration_s/duration/interval 显式提取，
   已知冲突不静默通过。
5. 按运行声明跳过明确不支持的档位，未知仍可探索；单次尝试的搜索/解析/探活
   共用一个 slot 预算，首慢源不能吃光总预算；不设最多三脚本上限。
6. 直接复用 proxy/quality_policy.py，不再保留本地七档回退；mp3 不反推 128k；
   码率未知记 0，不编造 999k/1999k；quality_evidence 标明报告档与探测格式。
7. 播放路径新日志只记媒体主机名与阶段，不记录带签名的原始直链。
8. source_capabilities 与 describe 不再硬编码三档，只报真实声明或未知。
9. 排序：env 清空回退持久顺序、新激活追加、停用保留槽位、重启恢复，
   运行期排序同步上游但绝不在播放热路径反复 POST。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from conftest import lxapp, mock_client
import playback_policy as policy


# ---------------------------------------------------------------- 测试辅助 --


def _ready(sid: str, supported, **extra) -> dict:
    """lxserver 源列表条目（启用 + 初始化成功）。"""
    entry = {
        "id": sid,
        "name": sid,
        "enabled": True,
        "status": "success",
        "supportedSources": list(supported),
    }
    entry.update(extra)
    return entry


def _item(**overrides) -> dict:
    item = {"id": "lx:kw:5886682", "lx_source": "kw", "songmid": "5886682",
            "title": "夜曲", "artist": "周杰伦", "duration_s": 227.0}
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


def _media_client(handler=None):
    """按最终直链后缀返回 flac/mp3 前缀魔数，便于断言实际格式。"""

    def dispatch(request: httpx.Request) -> httpx.Response:
        if handler is not None:
            decided = handler(request)
            if decided is not None:
                return decided
        if request.url.path.lower().endswith(".mp3"):
            return _mp3_response(request)
        return _flac_response(request)

    return mock_client(dispatch)


def _write_env(tmp_path: Path, monkeypatch, values: dict[str, str]) -> Path:
    path = tmp_path / "policy.env"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    monkeypatch.setenv("LX_ENV_PATH", str(path))
    return path


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """隔离直链缓存、在途、熔断键、策略签名、候选缓存与排序持久目录。"""
    monkeypatch.setitem(lxapp.CONF, "data_dir", str(tmp_path / "data"))
    for key in ("FNMUSIC_LX_AUTO_SWITCH", "FNMUSIC_LX_PLATFORM_PREFERRED",
                "FNMUSIC_LX_SOURCE_ORDER", "LX_ENV_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(lxapp, "_POLICY_SIGNATURE", None, raising=False)
    monkeypatch.setattr(lxapp, "_PERSISTED_ENV_CACHE", None, raising=False)
    monkeypatch.setattr(lxapp, "_LAST_UPSTREAM_ORDER", None, raising=False)
    for cache in (lxapp._SONG_CACHE, lxapp._URL_CACHE, lxapp._URL_INFLIGHT, lxapp._CHAIN_HEALTH):
        cache.clear()
    lxapp._CANDIDATES.invalidate()
    yield
    for cache in (lxapp._SONG_CACHE, lxapp._URL_CACHE, lxapp._URL_INFLIGHT, lxapp._CHAIN_HEALTH):
        cache.clear()
    lxapp._CANDIDATES.invalidate()


# =========================================================== 1 音质阶梯 ==


def test_low_tier_from_first_script_is_stashed_not_returned(
    test_app_client, fake_lx, runtime, tmp_path, monkeypatch,
):
    """首脚本只回 128k 时不得立即返回：目标档要先问完所有脚本。"""
    fake_lx.sources = [_ready("a.js", ["kw"]), _ready("b.js", ["kw"])]
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_SOURCE_ORDER": json.dumps(["a.js", "b.js"])})
    fake_lx.url_script = [
        {"url": "https://media.test/low.mp3", "type": "128k", "sourceId": "a.js"},
        {"url": "https://media.test/target.flac", "type": "flac", "sourceId": "b.js"},
    ]
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # 两个脚本都按目标档 flac 请求，没有先降到 320k
    assert [quality for _, quality in fake_lx.url_calls] == ["flac", "flac"]
    assert data["source_script"] == "b.js"
    assert data["actual_quality"] == "lossless"
    assert data["requested_quality"] == "lossless"


def test_stashed_lower_tier_is_reused_when_ladder_reaches_its_tier(
    test_app_client, fake_lx, runtime, tmp_path, monkeypatch,
):
    """单脚本目标无损只回 128k：暂存后走到 128k 档复用，不重复解析。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_SOURCE_ORDER": json.dumps(["a.js"])})
    fake_lx.url_script = [
        {"url": "https://media.test/only.mp3", "type": "128k", "sourceId": "a.js"},
    ]
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # 只请求目标档一次，低档结果来自暂存而不是重复解析
    assert [quality for _, quality in fake_lx.url_calls] == ["flac"]
    assert data["actual_quality"] == "standard"
    assert data["requested_quality"] == "lossless"
    assert "lossless" in data["fallback_reason"]
    assert "standard" in data["fallback_reason"]


def test_ladder_uses_better_stash_before_new_request(
    test_app_client, fake_lx, runtime, tmp_path, monkeypatch,
):
    """两个脚本分别只给 320k/128k：先暂存，再按脚本顺序走到 320k 档复用。"""
    fake_lx.sources = [_ready("a.js", ["kw"]), _ready("b.js", ["kw"])]
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_SOURCE_ORDER": json.dumps(["a.js", "b.js"])})
    fake_lx.url_script = [
        {"url": "https://media.test/a.mp3", "type": "320k", "sourceId": "a.js"},
        {"url": "https://media.test/b.mp3", "type": "128k", "sourceId": "b.js"},
    ]
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # 目标档各问一次后即用暂存，不再向下重复请求同一脚本
    assert [quality for _, quality in fake_lx.url_calls] == ["flac", "flac"]
    assert data["source_script"] == "a.js"
    assert data["actual_quality"] == "exhigh"


def test_reported_quality_above_target_is_never_claimed(
    test_app_client, fake_lx, runtime,
):
    """已知高于目标档必须拒绝，不能只把实际音质字段改低后原样交付。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]
    fake_lx.url_result = {"url": "https://media.test/up.flac", "type": "flac", "sourceId": "a.js"}
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=standard")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 404, response.text


@pytest.mark.parametrize('first_type', ['mp3', 'flac'])
def test_unknown_or_contradictory_quality_cannot_preempt_verified_target(
    test_app_client, fake_lx, runtime, first_type,
):
    # A只给MP3，未知品质或谎报flac都不能抢占B真实FLAC的目标档。
    fake_lx.sources = [_ready('a.js', ['kw']), _ready('b.js', ['kw'])]
    fake_lx.url_script = [
        {'url': 'https://media.test/a.mp3', 'type': first_type, 'sourceId': 'a.js'},
        {'url': 'https://media.test/b.flac', 'type': 'flac', 'sourceId': 'b.js'},
    ]
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get('/api/v1/track/url?id=lx:kw:5886682&quality=lossless')
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['source_script'] == 'b.js'
    assert data['actual_quality'] == 'lossless'
    assert data['ext'] == 'flac'


# ========================================= 2 sourceId 与固定脚本契约 ==


def test_get_music_url_api_switch_flag_keeps_old_default_and_allows_false():
    """enableAutoSwitchApiSource 参数化：缺省仍 true，固定脚本请求可显式 false。"""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"url": "https://media.test/song.flac",
                                         "type": "flac", "sourceId": "a.js"})

    async def run():
        from lxserver_client import LxServerClient
        client = LxServerClient(base_url="http://test", admin_password="pw")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
        try:
            await client.get_music_url({"source": "kw", "songmid": "1"}, "flac",
                                       exclude_api_sources=["b.js"], request_timeout=1.0)
            await client.get_music_url({"source": "kw", "songmid": "1"}, "flac",
                                       exclude_api_sources=["b.js"], request_timeout=1.0,
                                       auto_switch_api_source=False)
        finally:
            await client.close()

    asyncio.run(run())
    assert [body["enableAutoSwitchApiSource"] for body in captured] == [True, False]
    assert captured[0]["excludeApiSources"] == ["b.js"]


def test_pinned_attempt_sends_auto_switch_false(test_app_client, fake_lx, runtime):
    """固定脚本的播放请求必须显式关闭上游自动切源。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]
    fake_lx.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceId": "a.js"}
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 200, response.text
    assert fake_lx.url_auto_switch == [False]


@pytest.mark.parametrize("source_id", ["kw", "other.js", ""])
def test_attempt_rejects_response_without_pinned_source_id(
    test_app_client, fake_lx, runtime, source_id,
):
    """缺 ID、其他脚本、内置平台都不能冒充被固定的脚本成功。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]
    entry = {"url": "https://media.test/rogue.flac", "type": "flac"}
    if source_id:
        entry["sourceId"] = source_id
    fake_lx.url_script = [entry]
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=standard")
        second = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=standard")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 404, response.text
    assert second.status_code == 404
    # 来源不符是脚本侧失败，不是内部服务故障：只允许负缓存，不能写成功缓存
    assert all(entry.get("data") is None for entry in lxapp._URL_CACHE.values())


def test_attempt_rejects_response_from_other_script_then_uses_pinned_one(
    test_app_client, fake_lx, runtime, tmp_path, monkeypatch,
):
    """首个脚本返回别人的 sourceId 时继续下一个脚本，而不是当成成功。"""
    fake_lx.sources = [_ready("a.js", ["kw"]), _ready("b.js", ["kw"])]
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_SOURCE_ORDER": json.dumps(["a.js", "b.js"])})
    fake_lx.url_script = [
        {"url": "https://media.test/rogue.flac", "type": "flac", "sourceId": "b.js"},
        {"url": "https://media.test/ok.flac", "type": "flac", "sourceId": "b.js"},
    ]
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source_script"] == "b.js"
    # a.js 的固定请求被拒，b.js 的请求才成功
    assert fake_lx.url_excludes == [["b.js"], ["a.js"]]


# ================================================= 3 熔断键与失败分类 ==


@pytest.mark.asyncio
async def test_high_tier_failure_does_not_open_circuit_for_standard(fake_lx, runtime, monkeypatch):
    """熔断键含音质档：无损档连续失败不会把标准档一起熔断。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]

    async def without_flac(song_info, quality="128k", exclude_api_sources=None,
                           request_timeout=None, auto_switch_api_source=True):
        fake_lx.url_calls.append((str(song_info.get("songmid") or ""), quality))
        # 该脚本没有无损档：要 flac 时拿不到链接，320k 及以下正常
        if quality == "flac":
            return None
        return {"url": "https://media.test/ok.flac", "type": quality, "sourceId": "a.js"}

    monkeypatch.setattr(fake_lx, "get_music_url", without_flac)
    client = _media_client()
    try:
        for _ in range(3):
            result = await lxapp.resolve_and_probe(
                client, "kw", _item(), quality="lossless",
                budget=2.0, resolver_timeout=0.5, probe_timeout=0.5,
            )
            assert result is not None and result["actual_quality"] == "exhigh"
        # 只有无损档被记失败，标准档没有被牵连
        assert any(key.endswith(":lossless") for key in lxapp._CHAIN_HEALTH), lxapp._CHAIN_HEALTH
        assert not any(key.endswith(":standard") for key in lxapp._CHAIN_HEALTH)

        calls_before = len(fake_lx.url_calls)
        standard = await lxapp.resolve_and_probe(
            client, "kw", _item(), quality="standard",
            budget=1.0, resolver_timeout=0.5, probe_timeout=0.5,
        )
    finally:
        await client.aclose()
    assert standard is not None and standard["actual_quality"] == "standard"
    # 标准档是真的重新请求了脚本，而不是被熔断跳过
    assert fake_lx.url_calls[calls_before:] == [(str(_item()["songmid"]), "128k")]


@pytest.mark.asyncio
async def test_cancellation_and_budget_exhaustion_do_not_record_failure(
    fake_lx, runtime, monkeypatch,
):
    """取消与预算耗尽都不算脚本永久失效。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]
    started = asyncio.Event()

    async def slow(song_info, quality="128k", exclude_api_sources=None,
                   request_timeout=None, auto_switch_api_source=True):
        started.set()
        await asyncio.sleep(5)
        return None

    monkeypatch.setattr(fake_lx, "get_music_url", slow)
    client = _media_client()
    try:
        task = asyncio.create_task(lxapp.resolve_and_probe(
            client, "kw", _item(), quality="standard",
            budget=5.0, resolver_timeout=4.0, probe_timeout=0.5,
        ))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lxapp._CHAIN_HEALTH == {}

        # 预算耗尽：单次尝试超时后总预算也归零，同样不记失败
        assert await lxapp.resolve_and_probe(
            client, "kw", _item(), quality="standard",
            budget=0.2, resolver_timeout=0.05, probe_timeout=0.02,
        ) is None
    finally:
        await client.aclose()
    assert lxapp._CHAIN_HEALTH == {}


@pytest.mark.asyncio
async def test_script_failure_then_budget_exhaustion_records_only_true_failure(
    fake_lx, runtime, monkeypatch,
):
    """同一轮里「真失败」与「超时」要分开记账：只有超时不该留永久失败。

    a.js 明确返回空链接（预算充足时发生的真实脚本失败）可以记熔断；
    b.js 只是慢到单次超时，属于预算侧问题，不能写成脚本永久失效。
    """
    fake_lx.sources = [_ready("a.js", ["kw"]), _ready("b.js", ["kw"])]
    calls: list[str] = []

    async def fail_then_hang(song_info, quality="128k", exclude_api_sources=None,
                             request_timeout=None, auto_switch_api_source=True):
        pinned = "a.js" if exclude_api_sources == ["b.js"] else "b.js"
        calls.append(pinned)
        # a.js 真失败（脚本明确拿不出直链），b.js 慢到单次超时
        if pinned == "a.js":
            return None
        await asyncio.sleep(1.0)
        return None

    monkeypatch.setattr(fake_lx, "get_music_url", fail_then_hang)
    client = _media_client()
    try:
        # 单次解析超时上限远大于总预算：b.js 会把剩余预算耗尽后才结束
        assert await lxapp.resolve_and_probe(
            client, "kw", _item(), quality="standard",
            budget=0.12, resolver_timeout=5.0, probe_timeout=0.5,
        ) is None
    finally:
        await client.aclose()
    assert calls == ["a.js", "b.js"]
    recorded = set(lxapp._CHAIN_HEALTH)
    assert recorded == {f"policy:{lxapp._URL_CACHE_GEN}:a.js:kw:standard"}, recorded
    # 超时脚本不得留下熔断记录
    assert not any("b.js" in key for key in recorded)


# ======================================= 4 歌手/版本/时长可信匹配 ==


def test_artist_comparison_forbids_substring_matches():
    """歌手比较禁止子串：歌迷会与相近名字都不算同一歌手。"""
    assert not policy.artists_compatible("周杰伦", "周杰伦歌迷会")
    assert not policy.artists_compatible("王菲", "王菲菲")
    assert not policy.artists_compatible("Bill Withers", "Bill Withers Fanclub")
    assert not policy.artists_compatible("Theft", "The Fanclub")
    assert policy.artists_compatible("Bill Withers feat. Guest", "Bill Withers")
    # 正常同名与多歌手仍然兼容
    assert policy.artists_compatible("周杰伦", "周杰伦")
    assert policy.artists_compatible("周杰伦", "周杰伦、费玉清")
    # 缺失歌手身份一律拒绝
    assert not policy.artists_compatible("", "周杰伦")
    assert not policy.artists_compatible("未知歌手", "周杰伦")


def test_version_tags_normalize_live_spellings_but_keep_variants():
    """同版 Live 括号/尾词归一，伴奏/翻唱/现场版本保持区分。"""
    assert policy.version_tags("夜曲 (Live)") == policy.version_tags("夜曲 Live") == {"live"}
    assert policy.version_tags("夜曲（现场版）") == {"live"}
    assert policy.version_tags("夜曲 (伴奏)") == policy.version_tags("夜曲 伴奏") == {"instrumental"}
    assert policy.version_tags("夜曲 (伴奏)") != policy.version_tags("夜曲 (Live)")
    assert policy.version_tags("夜曲 (翻唱)") == {"cover"}
    assert policy.version_tags("夜曲") == set()


def test_trusted_candidate_extracts_duration_keys_and_rejects_conflicts():
    """时长从 duration_s/duration/interval 显式提取，已知冲突不得静默通过。"""
    target = {"title": "夜曲", "artist": "周杰伦", "duration_s": 227.0}
    # 候选只给 interval（03:47 = 227 秒）→ 可信
    assert policy.is_trusted_candidate(
        target, {"title": "夜曲", "artist": "周杰伦", "interval": "03:47"})
    # interval 明显冲突（05:24 = 324 秒）→ 不可信（旧实现未知时长直接放行）
    assert not policy.is_trusted_candidate(
        target, {"title": "夜曲", "artist": "周杰伦", "interval": "05:24"})
    assert not policy.is_trusted_candidate(
        target, {"title": "夜曲", "artist": "周杰伦", "duration": 324})
    # 候选自身多个时长键互相冲突 → 不静默通过
    assert not policy.is_trusted_candidate(
        target, {"title": "夜曲", "artist": "周杰伦", "duration_s": 227, "interval": "05:24"})
    # 双方都未知时按现有阈值放行，不因缺时长否定
    assert policy.is_trusted_candidate(
        {"title": "夜曲", "artist": "周杰伦"},
        {"title": "夜曲", "artist": "周杰伦"})


# ================================== 5 运行声明与 slot 预算分配 ==


def test_source_catalog_reads_runtime_quality_declaration():
    """运行声明映射为统一档位；无声明保持未知，不凭空造能力。"""
    catalog = policy.build_source_catalog([
        _ready("a.js", ["kw"], platforms={"kw": {"qualitys": ["128k", "flac"]}}),
        _ready("b.js", ["kw"]),
    ])
    assert catalog[0].qualities == {"kw": ("standard", "lossless")}
    assert catalog[1].qualities == {}


def test_auto_switch_off_keeps_first_platform_compatible_script_when_target_missing():
    scripts = policy.build_source_catalog([
        _ready('a.js', ['kw'], qualitys={'kw': ['128k']}),
        _ready('b.js', ['kw'], qualitys={'kw': ['flac']}),
    ])
    steps = policy.plan_steps(['lossless', 'exhigh', 'higher', 'standard'], scripts, 'kw',
                              auto_switch=False, platform_preferred=True)
    assert [(step.script_id, step.quality) for step in steps] == [('a.js', 'standard')]


def test_unknown_platform_declaration_does_not_authorize_cross_platform_search():
    scripts = policy.build_source_catalog([_ready('unknown.js', [])])
    steps = policy.plan_steps(['standard'], scripts, 'kw', auto_switch=True, platform_preferred=True)
    assert [step.platform for step in steps] == ['kw']


def test_plan_skips_tiers_missing_from_runtime_declaration():
    """明确声明不支持的档位直接跳过；未知声明的档位仍可探索。"""
    declared = policy.ScriptInfo(
        id="a.js", name="a", ready=True, platforms=("kw",),
        qualities={"kw": ("standard", "exhigh")},
    )
    unknown = policy.ScriptInfo(id="b.js", name="b", ready=True, platforms=("kw",))
    ladder = ["lossless", "exhigh", "higher", "standard"]
    declared_steps = policy.plan_steps(ladder, [declared], "kw")
    assert [step.quality for step in declared_steps] == ["exhigh", "standard"]
    unknown_steps = policy.plan_steps(ladder, [unknown], "kw")
    assert [step.quality for step in unknown_steps] == ladder


@pytest.mark.asyncio
async def test_declared_unsupported_tier_is_not_requested(fake_lx, runtime, monkeypatch):
    """声明只支持 128k/320k 的脚本不会被请求无损档。"""
    fake_lx.sources = [
        _ready("a.js", ["kw"], platforms={"kw": {"qualitys": ["128k", "320k"]}}),
    ]
    client = _media_client()
    try:
        await lxapp.resolve_and_probe(
            client, "kw", _item(), quality="lossless",
            budget=2.0, resolver_timeout=0.5, probe_timeout=0.5,
        )
    finally:
        await client.aclose()
    assert [quality for _, quality in fake_lx.url_calls] == ["320k"]


@pytest.mark.asyncio
async def test_first_slow_script_does_not_eat_total_budget(fake_lx, runtime, monkeypatch):
    """首慢源受 slot 预算约束，后续同档脚本仍有解析机会。"""
    fake_lx.sources = [_ready("a.js", ["kw"]), _ready("b.js", ["kw"])]

    async def slow(song_info, quality="128k", exclude_api_sources=None,
                   request_timeout=None, auto_switch_api_source=True):
        await asyncio.sleep(1.0)
        return {"url": "https://media.test/a.flac", "type": "128k", "sourceId": "a.js"}

    async def fast(song_info, quality="128k", exclude_api_sources=None,
                   request_timeout=None, auto_switch_api_source=True):
        return {"url": "https://media.test/b.flac", "type": "128k", "sourceId": "b.js"}

    calls: list[str] = []

    async def by_pin(song_info, quality="128k", exclude_api_sources=None,
                     request_timeout=None, auto_switch_api_source=True):
        pinned = "a.js" if exclude_api_sources == ["b.js"] else "b.js"
        calls.append(pinned)
        fake_lx.url_timeouts.append(request_timeout)
        return await (slow if pinned == "a.js" else fast)(
            song_info, quality, exclude_api_sources, request_timeout, auto_switch_api_source)

    monkeypatch.setattr(fake_lx, "get_music_url", by_pin)
    client = _media_client()
    try:
        result = await lxapp.resolve_and_probe(
            client, "kw", _item(), quality="standard",
            budget=1.0, resolver_timeout=8.0, probe_timeout=0.5,
        )
    finally:
        await client.aclose()
    assert calls == ["a.js", "b.js"]
    assert result is not None and result["source_script"] == "b.js"
    assert fake_lx.url_timeouts[0] < 1.0


@pytest.mark.asyncio
async def test_candidate_search_shares_step_slot_budget(fake_lx, runtime, monkeypatch):
    """候选搜索与解析共用同一个 slot 预算，不会单独占满总预算。"""
    fake_lx.sources = [
        _ready("only.js", ["kw", "wy"]),
    ]
    # 原平台解析失败才会去找其他平台候选
    fake_lx.url_script = [None]
    fake_lx.search_results_by_source = {
        "wy": [{"name": "夜曲", "singer": "周杰伦", "source": "wy", "songmid": "wy9",
                "albumName": "十一月的萧邦", "interval": "03:47",
                "types": [{"type": "128k"}, {"type": "flac"}]}],
    }
    monkeypatch.setenv("FNMUSIC_LX_PLATFORM_PREFERRED", "true")
    seen: list[float] = []
    original = lxapp._CANDIDATES.find_many

    async def spy(searcher, item, platforms, deadline, generation=0):
        seen.append(deadline)
        return await original(searcher, item, platforms, deadline, generation)

    monkeypatch.setattr(lxapp._CANDIDATES, "find_many", spy)
    client = _media_client()
    started = lxapp.time.monotonic()
    try:
        await lxapp.resolve_and_probe(
            client, "kw", _item(), quality="lossless",
            budget=4.0, resolver_timeout=1.0, probe_timeout=0.5,
        )
    finally:
        await client.aclose()
    assert seen, "跨平台候选搜索没有被调用"
    # 搜索截止时间被限制在当前 step 的 slot 内，而不是总 deadline
    assert seen[0] - started < 4.0


def test_plan_does_not_cap_ready_scripts_at_three():
    """计划不设最多三脚本上限。"""
    scripts = [policy.ScriptInfo(id=f"s{i}.js", name=f"s{i}", ready=True, platforms=("kw",))
               for i in range(6)]
    steps = policy.plan_steps(["standard"], scripts, "kw")
    assert [step.script_id for step in steps] == [f"s{i}.js" for i in range(6)]


# ======================================= 6 共享质量模块与证据字段 ==


def test_policy_reuses_shared_quality_module():
    """七档契约直接来自 proxy/quality_policy.py，不再保留本地回退定义。"""
    import proxy.quality_policy as shared

    assert policy.QUALITY_LEVELS == shared.QUALITY_LEVELS
    assert policy.normalize_quality is shared.normalize_quality
    assert policy.quality_ladder is shared.quality_ladder
    assert policy.to_lx_quality is shared.to_lx_quality
    assert not hasattr(policy, "_fallback_normalize_quality")
    assert not hasattr(policy, "_fallback_quality_ladder")


def test_from_lx_quality_does_not_infer_standard_from_mp3():
    """上游只说 mp3 时不能推断成 128k。"""
    assert policy.from_lx_quality("mp3") == ""
    assert policy.from_lx_quality("128k") == "standard"
    assert policy.from_lx_quality("flac24bit") == "hires"
    assert policy.from_lx_quality("") == ""


def test_unknown_reported_quality_is_reported_as_unknown(test_app_client, fake_lx, runtime):
    """报告档未知时如实记录，不冒充已验证档位。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]
    fake_lx.url_result = {"url": "https://media.test/song.mp3", "type": "mp3", "sourceId": "a.js"}
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=standard")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["actual_quality"] == ""
    evidence = data["quality_evidence"]
    assert evidence["reported_quality"] is None
    assert evidence["reported_known"] is False
    assert evidence["probe"] == "mp3"
    assert evidence["verified"] is False


def test_unknown_bitrate_is_not_fabricated(test_app_client, fake_lx, runtime):
    """码率未知记 0，不用 999k/1999k 之类的假码率。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]
    fake_lx.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceId": "a.js"}
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = restore
    assert response.json()["data"]["br"] == 0


# ==================================================== 7 日志脱敏 ==


def test_probe_rejection_log_contains_host_only(test_app_client, fake_lx, runtime, caplog):
    """播放路径日志只记媒体主机与阶段，不落带签名的原始直链。"""
    fake_lx.sources = [_ready("a.js", ["kw"])]
    signed = "https://cdn.media.test/secret.mp3?sign=TOPSECRETVALUE&expires=1999999999"
    fake_lx.url_result = {"url": signed, "type": "128k", "sourceId": "a.js"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"denied")

    restore = lxapp.app.state.client
    lxapp.app.state.client = mock_client(handler)
    try:
        with caplog.at_level(logging.INFO, logger="fnmusic.lxmusic"):
            response = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=standard")
    finally:
        lxapp.app.state.client = restore
    assert response.status_code == 404, response.text
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "TOPSECRETVALUE" not in text
    assert "sign=" not in text
    assert "cdn.media.test" in text


# ======================================= 8 capabilities / describe ==


def test_health_source_snapshot_includes_policy_for_webui_fast_path(runtime, tmp_path, monkeypatch):
    _write_env(tmp_path, monkeypatch, {
        'FNMUSIC_LX_AUTO_SWITCH': 'false', 'FNMUSIC_LX_PLATFORM_PREFERRED': 'true',
        'FNMUSIC_LX_SOURCE_ORDER': '["b.js", "a.js"]',
    })
    snapshot = lxapp._describe_source_snapshot([_ready('a.js', ['kw']), _ready('b.js', ['kw'])])
    assert snapshot['policy']['auto_switch'] is False
    assert snapshot['policy']['platform_preferred'] is True
    assert snapshot['policy']['active_source_order'] == ['b.js', 'a.js']


def test_source_capabilities_reports_declared_or_unknown():
    """能力快照不再硬编码三档：有声明报声明，无声明如实为空。"""
    snapshot = lxapp._source_capabilities_snapshot([
        _ready("a.js", ["kw"], platforms={"kw": {"qualitys": ["128k", "flac"]}}),
    ])
    assert snapshot["kw"]["qualitys"] == ["128k", "flac"]
    assert snapshot["kw"]["qualitys_declared"] is True

    unknown = lxapp._source_capabilities_snapshot([_ready("b.js", ["kw"])])
    assert unknown["kw"]["qualitys"] == []
    assert unknown["kw"]["qualitys_declared"] is False


def test_describe_source_does_not_hardcode_quality_ladder():
    """describe 的平台档位只反映真实声明，未知不编造。"""
    snapshot = lxapp._describe_source_snapshot([_ready("a.js", ["kw"])])
    assert snapshot["source"]["platforms"]["kw"] == {"qualitys": []}


# ============================================= 9 排序持久与上游同步 ==


def test_env_order_cleared_falls_back_to_persisted_order(runtime, tmp_path):
    """env 排序清空后回退持久顺序，不丢用户排好的顺序。"""
    lxapp._write_source_order_file(["c.js", "a.js", "b.js"], [])
    assert lxapp._effective_source_order(["a.js", "b.js", "c.js"], ()) == ["c.js", "a.js", "b.js"]
    settings = lxapp._policy_settings()
    assert settings.source_order == ()


def test_new_activated_script_appends_and_disabled_slot_survives_restart(runtime):
    """新激活追加、停用保留槽位；模拟重启后仍从持久文件恢复同一顺序。"""
    lxapp._write_source_order_file(["b.js", "a.js"], ["b.js", "a.js"])
    # 新增 c.js → 追加到尾部
    assert lxapp._effective_source_order(
        ["a.js", "b.js", "c.js"], ("b.js", "a.js")) == ["b.js", "a.js", "c.js"]
    # 停用 a.js 后重新激活：相对位置不变（文件里原本就在 a.js 之前/之后）
    assert lxapp._effective_source_order(
        ["b.js", "a.js"], ("b.js", "a.js")) == ["b.js", "a.js"]
    # 模拟重启：清空进程内状态后仍读回同一顺序
    lxapp._POLICY_SIGNATURE = None
    lxapp._LAST_UPSTREAM_ORDER = None
    lxapp._PERSISTED_ENV_CACHE = None
    assert lxapp._effective_source_order(
        ["a.js", "b.js"], ("b.js", "a.js")) == ["b.js", "a.js"]


def test_runtime_order_syncs_upstream_once_and_not_on_playback_path(
    test_app_client, fake_lx, runtime, tmp_path, monkeypatch,
):
    """运行期排序同步上游一次；播放热路径不得反复 POST。"""
    fake_lx.sources = [_ready("a.js", ["kw"]), _ready("b.js", ["kw"])]
    _write_env(tmp_path, monkeypatch, {"FNMUSIC_LX_SOURCE_ORDER": json.dumps(["b.js", "a.js"])})
    first = test_app_client.get("/api/v1/source")
    assert first.status_code == 200, first.text
    assert fake_lx.reorder_calls == [["b.js", "a.js"]]

    # 第二次查询已经同步过，不重复 POST
    second = test_app_client.get("/api/v1/source")
    assert second.status_code == 200, second.text
    assert fake_lx.reorder_calls == [["b.js", "a.js"]]

    # 播放热路径多次解析也不触发排序 POST
    fake_lx.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceId": "a.js"}
    restore = lxapp.app.state.client
    lxapp.app.state.client = _media_client()
    try:
        for _ in range(5):
            test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless",
                                params={"fresh": 1})
    finally:
        lxapp.app.state.client = restore
    assert fake_lx.reorder_calls == [["b.js", "a.js"]]


# ================================ 10 不安全 VM 不自动授权（lead 追加契约） ==


@pytest.mark.parametrize('status, success, message', [
    (500, False, 'confirmation required'),
    (200, True, 'confirmation required'),
    (500, False, '脚本需要 unsafe VM 运行环境，请确认 allowUnsafeVM'),
])
def test_upload_and_import_never_answer_unsafe_vm_confirm(status, success, message):
    """第三方脚本不受信任：上传/导入都不代答 allowUnsafeVM，且不静默重试。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        calls.append(payload)
        return httpx.Response(status, json={
            "success": success,
            "error": message,
            "requireUnsafe": True,
        })

    async def run():
        from lxserver_client import LxServerClient, UnsafeVmRequired
        client = LxServerClient(base_url="http://test", admin_password="pw")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                           base_url="http://test")
        try:
            with pytest.raises(UnsafeVmRequired):
                await client.upload_custom_source("x.js", "console.log(1)")
            with pytest.raises(UnsafeVmRequired):
                await client.import_custom_source("https://example.test/src.js")
        finally:
            await client.close()

    asyncio.run(run())
    # 两次调用各一次请求：没有自动重试、没有把 true 发上去
    assert [payload.get("allowUnsafeVM") for payload in calls] == [False, False]


def test_source_upload_maps_unsafe_vm_to_explicit_category(test_app_client, fake_lx, runtime):
    """需要不安全 VM 时返回明确分类，而不是“脚本失败/服务不可用”。"""
    from lxserver_client import UnsafeVmRequired

    async def need_unsafe(filename, content, **kwargs):
        raise UnsafeVmRequired("脚本需要 unsafe VM 运行环境；本次未验证")

    fake_lx.upload_custom_source = need_unsafe
    response = test_app_client.post(
        "/api/v1/source/upload", json={"filename": "x.js", "script": "console.log(1)"})
    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["category"] == "unsafe_vm_required"
    assert payload["data"]["requires_unsafe_vm"] is True
    assert payload["data"]["verified"] is False
    # 不留下半成品激活状态
    assert fake_lx.toggle_calls == []
