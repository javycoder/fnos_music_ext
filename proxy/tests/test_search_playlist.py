"""歌单搜索注入：/music/api/v1/search/playlist 拦截后扩展歌单按名匹配追加。

官方自建歌单能被搜到（search/playlist 查官方库），扩展歌单（推荐/网易账号/
公开）不在官方库搜不到——拦截端点按歌单名子串匹配注入本地缓存卡片。
"""
import importlib
import json

import pytest
from starlette.testclient import TestClient

p = importlib.import_module("proxy.app")


@pytest.fixture
def client(monkeypatch, tmp_path):
    for key in ("cache_dir", "library_dir", "fav_dir"):
        monkeypatch.setitem(p.CONF, key, str(tmp_path / key))
    monkeypatch.setitem(p.CONF, "music_db", str(tmp_path / "missing.db"))
    monkeypatch.setitem(p.CONF, "tee_save_dir", "")
    monkeypatch.setattr(p, "_TEE_SAVE_DIR_WARNED", False)
    # 官方信封：模拟官方返回一个自建歌单 + 已登录
    async def fake_envelope(request, client_):
        return {"code": 0, "msg": "ok", "data": {"list": [
            {"guid": "official-1", "name": "我的最爱", "coverId": "c1",
             "createdAt": 1, "updatedAt": 1, "trackCount": 3},
        ], "total": 1}}

    async def fake_probe(request, client_):
        return True, "user-abc", None

    monkeypatch.setattr(p, "fetch_upstream_envelope", fake_envelope)
    monkeypatch.setattr(p, "_probe_upstream_auth", fake_probe)
    app = p.app
    return TestClient(app)


def _search(client, kw):
    r = client.get(f"/music/api/v1/search/playlist?q={kw}")
    assert r.status_code == 200
    return r.json()


def test_official_result_preserved_and_ext_appended(client):
    """官方自建歌单原样保留；名字命中的扩展歌单追加在尾部。"""
    data = _search(client, "每日推荐")["data"]
    names = [x["name"] for x in data["list"]]
    assert names[0] == "我的最爱"  # 官方在前
    assert any("每日推荐" in n for n in names[1:])
    assert data["total"] == len(names)


def test_extension_card_fields_match_official_shape(client):
    """注入卡片字段与官方 Tj 消费形态对齐（guid/name/coverId/trackCount...）。"""
    data = _search(client, "猜你喜欢")["data"]
    hit = next(x for x in data["list"] if x["name"] == "猜你喜欢")
    for key in ("guid", "name", "coverId", "createdAt", "updatedAt", "trackCount"):
        assert key in hit, key
    # 修复1：扩展卡片豁免伪装，guid 保留 online: 原文（与歌单列表注入同构，
    # 不依赖内存反解表，重启后详情仍可达）
    assert hit["guid"].startswith("online:playlist:liked:")
    assert hit["guid"] == p.dailyrec.recommend_playlist_guid("liked", user_guid="user-abc")
    # coverId 伪装成官方形态（track_ 前缀 + 32hex）
    assert hit["coverId"].startswith("track_")


def test_no_keyword_pure_passthrough(client):
    """空关键词：官方结果原样返回，不注入。"""
    data = _search(client, "")["data"]
    assert [x["name"] for x in data["list"]] == ["我的最爱"]
    assert data["total"] == 1


def test_no_match_no_injection(client):
    """关键词无命中：官方结果原样返回。"""
    data = _search(client, "不存在的歌单名")["data"]
    assert [x["name"] for x in data["list"]] == ["我的最爱"]


def test_recommend_switch_gates_injection(client, monkeypatch):
    """recommend_daily 关闭时每日推荐不进搜索结果。"""
    monkeypatch.setitem(p.CONF, "recommend_daily", False)
    data = _search(client, "每日推荐")["data"]
    assert not any("每日推荐" in x["name"] for x in data["list"])


def test_public_playlist_from_cache_only(client, monkeypatch, tmp_path):
    """公开歌单从缓存命中（不发在线请求）；需要 lx 源运行时可用。"""
    monkeypatch.setitem(p.CONF, "public_playlists_enabled", True)
    monkeypatch.setitem(p.CONF, "lx_enabled", True)
    monkeypatch.setitem(p.CONF, "lx_sources", ["s1"])
    monkeypatch.setitem(p.CONF, "public_playlists_selection",
                        json.dumps([{"provider": "lx", "source": "s1", "id": "1"}]))
    entry = {"provider": "lx", "source": "s1", "kind": "playlist", "id": "1"}
    guid = p.pubpl.playlist_guid(entry)
    summary = {"name": "华语金曲集", "trackCount": 12, "guid": guid}
    p.pubpl._summary_cache[guid] = (summary, p.time.monotonic() + 3600)
    try:
        data = _search(client, "华语")["data"]
        # 公开歌单卡片名带源标签前缀（与歌单列表注入一致）：[S1] 华语金曲集
        assert any("华语金曲集" in x["name"] for x in data["list"])
    finally:
        p.pubpl._summary_cache.pop(guid, None)


def test_hits_capped_at_limit(client, monkeypatch, tmp_path):
    """公开歌单命中超过上限时截断（防刷屏）。"""
    monkeypatch.setitem(p.CONF, "public_playlists_enabled", True)
    monkeypatch.setitem(p.CONF, "lx_enabled", True)
    monkeypatch.setitem(p.CONF, "lx_sources", ["s1"])
    selection = []
    for i in range(8):
        entry = {"provider": "lx", "source": "s1", "kind": "playlist", "id": str(i)}
        selection.append(entry)
        guid = p.pubpl.playlist_guid(entry)
        summary = {"name": f"合集测试{i}", "trackCount": 1, "guid": guid}
        p.pubpl._summary_cache[guid] = (summary, p.time.monotonic() + 3600)
    monkeypatch.setitem(p.CONF, "public_playlists_selection", json.dumps(selection))
    try:
        data = _search(client, "合集测试")["data"]
        matched = [x for x in data["list"] if "合集测试" in x["name"]]
        assert len(matched) == p._EXT_PLAYLIST_SEARCH_MAX
    finally:
        for entry in selection:
            p.pubpl._summary_cache.pop(p.pubpl.playlist_guid(entry), None)


def test_official_error_envelope_passthrough(client, monkeypatch):
    """官方 401 信封原样透传，不注入不吞语义。"""
    async def fake_envelope(request, client_):
        return {"code": 99999, "msg": "INVALID TOKEN", "data": None}

    monkeypatch.setattr(p, "fetch_upstream_envelope", fake_envelope)
    r = client.get("/music/api/v1/search/playlist?q=每日推荐")
    assert r.status_code == 200
    assert r.json()["code"] == 99999


# ------------------------------------------------ 在线公开歌单搜索层 ----

@pytest.fixture
def online_env(client, monkeypatch):
    """在线层环境：开关开、lx 源可用、清缓存。"""
    monkeypatch.setitem(p.CONF, "public_search_enabled", True)
    monkeypatch.setitem(p.CONF, "lx_enabled", True)
    monkeypatch.setitem(p.CONF, "lx_sources", ["kw"])
    p._public_search_cache.clear()
    yield client
    p._public_search_cache.clear()


def _mock_lx_search(monkeypatch, items, source="kw"):
    async def fake_get(url, params=None, timeout=None):
        class R:
            status_code = 200
            def json(self):
                return {"ok": True, "items": items, "total": len(items)}
        return R()
    class FakeClient:
        def get(self, *a, **k):
            return fake_get(*a, **k)
    monkeypatch.setattr(p, "get_lx_client", lambda app: FakeClient())


@pytest.mark.anyio
async def test_online_search_finds_unadded_playlists(online_env, monkeypatch):
    """未添加的公开歌单经在线搜索参与匹配，卡片带 🔍 前缀。"""
    _mock_lx_search(monkeypatch, [
        {"id": "999", "name": "网络热歌华流", "cover_url": "", "track_count": 50},
    ])
    from starlette.testclient import TestClient
    r = online_env.get("/music/api/v1/search/playlist?q=华流")
    assert r.status_code == 200
    data = r.json()["data"]
    hit = next((x for x in data["list"] if "华流" in x["name"]), None)
    assert hit is not None
    assert hit["name"].startswith("🔍")
    assert hit["trackCount"] == 50
    # guid 可被 entry_from_guid 反解（详情端点按它在线拉摘要）
    entry = p.pubpl.entry_from_guid(p.resolve_real_guid(hit["guid"]))
    assert entry is not None
    assert entry["id"] == "999"
    # coverId 经伪装（disguise 出口处理 online: → 32hex）


@pytest.mark.anyio
async def test_online_search_keyword_cached(online_env, monkeypatch):
    """同一关键词短 TTL 缓存：第二次请求不再打源站。"""
    calls = []
    async def fake_get(url, params=None, timeout=None):
        calls.append(params)
        class R:
            status_code = 200
            def json(self):
                return {"ok": True, "items": [{"id": "1", "name": "x"}], "total": 1}
        return R()
    class FakeClient:
        def get(self, *a, **k):
            return fake_get(*a, **k)
    monkeypatch.setattr(p, "get_lx_client", lambda app: FakeClient())
    online_env.get("/music/api/v1/search/playlist?q=cached")
    online_env.get("/music/api/v1/search/playlist?q=cached")
    assert len(calls) == 1


@pytest.mark.anyio
async def test_online_search_switch_off(online_env, monkeypatch):
    """开关关闭：不发起在线搜索（未添加歌单不出现）。"""
    monkeypatch.setitem(p.CONF, "public_search_enabled", False)
    called = []
    _mock_lx_search(monkeypatch, [])
    monkeypatch.setattr(p, "get_lx_client", lambda app: called.append(1) or (_ for _ in ()).throw(AssertionError("should not call")))
    r = online_env.get("/music/api/v1/search/playlist?q=anything")
    assert r.status_code == 200


@pytest.mark.anyio
async def test_online_search_timeout_does_not_break(online_env, monkeypatch):
    """在线层超时/异常不影响主响应（官方结果 + 缓存命中照常）。"""
    class SlowClient:
        async def get(self, *a, **k):
            await asyncio.sleep(30)
    monkeypatch.setattr(p, "get_lx_client", lambda app: SlowClient())
    r = online_env.get("/music/api/v1/search/playlist?q=slow")
    assert r.status_code == 200
    data = r.json()["data"]
    assert any(x["name"] == "我的最爱" for x in data["list"])


import asyncio  # noqa: E402  (测试文件尾部，供超时用例使用)


# ------------------------------------------------ 修复1/2/3/4 回归 ----

@pytest.mark.anyio
async def test_online_card_raw_guid_survives_restart(online_env, monkeypatch):
    """修复1：在线层卡片 guid 原文下发（不经伪装），详情端点直接反解。
    同时验证重启场景：清空伪装反解表后原文 guid 仍可解析。"""
    _mock_lx_search(monkeypatch, [
        {"id": "888", "name": "修复验证歌单", "cover_url": "http://img.example/x.jpg",
         "track_count": 9},
    ])
    r = online_env.get("/music/api/v1/search/playlist?q=修复验证")
    hit = next(x for x in r.json()["data"]["list"] if "修复验证" in x["name"])
    assert hit["guid"].startswith("online:playlist:public:lx:kw:playlist:888")
    # 反解表清空（模拟重启）：原文 guid 不依赖表，详情端点 entry_from_guid 直接命中
    saved = dict(p._FAKE_GUID_REVERSE)
    p._FAKE_GUID_REVERSE.clear()
    try:
        assert p.pubpl.entry_from_guid(hit["guid"]) is not None
    finally:
        p._FAKE_GUID_REVERSE.update(saved)


@pytest.mark.anyio
async def test_online_card_cover_url_registered(online_env, monkeypatch):
    """修复3：搜索结果自带封面直链登记进 _cover_urls，封面端点可同源直出。"""
    _mock_lx_search(monkeypatch, [
        {"id": "777", "name": "带封面歌单", "cover_url": "http://img.example/c.jpg",
         "track_count": 3},
    ])
    entry = {"provider": "lx", "source": "kw", "kind": "playlist", "id": "777"}
    guid = p.pubpl.playlist_guid(entry)
    try:
        r = online_env.get("/music/api/v1/search/playlist?q=带封面")
        assert any("带封面" in x["name"] for x in r.json()["data"]["list"])
        assert p.pubpl.cover_url_for(guid) == "http://img.example/c.jpg"
    finally:
        p.pubpl._cover_urls.pop(guid, None)


@pytest.mark.anyio
async def test_online_card_no_cover_marked_unavailable(online_env, monkeypatch):
    """修复4：源搜索结果无封面（源不可用征兆）时名字后缀（暂不可用）。"""
    _mock_lx_search(monkeypatch, [
        {"id": "666", "name": "无封面歌单", "cover_url": "", "track_count": 0},
    ])
    r = online_env.get("/music/api/v1/search/playlist?q=无封面")
    hit = next(x for x in r.json()["data"]["list"] if "无封面" in x["name"])
    assert hit["name"].endswith("（暂不可用）")
    assert hit["name"].startswith("🔍")


@pytest.mark.anyio
async def test_track_list_failure_degrades_to_empty(client, monkeypatch):
    """修复2：公开歌单曲目拉取失败返回空列表（code 0），不再 code -1 空白页。"""
    guid = "online:playlist:public:lx:kw:playlist:1"
    entry = p.pubpl.entry_from_guid(guid)

    async def fail_tracks(state, entry_, build_track):
        raise RuntimeError("source unavailable")

    # dev 的按用户 selection 门槛：空 selection 对任何用户返回 False → 提前 -1；
    # 本用例只关注"拉取失败降级"，放行 selection
    monkeypatch.setattr(p.pubpl, "selection_allows",
                        lambda conf, user_guid, entry_: True)
    monkeypatch.setattr(p.pubpl, "load_tracks", fail_tracks)
    # detail 端点响应不受影响
    r = client.get(f"/music/api/v1/track/playlist-detail/list?guid={guid}")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["list"] == []
    assert body["data"]["total"] == 0


@pytest.mark.anyio
async def test_online_card_coverid_resolvable_for_cover_endpoint(online_env, monkeypatch):
    """搜索页封面修复：卡片 coverId 的 fake 注册进反解表，封面端点
    track_<fake> → 原文 guid → 公开歌单封面分支（cover_url_for 直出）。"""
    _mock_lx_search(monkeypatch, [
        {"id": "555", "name": "封面反解歌单", "cover_url": "http://img.example/s.jpg",
         "track_count": 4},
    ])
    entry = {"provider": "lx", "source": "kw", "kind": "playlist", "id": "555"}
    guid = p.pubpl.playlist_guid(entry)
    try:
        r = online_env.get("/music/api/v1/search/playlist?q=封面反解")
        hit = next(x for x in r.json()["data"]["list"] if "封面反解" in x["name"])
        cover_id = hit["coverId"]
        assert cover_id == "track_" + p.fake_official_guid(guid)
        # 反解表已注册（卡片下发时副作用）：封面端点同款解析链可达原文 guid
        resolved = p.resolve_real_guid(cover_id)
        assert resolved == guid
        assert p.pubpl.is_public_playlist_guid(resolved)
        assert p.pubpl.cover_url_for(guid) == "http://img.example/s.jpg"
    finally:
        p.pubpl._cover_urls.pop(guid, None)
