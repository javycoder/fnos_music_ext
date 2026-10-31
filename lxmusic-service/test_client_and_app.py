"""lxmusic-service/test_client_and_app.py
全套契约与单元测试：覆盖字段映射、探活签名、API 契约和容错机制。
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from conftest import lxapp, mock_client
from lxserver_client import (
    LxServerClient,
    extract_track_identifier,
    map_lxserver_song,
    normalize_source,
    parse_interval_to_seconds,
)


# ------------------------------------------------------------- 映射函数单测 --

def test_normalize_source():
    assert normalize_source("kugou") == "kg"
    assert normalize_source("KG") == "kg"
    assert normalize_source("163") == "wy"
    assert normalize_source("netease") == "wy"
    assert normalize_source("kuwo") == "kw"
    assert normalize_source("migu") == "mg"
    assert normalize_source("qq") == "tx"
    assert normalize_source("unknown") == ""
    assert normalize_source(None) == ""


def test_parse_interval_to_seconds():
    assert parse_interval_to_seconds("05:24") == 324
    assert parse_interval_to_seconds("00:45") == 45
    assert parse_interval_to_seconds("01:02:03") == 3723
    assert parse_interval_to_seconds(180) == 180
    assert parse_interval_to_seconds(180.5) == 180
    assert parse_interval_to_seconds("") == 0
    assert parse_interval_to_seconds(None) == 0


def test_map_lxserver_song_kuwo():
    raw = {
        "name": "海阔天空",
        "singer": "Beyond",
        "source": "kw",
        "songmid": "5886682",
        "albumName": "乐与怒",
        "interval": "05:24",
        "img": "https://img.test/pic.jpg",
        "types": [{"type": "128k"}, {"type": "320k"}, {"type": "flac"}],
    }
    item = map_lxserver_song(raw)
    assert item["id"] == "lx:kw:5886682"
    assert item["lx_source"] == "kw"
    assert item["title"] == "海阔天空"
    assert item["artist"] == "Beyond"
    assert item["album"] == "乐与怒"
    assert item["duration_s"] == 324
    assert item["ext"] == "flac"
    assert item["cover_url"] == "https://img.test/pic.jpg"
    assert "flac" in item["types"]


def test_map_lxserver_song_kugou_hash():
    raw = {
        "name": "泡沫",
        "singer": "邓紫棋",
        "source": "kg",
        "hash": "F52899ABCDEF",
        "albumName": "X.P.X",
        "interval": "04:18",
        "types": [{"type": "320k", "hash": "F52899ABCDEF"}],
    }
    item = map_lxserver_song(raw)
    assert item["id"] == "lx:kg:f52899abcdef"
    assert item["hash"] == "f52899abcdef"
    assert item["ext"] == "mp3"


def test_map_lxserver_song_migu_copyright():
    raw = {
        "name": "稻香",
        "singer": "周杰伦",
        "source": "mg",
        "copyrightId": "60054701983",
        "interval": 223,
    }
    item = map_lxserver_song(raw)
    assert item["id"] == "lx:mg:60054701983"
    assert item["copyrightId"] == "60054701983"
    assert item["duration_s"] == 223


# ------------------------------------------------------------- 探活与魔数单测 --

def test_media_signature():
    assert lxapp._media_signature(b"fLaC\x00\x00") == "flac"
    assert lxapp._media_signature(b"ID3\x04\x00") == "mp3"
    assert lxapp._media_signature(b"OggS\x00\x02") == "ogg"
    assert lxapp._media_signature(b"RIFF\x00\x00\x00\x00WAVE") == "wav"
    assert lxapp._media_signature(b"\x00\x00\x00 ftypM4A ") == "m4a"
    assert lxapp._media_signature(b"\xff\xf1\x50\x80") == "aac"
    assert lxapp._media_signature(b"<html>error</html>") == ""


@pytest.mark.asyncio
async def test_probe_url_flac_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
            content=b"fLaC" + b"\x00" * 4092,
        )

    client = mock_client(handler)
    try:
        ok, final_url, ct, size = await lxapp.probe_url(client, "https://media.test/track.flac")
        assert ok is True
        assert ct == "audio/flac"
        assert size == 30000000
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_probe_url_html_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>Denied</html>")

    client = mock_client(handler)
    try:
        ok, _, _, _ = await lxapp.probe_url(client, "https://media.test/track.mp3")
        assert ok is False
    finally:
        await client.aclose()


# ------------------------------------------------------------- API 契约测试 --

def test_healthz_endpoint(test_app_client):
    res = test_app_client.get("/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["service"] == "fnmusic-lxmusic"
    assert "capabilities" in data
    assert "user_source" in data
    assert "charts" in data
    assert data["user_source"]["configured"] is True


def test_healthz_reports_lxserver_failure(test_app_client, fake_lx):
    fake_lx.alive = False
    res = test_app_client.get("/healthz")
    assert res.status_code == 503
    data = res.json()
    assert data["ok"] is False
    assert data["error"] == "lxserver unreachable"
    assert data["user_source"]["last_error"] == "lxserver unreachable"


def test_search_endpoint_ignores_probe_and_skips_pre_probe(test_app_client, fake_lx, monkeypatch):
    """旧客户端 probe=1 被忽略；搜索结果直接交付，不预解析直链。"""
    def fail_resolve(*args, **kwargs):
        pytest.fail("search must not resolve URLs")

    monkeypatch.setattr(lxapp, "resolve_and_probe", fail_resolve)
    res = test_app_client.get("/api/v1/search?q=海阔天空&sources=kw&limit=10&probe=1")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert isinstance(data["items"], list)
    assert len(data["items"]) > 0
    item = data["items"][0]
    assert item["id"] == "lx:kw:5886682"
    assert item["title"] == "海阔天空"
    assert item["artist"] == "Beyond"
    assert item["verified"] is False
    assert item["validation_status"] == "candidate"
    assert fake_lx.url_calls == []


def test_recommend_endpoint(test_app_client):
    res = test_app_client.get("/api/v1/recommend?sources=kw&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert isinstance(data["items"], list)
    # issue #47 回归：bangid 契约修复后榜单不得再静默返回空
    assert data["items"], "recommend 榜单为空：get_leaderboard_list 参数契约或 boards 解析回归"


def test_leaderboard_list_sends_bangid_param():
    """issue #47：lxserver 榜单歌曲接口的参数名是 bangid，发 id 会被上游 400 拒绝
    （"Missing bangid"）且曾被静默吞掉导致榜单全空。用 MockTransport 锁定参数契约。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "total": 1,
            "list": [{
                "name": "真的爱你", "singer": "Beyond", "albumName": "秘密警察",
                "songmid": "346075", "source": "wy", "interval": "04:45", "img": "",
                "types": [{"type": "128k", "size": "4.5 MB"}],
            }],
        })

    client = LxServerClient(base_url="http://lxserver-test")

    async def _run():
        client._client = httpx.AsyncClient(
            base_url=client.base_url,
            transport=httpx.MockTransport(handler),
            timeout=5.0,
        )
        try:
            return await client.get_leaderboard_list("wy", "3779629", page=1)
        finally:
            await client.close()

    items = asyncio.run(_run())
    assert seen["params"].get("bangid") == "3779629"
    assert "id" not in seen["params"]
    assert seen["params"].get("source") == "wy"
    assert seen["params"].get("page") == "1"
    assert len(items) == 1
    assert items[0]["id"] == "lx:wy:346075"
    assert items[0]["title"] == "真的爱你"
    assert items[0]["duration_s"] == 285


def test_leaderboard_list_non200_returns_empty():
    """issue #47：非 200 响应（如上游 400/500）返回空列表且不抛异常——
    修复前该路径无日志静默吞错，如今失败至少可见（client 侧有 warning）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Missing bangid")

    client = LxServerClient(base_url="http://lxserver-test")

    async def _run():
        client._client = httpx.AsyncClient(
            base_url=client.base_url,
            transport=httpx.MockTransport(handler),
            timeout=5.0,
        )
        try:
            return await client.get_leaderboard_list("kg", "8888", page=1)
        finally:
            await client.close()

    assert asyncio.run(_run()) == []


def test_public_leaderboard_error_is_not_an_empty_page():
    client = LxServerClient(base_url="http://lxserver-test")
    async def run():
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(lambda request: httpx.Response(500)))
        try:
            with pytest.raises(RuntimeError, match="500"):
                await client.get_leaderboard_list("kg", "8888", strict=True)
        finally:
            await client.close()
    asyncio.run(run())


def test_song_list_client_contracts_preserve_raw_ids():
    """歌单广场：tag/list/detail 契约与原始 ID（含非数字形态）保持不变。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "params": dict(request.url.params)})
        path = request.url.path
        if path == "/api/music/songList/tags":
            return httpx.Response(200, json={
                "tags": [{"name": "分类", "list": [{"id": "id_abc", "name": "推荐"}]}],
                "hotTag": [{"id": "hot", "name": "热门"}],
                "sortList": [{"id": "5", "name": "推荐"}], "source": "kg",
            })
        if path == "/api/music/songList/list":
            return httpx.Response(200, json={
                "list": [{"id": "id_abc", "name": "精选歌单", "img": "http://img/kg.jpg",
                          "total": 20, "author": "酷狗"}],
                "total": 1, "limit": 30, "source": "kg",
            })
        if path == "/api/music/songList/detail":
            return httpx.Response(200, json={
                "list": [{"name": "晴天", "singer": "周杰伦", "songmid": "ABC",
                          "source": "kg", "hash": "ABC", "interval": "04:29"}],
                "total": 1, "limit": 1000,
                "info": {"name": "精选歌单", "img": "http://img/kg.jpg", "desc": "", "author": "酷狗"},
            })
        return httpx.Response(404)

    client = LxServerClient(base_url="http://lxserver-test")

    async def _run():
        client._client = httpx.AsyncClient(
            base_url=client.base_url, transport=httpx.MockTransport(handler), timeout=5.0,
        )
        try:
            tags = await client.get_song_list_tags("kg")
            rows = await client.get_song_lists("kg", "id_abc", "5", 1)
            detail = await client.get_song_list_detail("kg", "id_abc", 1)
            return tags, rows, detail
        finally:
            await client.close()

    tags, rows, detail = asyncio.run(_run())
    assert seen[1]["params"] == {"source": "kg", "tagId": "id_abc", "sortId": "5", "page": "1"}
    assert seen[2]["params"] == {"source": "kg", "id": "id_abc", "page": "1"}
    summary = client.normalize_public_playlist(rows["list"][0], "kg")
    assert summary["id"] == "id_abc" and summary["track_count"] == 20
    assert detail["info"]["name"] == "精选歌单"
    chart = client.normalize_public_playlist({"id": "kg__8888", "bangid": "8888", "name": "TOP500"}, "kg", "chart")
    assert chart["id"] == "8888"


def test_song_list_search_client_contract():
    """歌单搜索：source/text/page 契约、字符串 ID 透传、非 200 与非法载荷报错。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "params": dict(request.url.params)})
        return httpx.Response(200, json={
            "list": [{"id": "kw_pl_9", "name": "华语精选", "img": "http://img/kw.jpg",
                      "total": 33, "author": "酷我用户", "source": "kw"}],
            "limit": 20, "total": 57, "source": "kw",
        })

    client = LxServerClient(base_url="http://lxserver-test")

    async def _run():
        client._client = httpx.AsyncClient(
            base_url=client.base_url, transport=httpx.MockTransport(handler), timeout=5.0,
        )
        try:
            data = await client.get_song_list_search("kw", " 华语 ", 3)
        finally:
            await client.close()
        return data

    data = asyncio.run(_run())
    assert seen == [{"path": "/api/music/songList/search",
                     "params": {"source": "kw", "text": "华语", "page": "3"}}]
    summary = client.normalize_public_playlist(data["list"][0], "kw")
    assert summary["id"] == "kw_pl_9" and summary["track_count"] == 33
    assert summary["name"] == "华语精选" and summary["cover_url"] == "http://img/kw.jpg"


def test_song_list_search_client_rejects_bad_input_and_payloads():
    client = LxServerClient(base_url="http://lxserver-test")

    with pytest.raises(ValueError, match="keyword"):
        asyncio.run(client.get_song_list_search("kw", "   ", 1))
    with pytest.raises(ValueError, match="source"):
        asyncio.run(client.get_song_list_search("xm", "华语", 1))

    def bad_status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    def bad_payload(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"list": "nope"})

    async def _with(handler, fn):
        client._client = httpx.AsyncClient(
            base_url=client.base_url, transport=httpx.MockTransport(handler), timeout=5.0,
        )
        try:
            await fn()
        finally:
            await client.close()

    with pytest.raises(RuntimeError, match="500"):
        asyncio.run(_with(bad_status, lambda: client.get_song_list_search("kw", "华语", 1)))
    with pytest.raises(RuntimeError, match="invalid payload"):
        asyncio.run(_with(bad_payload, lambda: client.get_song_list_search("kw", "华语", 1)))


def test_public_playlist_search_maps_and_infers_more(test_app_client, fake_lx):
    async def search(source, text, page=1):
        if not text.strip():
            raise ValueError("empty search keyword")  # 与真实客户端契约一致 → 400
        assert source == "kw" and text == "华语"
        if page == 1:
            return {"list": [{"id": "kw_pl_9", "name": "华语精选", "total": 33,
                              "img": "http://img/kw.jpg", "author": "酷我用户"}],
                    "limit": 20, "total": 57, "source": "kw"}
        return {"list": [], "limit": 20, "total": 57, "source": "kw"}

    fake_lx.get_song_list_search = search
    fake_lx.normalize_public_playlist = LxServerClient.normalize_public_playlist
    res = test_app_client.get(
        "/api/v1/public-playlists",
        params={"source": "kw", "channel": "search", "keyword": "华语", "page": 1},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True and data["total"] == 57 and data["has_more"] is True
    assert data["items"][0]["id"] == "kw_pl_9" and data["items"][0]["track_count"] == 33
    assert data["items"][0]["name"] == "华语精选"

    # 上游固定页大小 20：第 3 页已越过 total=57，即使空页也不再有下一页
    page3 = test_app_client.get(
        "/api/v1/public-playlists",
        params={"source": "kw", "channel": "search", "keyword": "华语", "page": 3},
    ).json()
    assert page3["has_more"] is False and page3["items"] == []

    # 深页（原 100 页限制已解除）与缺关键词均为合法路径：深页透传，缺关键词 400
    deep = test_app_client.get(
        "/api/v1/public-playlists",
        params={"source": "kw", "channel": "search", "keyword": "华语", "page": 150},
    )
    assert deep.status_code == 200
    missing = test_app_client.get(
        "/api/v1/public-playlists", params={"source": "kw", "channel": "search"},
    )
    assert missing.status_code == 400 and missing.json()["ok"] is False


@pytest.mark.parametrize("total", [0, 99999])
def test_public_playlist_search_empty_raw_page_stops(test_app_client, fake_lx, total):
    async def search(source, text, page=1):
        return {"list": [], "limit": 20, "total": total, "has_more": True}

    fake_lx.get_song_list_search = search
    fake_lx.normalize_public_playlist = LxServerClient.normalize_public_playlist
    response = test_app_client.get("/api/v1/public-playlists", params={
        "source": "kw", "channel": "search", "keyword": "华语", "page": 2,
    })
    assert response.status_code == 200
    result = response.json()
    assert result["items"] == [] and result["raw_count"] == 0
    assert result["total"] == total and result["has_more"] is False


def test_public_playlist_search_filtered_window_keeps_native_pagination(test_app_client, fake_lx):
    pages = []

    async def search(source, text, page=1):
        pages.append(page)
        return {"list": [{}, None], "limit": 20, "total": 57}

    fake_lx.get_song_list_search = search
    fake_lx.normalize_public_playlist = LxServerClient.normalize_public_playlist
    for page, more in [(2, True), (3, False)]:
        result = test_app_client.get("/api/v1/public-playlists", params={
            "source": "kw", "channel": "search", "keyword": "华语", "page": page,
        }).json()
        assert result["items"] == [] and result["raw_count"] == 2
        assert result["has_more"] is more
    assert pages == [2, 3]


def test_public_chart_tracks_follow_native_pages(test_app_client, fake_lx):
    calls: list[int] = []

    async def paged(source, board_id, page=1, *, strict=False):
        assert strict is True
        assert source == "kg" and board_id == "8888"
        calls.append(page)
        if page > 2:
            return []
        return [{"id": f"lx:kg:song-{page}", "title": f"歌曲{page}", "lx_source": "kg"}]

    fake_lx.get_leaderboard_list = paged
    res = test_app_client.get(
        "/api/v1/public-playlists/8888/tracks",
        params={"source": "kg", "kind": "chart"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True and data["total"] == 2 and data["truncated"] is False
    assert [item["title"] for item in data["data"]] == ["歌曲1", "歌曲2"]
    assert calls == [1, 2, 3]


def test_public_chart_stops_when_platform_repeats_page(test_app_client, fake_lx):
    calls = []
    async def repeated(source, board_id, page=1, *, strict=False):
        calls.append(page)
        return [{"id": "lx:wy:101", "title": "同一首歌", "lx_source": "wy"}]
    fake_lx.get_leaderboard_list = repeated
    result = test_app_client.get("/api/v1/public-playlists/3779629/tracks", params={"source": "wy", "kind": "chart"}).json()
    assert result["total"] == 1 and result["truncated"] is False
    assert calls == [1, 2]


def test_same_source_discovery_requests_are_serialized():
    active = 0
    overlapped = False

    class GatedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal active, overlapped
            active += 1
            if active > 1:
                overlapped = True
            await asyncio.sleep(0)
            active -= 1
            return httpx.Response(200, json={"list": [], "info": {}, "total": 0, "limit": 30})

    client = LxServerClient(base_url="http://lxserver-test")

    async def run():
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=GatedTransport(), timeout=5.0)
        try:
            await asyncio.gather(
                client.get_song_list_detail("kg", "id_abc", 1),
                client.get_song_list_detail("kg", "id_def", 2),
            )
        finally:
            await client.close()

    asyncio.run(run())
    assert overlapped is False


def test_track_info_endpoint(test_app_client):
    # 未缓存条目返回合成数据
    res = test_app_client.get("/api/v1/track/info?id=lx:kw:5886682")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["data"]["id"] == "lx:kw:5886682"
    assert data["data"]["lx_source"] == "kw"


def test_track_lyric_endpoint(test_app_client):
    res = test_app_client.get("/api/v1/track/lyric?id=lx:kw:5886682")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "[00:00.00]" in data["data"]["lyric"]


def test_source_management_endpoints(test_app_client, fake_lx):
    # GET /api/v1/source
    res = test_app_client.get("/api/v1/source")
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # POST /api/v1/source/upload —— 返回 lxserver 真实存储路径（id 由 @name 派生）
    res = test_app_client.post(
        "/api/v1/source/upload",
        json={"filename": "my_src.js", "script": "/*!\n * @name MyTest\n */\nconsole.log(1);"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    data = res.json()["data"]
    assert data["url"] == "file:///data/lxserver/users/source/_open/MyTest.js"
    assert data["meta"]["name"] == "MyTest"

    # 同一脚本重复上传：复用既有源，不报错（多源列表的常规操作）
    res = test_app_client.post(
        "/api/v1/source/upload",
        json={"filename": "my_src.js", "script": "/*!\n * @name MyTest\n */\nconsole.log(1);"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["url"] == data["url"]

    # POST /api/v1/source（激活上传返回的 file:// URL）
    res = test_app_client.post("/api/v1/source", json={"url": data["url"]})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    # 多源叠加语义：目标启用、既有激活源保持不变
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("MyTest.js") is True
    assert states.get("source1") is True

    # enabled=false：停用目标源，其余不受影响
    res = test_app_client.post("/api/v1/source", json={"url": data["url"], "enabled": False})
    assert res.status_code == 200
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("MyTest.js") is False
    assert states.get("source1") is True

    # describe 返回全部启用源列表
    res = test_app_client.get("/api/v1/source")
    src_data = res.json()["data"]
    assert src_data["configured"] is True
    assert src_data["active_count"] == 1
    assert [s["id"] for s in src_data["sources"]] == ["source1"]

    # DELETE /api/v1/source (清除)
    res = test_app_client.delete("/api/v1/source")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_source_set_activates_unknown_local_file_by_import(test_app_client, fake_lx, tmp_path):
    """旧数据卷路径（如 /data/lxmusic/uploads）不在 lxserver 列表时自动补导入再激活。"""
    script_path = tmp_path / "legacy_src.js"
    script_path.write_text("/*!\n * @name LegacySrc\n */\nconsole.log(1);", encoding="utf-8")
    res = test_app_client.post("/api/v1/source", json={"url": f"file://{script_path}"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    # 已按派生 id 导入并激活
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("LegacySrc.js") is True


def test_source_set_rejects_missing_source(test_app_client):
    res = test_app_client.post("/api/v1/source", json={"url": "file:///nonexistent/nope.js"})
    assert res.status_code == 500
    assert "未找到要激活的源" in res.json()["error"]


def test_upload_classifies_lxserver_unreachable_as_service_error(test_app_client, fake_lx):
    """连不上 lxserver 必须报服务不可用，而不是 download（换脚本无效，issue #78）。"""
    import httpx as _httpx

    async def boom(filename, script_content):
        raise _httpx.ConnectError("All connection attempts failed")

    fake_lx.upload_custom_source = boom
    res = test_app_client.post("/api/v1/source/upload",
                               json={"filename": "x.js", "script": "/* @name X */"})
    assert res.status_code == 503
    body = res.json()
    assert body["category"] == "service_unavailable"
    assert body["stage"] == "lxmusic_to_lxserver"
    assert "lxserver 不可连接" in body["error"]


def test_upload_keeps_download_category_for_real_failures(test_app_client, fake_lx):
    """lxserver 可达但拒绝上传（如脚本不合法）：仍按 download/脚本问题归类。"""

    async def reject(filename, script_content):
        raise RuntimeError("脚本内容不合法")

    fake_lx.upload_custom_source = reject
    res = test_app_client.post("/api/v1/source/upload",
                               json={"filename": "x.js", "script": "/* @name X */"})
    assert res.status_code == 500
    assert res.json()["category"] == "download"


def test_source_set_classifies_lxserver_unreachable(test_app_client, fake_lx):
    """激活源时 lxserver 掉线同样报服务不可用，不误导为源失效。"""
    import httpx as _httpx

    async def boom(url):
        raise _httpx.ConnectError("All connection attempts failed")

    fake_lx.import_custom_source = boom
    res = test_app_client.post("/api/v1/source", json={"url": "https://s/1.js"})
    assert res.status_code == 503
    assert res.json()["category"] == "service_unavailable"
    assert res.json()["stage"] == "lxmusic_to_lxserver"


@pytest.mark.parametrize("operation", ["upload_duplicate", "activate_duplicate", "script"])
def test_source_management_reports_connection_loss(test_app_client, fake_lx, operation):
    async def disconnected(*args, **kwargs):
        raise httpx.ConnectError("All connection attempts failed")

    async def duplicate(*args, **kwargs):
        raise RuntimeError('源 "X" 已存在于 [open]')

    if operation == "script":
        fake_lx.upload_custom_source = disconnected
        path, payload = "/api/v1/source", {"script": "/* @name X */"}
    else:
        fake_lx.list_custom_sources = disconnected
        if operation == "upload_duplicate":
            fake_lx.upload_custom_source = duplicate
            path, payload = "/api/v1/source/upload", {"filename": "x.js", "script": "/* @name X */"}
        else:
            fake_lx.import_custom_source = duplicate
            path, payload = "/api/v1/source", {"url": "https://s/1.js"}
    res = test_app_client.post(path, json=payload)
    assert res.status_code == 503, res.text
    assert res.json()["category"] == "service_unavailable"
    assert res.json()["stage"] == "lxmusic_to_lxserver"
    assert not fake_lx.toggle_calls


@pytest.mark.parametrize("error", [RuntimeError("脚本不合法"), PermissionError("local file")])
def test_script_activation_preserves_script_error_semantics(test_app_client, fake_lx, error):
    async def reject(*args):
        raise error

    fake_lx.upload_custom_source = reject
    res = test_app_client.post("/api/v1/source", json={"script": "bad script"})
    assert res.status_code == 500
    assert res.json()["category"] == "download"
    assert str(error) in res.json()["error"]
    assert not fake_lx.toggle_calls


@pytest.mark.parametrize("error", [
    httpx.ConnectError("offline"), httpx.ReadError("read"), httpx.WriteError("write"),
    httpx.ConnectTimeout("connect"), httpx.ReadTimeout("read"),
    httpx.WriteTimeout("write"), httpx.PoolTimeout("pool"),
    httpx.RemoteProtocolError("protocol"),
])
def test_internal_http_transport_failure_classification(error):
    from lxserver_client import is_lxserver_unreachable

    assert is_lxserver_unreachable(error)


@pytest.mark.parametrize("error", [
    OSError("disk"), FileNotFoundError("missing"), PermissionError("denied"),
    RuntimeError("脚本不合法"),
])
def test_local_and_script_errors_are_not_internal_http_failures(error):
    from lxserver_client import is_lxserver_unreachable

    assert not is_lxserver_unreachable(error)


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("operation", ["list", "upload", "toggle"])
def test_lxserver_http_service_failures_are_not_empty_sources(status, operation):
    from lxserver_client import LxServerClient, is_lxserver_unreachable

    async def run():
        client = LxServerClient(base_url="http://test", admin_password="pw")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(status, text="unavailable")),
            base_url="http://test",
        )
        try:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                if operation == "list":
                    await client.list_custom_sources()
                elif operation == "upload":
                    await client.upload_custom_source("x.js", "script")
                else:
                    await client.toggle_custom_source("x.js", True)
            assert is_lxserver_unreachable(caught.value)
        finally:
            await client.close()

    asyncio.run(run())


def test_activation_list_503_returns_service_failure(test_app_client, fake_lx, monkeypatch):
    from lxserver_client import LxServerClient

    async def run_list():
        client = LxServerClient(base_url="http://test", admin_password="pw")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(503, json={"error": "offline"})),
            base_url="http://test",
        )
        try:
            return await client.list_custom_sources()
        finally:
            await client.close()

    monkeypatch.setattr(fake_lx, "list_custom_sources", run_list)
    monkeypatch.setattr(fake_lx, "set_source_enabled",
                        LxServerClient.set_source_enabled.__get__(fake_lx))
    monkeypatch.setattr(fake_lx, "mgmt_lock", asyncio.Lock(), raising=False)
    before = [dict(s) for s in fake_lx.sources]
    res = test_app_client.post("/api/v1/source", json={"url": "file:///x/source1"})
    assert res.status_code == 503
    assert res.json()["category"] == "service_unavailable"
    assert res.json()["stage"] == "lxmusic_to_lxserver"
    assert fake_lx.sources == before
    assert not fake_lx.toggle_calls


def test_lxserver_upload_sends_json_contract():
    """上传必须发 JSON {filename, content, allowUnsafeVM}（lxserver 端点 JSON.parse，multipart 会被拒）。

    安全契约：第三方脚本不受信任，allowUnsafeVM 默认显式 false，不再代答
    “原生 VM 模式”确认；需要 VM 的脚本按 unsafe_vm_required 明确报告。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"success": True, "id": "x.js", "metadata": {"name": "x"}})

    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test", admin_password="pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    res = asyncio.run(client.upload_custom_source("x.js", "console.log(1)"))
    assert captured["content_type"].startswith("application/json")
    assert captured["body"] == {"filename": "x.js", "content": "console.log(1)",
                                "allowUnsafeVM": False}
    assert res["id"] == "x.js"


def test_lxserver_import_sends_allow_unsafe_vm():
    """URL 导入同样默认 allowUnsafeVM=False，不自动授权不安全 VM。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"success": True, "id": "imp.js", "metadata": {"name": "imp"}})

    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test", admin_password="pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    res = asyncio.run(client.import_custom_source("https://example.com/src.js"))
    assert captured["body"] == {"url": "https://example.com/src.js", "allowUnsafeVM": False}
    assert res["id"] == "imp.js"


def test_lxserver_admin_resp_semantics():
    """管理端点失败语义：success=false / HTTP 500 抛 RuntimeError 带服务端文案。"""
    from lxserver_client import LxServerClient

    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": '源 "x" 已存在于 [open]'})

    client = LxServerClient(base_url="http://test", admin_password="pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(fail_handler), base_url="http://test")
    with pytest.raises(RuntimeError, match="已存在"):
        asyncio.run(client.upload_custom_source("x.js", "s"))

    def server_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False, "error": "boom"})

    client2 = LxServerClient(base_url="http://test", admin_password="pw")
    client2._client = httpx.AsyncClient(transport=httpx.MockTransport(server_error_handler), base_url="http://test")
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(client2.toggle_custom_source("x.js", True))


def test_lxserver_toggle_sends_enabled_key():
    """toggle 必须用 enabled 键（lxserver 端点不认识 enable，会退化成布尔翻转）。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"success": True, "enabled": True})

    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test", admin_password="pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    assert asyncio.run(client.toggle_custom_source("MyTest.js", True)) is True
    assert captured["body"] == {"id": "MyTest.js", "enabled": True}


def test_track_url_resolution(test_app_client):
    # 模拟探测成功的直链返回
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
            content=b"fLaC" + b"\x00" * 4092,
        )

    # 替换全局 client 进行探活
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(handler)
    try:
        res = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["data"]["id"] == "lx:kw:5886682"
        assert data["data"]["ext"] == "flac"
        assert data["data"]["actual_tier"] == "lossless"
        assert "headers" in data["data"]
    finally:
        lxapp.app.state.client = orig_client


def test_verify_source_json_format_guard():
    from verify_source import _looks_like_json_source

    assert _looks_like_json_source('{"api": "https://test.com"}') is True
    assert _looks_like_json_source('[{"api": 1}]') is True
    assert _looks_like_json_source("/* @name test */ console.log(1);") is False


@pytest.mark.parametrize("stage", ["search", "resolve"])
@pytest.mark.parametrize("failure", ["connect", 503, 500])
def test_verify_real_client_search_resolve_failures(test_app_client, monkeypatch, stage, failure):
    import verify_source
    from lxserver_client import LxServerClient

    calls = []
    failed_path = "/api/music/search" if stage == "search" else "/api/music/url"

    def handler(request):
        path = request.url.path
        calls.append(path)
        if path == failed_path:
            if failure == "connect":
                raise httpx.ConnectError("internal offline", request=request)
            return httpx.Response(failure, json={"error": "upstream script failed"})
        if path.endswith("/upload"):
            return httpx.Response(200, json={"success": True, "id": "temp"})
        if path.endswith("/list"):
            return httpx.Response(200, json=[
                {"id": "temp", "name": "X", "supportedSources": ["kw"]}])
        if path == "/api/music/search":
            return httpx.Response(200, json=[
                {"name": "Song", "singer": "Artist", "songmid": "1", "source": "kw"}])
        return httpx.Response(200, json={"success": True})

    def factory(**kwargs):
        client = LxServerClient(**kwargs)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=client.base_url)
        return client

    async def download(*args):
        return "/*! @name X */"

    async def no_wait(*args):
        pass

    monkeypatch.setattr(verify_source, "_new_temp_client", factory)
    monkeypatch.setattr(verify_source, "download_script", download)
    monkeypatch.setattr(verify_source.asyncio, "sleep", no_wait)
    res = test_app_client.post("/api/v1/source/verify", json={"url": "https://external/x.js"})
    data = res.json()["data"]
    if failure == 500:
        assert res.status_code == 200
        assert data["category"] == "resolve"
        assert data["attempts"]
    else:
        assert res.status_code == 503
        assert data["category"] == "service_unavailable"
        assert data["stage"] == "lxmusic_to_lxserver"
    assert failed_path in calls
    assert "/api/custom-source/delete" in calls


def test_verify_external_download_failure_is_not_internal(test_app_client, monkeypatch):
    import verify_source

    async def download(*args):
        raise httpx.ConnectError("external script host offline")

    monkeypatch.setattr(verify_source, "download_script", download)
    res = test_app_client.post("/api/v1/source/verify", json={"url": "https://external/x.js"})
    assert res.status_code == 200
    assert res.json()["data"]["category"] == "download"


def test_source_set_external_import_failure_is_not_internal(test_app_client, fake_lx, monkeypatch):
    from lxserver_client import LxServerClient

    async def import_source(url):
        client = LxServerClient(base_url="http://internal", admin_password="pw")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(
                500, json={"error": "下载脚本失败: ECONNREFUSED"})),
            base_url="http://internal")
        try:
            return await client.import_custom_source(url)
        finally:
            await client.close()

    monkeypatch.setattr(fake_lx, "import_custom_source", import_source)
    res = test_app_client.post("/api/v1/source", json={"url": "https://external/x.js"})
    assert res.status_code == 500
    assert res.json()["category"] == "runtime"
    assert "下载脚本失败" in res.json()["error"]
    assert not fake_lx.toggle_calls


@pytest.mark.parametrize("failure_stage", ["upload", "list", "toggle", "search", "resolve"])
def test_verify_reports_unreachable_lxserver(test_app_client, tmp_path, monkeypatch, failure_stage):
    """校验时连不上 lxserver：报 service_unavailable，不当成脚本问题（issue #78）。"""
    import httpx as _httpx
    import verify_source

    closed = []
    deleted = []

    def fail(stage):
        if failure_stage == stage:
            raise _httpx.ConnectError("All connection attempts failed")

    class DeadClient:
        """在各校验阶段断连，确保都不会退化成脚本 resolve 失败。"""

        async def upload_custom_source(self, filename, script_content):
            fail("upload")
            return {"id": "temp"}

        async def list_custom_sources(self):
            fail("list")
            return [{"id": "temp", "name": "X", "supportedSources": ["kw"]}]

        async def toggle_custom_source(self, source_id, enabled):
            fail("toggle")

        async def search(self, *args, **kwargs):
            fail("search")
            return [{"id": "lx:kw:1"}]

        def get_cached_song_info(self, *args):
            return {"songmid": "1"}

        async def get_music_url(self, *args, **kwargs):
            fail("resolve")

        async def delete_custom_source(self, source_id):
            deleted.append(source_id)

        async def close(self):
            closed.append(True)

    async def no_wait(*args):
        pass

    monkeypatch.setattr(verify_source, "_new_temp_client", lambda **kw: DeadClient())
    monkeypatch.setattr(verify_source.asyncio, "sleep", no_wait)
    script = tmp_path / "x.js"
    script.write_text("/*!\n * @name X\n */\nconsole.log(1);", encoding="utf-8")
    res = test_app_client.post("/api/v1/source/verify", json={"url": f"file://{script}"})
    assert closed == [True]
    assert deleted == ([] if failure_stage == "upload" else ["temp"])
    assert res.status_code == 503
    data = res.json()["data"]
    assert data["category"] == "service_unavailable"
    assert data["stage"] == "lxmusic_to_lxserver"
    assert "lxserver 不可连接" in data["message"]


def test_source_set_http_url_activates_target(test_app_client, fake_lx):
    """测试通过 HTTP URL 设置源，能够正确导入并激活该源（多源叠加，不动既有源）。"""
    res = test_app_client.post("/api/v1/source", json={"url": "https://example.com/remote_source.js"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert "https://example.com/remote_source.js" in fake_lx.import_calls
    # 验证目标源已被激活，旧激活源保持不变
    states = {s["id"]: s["enabled"] for s in fake_lx.sources}
    assert states.get("remote_source.js") is True
    assert states.get("source1") is True


def test_activate_nonexistent_source_does_not_disable_existing():
    """激活不存在的目标源时，不应当误将当前运行中的源全部禁用。"""
    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test")
    # 模拟已有激活源
    sources = [
        {"id": "source1", "name": "source1", "enabled": True},
        {"id": "source2", "name": "source2", "enabled": False},
    ]
    toggled = []

    async def fake_list():
        return list(sources)

    async def fake_toggle(sid, enable):
        toggled.append((sid, enable))
        return True

    client.list_custom_sources = fake_list
    client.toggle_custom_source = fake_toggle

    ok = asyncio.run(client.set_source_enabled("non_existent_source", True))
    assert ok is False
    # 没有任何 toggle 被执行，旧源保持原样
    assert toggled == []


def test_set_source_enabled_overlay_semantics():
    """set_source_enabled 只改目标源启用态，多源可同时启用。"""
    from lxserver_client import LxServerClient

    client = LxServerClient(base_url="http://test")
    sources = [
        {"id": "source1", "name": "source1", "enabled": True},
        {"id": "source2", "name": "source2", "enabled": False},
    ]
    toggled = []

    async def fake_list():
        return list(sources)

    async def fake_toggle(sid, enable):
        toggled.append((sid, enable))
        for s in sources:
            if s["id"] == sid:
                s["enabled"] = enable
        return True

    client.list_custom_sources = fake_list
    client.toggle_custom_source = fake_toggle

    assert asyncio.run(client.set_source_enabled("source2", True)) is True
    assert toggled == [("source2", True)]
    assert {s["id"]: s["enabled"] for s in sources} == {"source1": True, "source2": True}

    # 支持按名称匹配
    assert asyncio.run(client.set_source_enabled("source1", False)) is True
    assert toggled == [("source2", True), ("source1", False)]


def test_source_capabilities_union_of_enabled_sources():
    """多源同时激活时 capabilities 按启用源平台并集判定。"""
    from app import source_capabilities
    import app as lxapp

    orig_lx = lxapp.LXSERVER
    try:
        class MultiClient:
            async def is_alive(self):
                return True

            async def list_custom_sources(self):
                return [
                    {"id": "a.js", "name": "a", "enabled": True, "status": "success", "supportedSources": ["kw", "kg"]},
                    {"id": "b.js", "name": "b", "enabled": True, "status": "success", "supportedSources": ["wy", "tx"]},
                    {"id": "c.js", "name": "c", "enabled": False, "supportedSources": ["mg"]},
                ]

        lxapp.LXSERVER = MultiClient()
        caps = asyncio.run(source_capabilities())
        assert caps["kw"]["playback_available"] is True
        assert caps["kg"]["playback_available"] is True
        assert caps["wy"]["playback_available"] is True
        assert caps["tx"]["playback_available"] is True
        assert caps["mg"]["playback_available"] is False
        assert "not supported" in caps["mg"]["reason"]
    finally:
        lxapp.LXSERVER = orig_lx


def test_describe_user_source_lists_all_enabled():
    """describe 返回全部启用源（sources 数组 + active_count），兼容字段取第一个。"""
    from app import describe_user_source
    import app as lxapp

    orig_lx = lxapp.LXSERVER
    try:
        class MultiClient:
            async def list_custom_sources(self):
                return [
                    {"id": "a.js", "name": "src-a", "enabled": True, "status": "success", "version": "1.0",
                     "supportedSources": ["kw", "kg"]},
                    {"id": "b.js", "name": "src-b", "enabled": True, "status": "success", "version": "2.0",
                     "supportedSources": ["wy"]},
                    {"id": "c.js", "name": "src-c", "enabled": False, "version": "1.0",
                     "supportedSources": ["mg"]},
                ]

        lxapp.LXSERVER = MultiClient()
        desc = asyncio.run(describe_user_source())
        assert desc["configured"] is True
        assert desc["active_count"] == 2
        assert [s["id"] for s in desc["sources"]] == ["a.js", "b.js"]
        assert [s["platforms"] for s in desc["sources"]] == [["kw", "kg"], ["wy"]]
        assert desc["source"]["name"] == "src-a"
    finally:
        lxapp.LXSERVER = orig_lx


def test_source_capabilities_reflects_failed_status():
    """当激活源状态为 failed 时，playback_available 应为 False 并给出错误原因。"""
    from app import source_capabilities
    import app as lxapp

    orig_lx = lxapp.LXSERVER
    try:
        class FailedClient:
            async def is_alive(self):
                return True

            async def list_custom_sources(self):
                return [{
                    "id": "bad.js",
                    "name": "bad",
                    "enabled": True,
                    "status": "failed",
                    "error": "script crashed",
                    "supportedSources": ["kw"],
                }]

        lxapp.LXSERVER = FailedClient()
        caps = asyncio.run(source_capabilities())
        for plat, info in caps.items():
            assert info["playback_available"] is False
            assert "failed" in info["reason"]
    finally:
        lxapp.LXSERVER = orig_lx


# ------------------------------------------------- issue #96：状态语义回归 --

_BAD = {"id": "bad.js", "name": "bad", "enabled": True, "status": "failed",
        "error": "script crashed", "supportedSources": ["kw"]}
_GOOD = {"id": "good.js", "name": "good", "enabled": True, "status": "success",
         "supportedSources": ["kw", "wy"]}


def test_describe_aggregates_any_ready_source(monkeypatch):
    """坏源排在首位也不能把其余健康源整体拖成"未加载"（issue #96 核心）。"""
    from app import describe_user_source
    import app as lxapp

    class Fixed:
        def __init__(self, sources):
            self._sources = sources

        async def list_custom_sources(self):
            return self._sources

    orig = lxapp.LXSERVER
    try:
        for label, sources in (("bad-first", [_BAD, _GOOD]), ("good-first", [_GOOD, _BAD])):
            lxapp.LXSERVER = Fixed([dict(s) for s in sources])
            desc = asyncio.run(describe_user_source())
            assert desc["configured"] is True, label
            assert desc["initialized"] is True, label
            assert desc["state"] == "degraded", label
            assert desc["ready_count"] == 1 and desc["failed_count"] == 1, label
            assert "script crashed" in desc["last_error"], label
            by_id = {s["id"]: s for s in desc["sources"]}
            assert by_id["good.js"]["initialized"] is True, label
            assert by_id["bad.js"]["initialized"] is False, label
    finally:
        lxapp.LXSERVER = orig


def test_describe_reports_loading_when_status_missing():
    """上游重载窗口缺 status：不得默认成功，应显示加载中。"""
    from app import describe_user_source
    import app as lxapp

    class Reloading:
        async def list_custom_sources(self):
            return [{"id": "p.js", "name": "p", "enabled": True, "supportedSources": ["kw"]}]

    orig = lxapp.LXSERVER
    try:
        lxapp.LXSERVER = Reloading()
        desc = asyncio.run(describe_user_source())
        assert desc["initialized"] is False
        assert desc["state"] == "loading"
        assert desc["pending_count"] == 1
        assert desc["sources"][0]["status"] == "loading"
    finally:
        lxapp.LXSERVER = orig


def test_source_info_reports_service_failure(test_app_client, fake_lx):
    """源列表查询失败必须按服务故障返回，不能伪装成"未配置源"。"""
    async def dead():
        raise httpx.ConnectError("lxserver down")

    fake_lx.list_custom_sources = dead
    res = test_app_client.get("/api/v1/source")
    assert res.status_code == 503
    body = res.json()
    assert body["category"] == "service_unavailable"
    assert body["stage"] == "lxmusic_to_lxserver"
    assert body["data"]["state"] == "unavailable"


def test_activate_confirms_target_initialization_failure(test_app_client, fake_lx):
    """toggle 成功但目标源初始化失败：不得返回激活成功。"""
    async def failing(target, enabled):
        s = fake_lx._find(target)
        if s and enabled:
            s["enabled"] = True
            s["status"] = "failed"
            s["error"] = "init timeout after 3000ms"
            fake_lx.toggle_calls.append((s["id"], True))
            return True
        return await FakeLxServerClient.set_source_enabled(fake_lx, target, enabled)

    fake_lx.set_source_enabled = failing
    res = test_app_client.post("/api/v1/source", json={"url": "source2"})
    assert res.status_code == 500
    body = res.json()
    assert body["category"] == "runtime"
    assert "init timeout" in body["error"]


def test_activate_pending_target_returns_504(test_app_client, fake_lx, monkeypatch):
    """目标初始化结果一直待定：返回待确认而非假成功，也不误报脚本失败。"""
    import app as lxapp
    monkeypatch.setattr(lxapp, "SOURCE_INIT_CONFIRM_TIMEOUT", 0.3)
    monkeypatch.setattr(lxapp, "SOURCE_INIT_POLL_INTERVAL", 0.05)

    async def pending(target, enabled):
        s = fake_lx._find(target)
        if s and enabled:
            s["enabled"] = True  # 保持无 status：模拟上游重载/初始化中
            fake_lx.toggle_calls.append((s["id"], True))
            return True
        return await FakeLxServerClient.set_source_enabled(fake_lx, target, enabled)

    fake_lx.set_source_enabled = pending
    res = test_app_client.post("/api/v1/source", json={"url": "source2"})
    assert res.status_code == 504
    body = res.json()
    assert body["category"] == "initialization_pending"
    assert body["source_id"] == "source2"


def test_reactivate_ready_source_skips_toggle(test_app_client, fake_lx):
    """已启用且初始化成功的源重复激活：幂等直达确认，不再触发 toggle。"""
    before = list(fake_lx.toggle_calls)
    res = test_app_client.post("/api/v1/source", json={"url": "source1"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert fake_lx.toggle_calls == before



def test_env_active_source_urls_parsing(monkeypatch):
    """LX_SOURCE_LIST 环境变量解析：仅取 active 标记项的 URL，非法输入返回空。"""
    from app import _env_active_source_urls

    monkeypatch.setenv("LX_SOURCE_LIST", json.dumps([
        {"name": "a", "url": "https://s/a.js", "active": True},
        {"name": "b", "url": "https://s/b.js", "active": False},
        {"name": "c", "url": "https://s/c.js", "active": True},
        {"name": "d", "url": "", "active": True},
    ]))
    assert _env_active_source_urls() == ["https://s/a.js", "https://s/c.js"]

    monkeypatch.setenv("LX_SOURCE_LIST", "not-json")
    assert _env_active_source_urls() == []
    monkeypatch.setenv("LX_SOURCE_LIST", "")
    assert _env_active_source_urls() == []
    monkeypatch.delenv("LX_SOURCE_LIST", raising=False)
    assert _env_active_source_urls() == []


def test_bootstrap_migration_restores_multi_active_sources(tmp_path, monkeypatch):
    """lxserver 无启用源（数据卷被清）时，按 LX_SOURCE_LIST 的 active 标记叠加恢复多源。"""
    from conftest import FakeLxServerClient

    fake = FakeLxServerClient()
    for s in fake.sources:
        s["enabled"] = False
    orig_lx = lxapp.LXSERVER
    monkeypatch.setattr(lxapp, "LXSERVER", fake)
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path))  # 无历史 uploads/state.json
    # 隔离仓库根目录可能存在的 .env（_persisted_source_targets 磁盘优先，
    # 命中真实安装配置会让用例测不到环境变量回退路径）
    monkeypatch.setenv("LX_ENV_PATH", str(tmp_path / "absent.env"))
    monkeypatch.setenv("LX_SOURCE_LIST", json.dumps([
        {"name": "a", "url": "https://s/a.js", "active": True},
        {"name": "b", "url": "https://s/b.js", "active": False},
    ]))
    monkeypatch.delenv("LX_SOURCE_URL", raising=False)
    try:
        asyncio.run(lxapp._bootstrap_migration())
    finally:
        lxapp.LXSERVER = orig_lx
    states = {s["id"]: s["enabled"] for s in fake.sources}
    # a.js 经导入 + 激活；既有 source1 保持停用（叠加语义不误开）
    assert states.get("a.js") is True
    assert states.get("source1") is False


def test_bootstrap_migration_falls_back_to_single_url(tmp_path, monkeypatch):
    """旧数据兼容：LX_SOURCE_LIST 无 active 标记时回退 LX_SOURCE_URL 单值激活。"""
    from conftest import FakeLxServerClient

    fake = FakeLxServerClient()
    for s in fake.sources:
        s["enabled"] = False
    orig_lx = lxapp.LXSERVER
    monkeypatch.setattr(lxapp, "LXSERVER", fake)
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LX_ENV_PATH", str(tmp_path / "absent.env"))
    monkeypatch.setenv("LX_SOURCE_LIST", json.dumps([
        {"name": "a", "url": "https://s/a.js", "active": False},
    ]))
    monkeypatch.setenv("LX_SOURCE_URL", "file:///x/source1")
    try:
        asyncio.run(lxapp._bootstrap_migration())
    finally:
        lxapp.LXSERVER = orig_lx
    states = {s["id"]: s["enabled"] for s in fake.sources}
    assert states.get("source1") is True
    assert "https://s/a.js" not in fake.import_calls
# ------------------------------------------------- 播放直链缓存与并发合并 --
# issue #45：同曲同音质重复解析治理——成功缓存、失败负缓存、探活续期、
# 同键在途合并、fresh 旁路与切源失效。

def _flac_probe_handler(request: httpx.Request) -> httpx.Response:
    """探活 mock：dead 链接模拟直链过期后 CDN 拒绝，其余返回合法 flac 前缀。"""
    if "dead" in str(request.url):
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>gone</html>")
    return httpx.Response(
        206,
        headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
        content=b"fLaC" + b"\x00" * 4092,
    )


@pytest.fixture
def fresh_url_cache():
    """隔离模块级直链缓存/在途/熔断状态。"""
    lxapp._URL_CACHE.clear()
    lxapp._URL_INFLIGHT.clear()
    lxapp._CHAIN_HEALTH.clear()
    yield
    lxapp._URL_CACHE.clear()
    lxapp._URL_INFLIGHT.clear()
    lxapp._CHAIN_HEALTH.clear()


def test_track_url_description_reports_remaining_cache_lifetime(test_app_client, fresh_url_cache):
    lxapp._URL_CACHE[("lx:kw:123", "lossless")] = {
        "data": {"url": "https://cdn.example/fixture.flac", "ext": "flac"},
        "ts": lxapp.time.monotonic() - float(lxapp.CONF["url_cache_ttl"]) + 8,
    }
    response = test_app_client.get("/api/v1/track/url", params={"id": "lx:kw:123"})
    assert response.status_code == 200
    assert 0 < response.json()["data"]["reuse_for_s"] <= 8
    assert response.json()["data"]["expiry_source"] == "policy"


def test_track_url_success_cache_reuses_resolution(test_app_client, fresh_url_cache, fake_lx):
    """同曲同音质连续请求：第二次命中缓存，音源仅解析一次（issue #45 主诉求）。"""
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_probe_handler)
    try:
        r1 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
        r2 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = orig_client
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(fake_lx.url_calls) == 1
    assert r1.json()["data"]["url"] == r2.json()["data"]["url"] == "https://media.test/song.flac"


@pytest.mark.asyncio
async def test_track_url_concurrent_requests_merge_inflight(fake_lx, fresh_url_cache):
    """同键并发请求共享一次实际解析（在途 Future 合并），不放大音源调用量。"""
    gate = asyncio.Event()
    fake_lx.url_gate = gate
    probe_client = mock_client(_flac_probe_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    leader = asyncio.create_task(lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0))
    while not fake_lx.url_calls:
        await asyncio.sleep(0.01)
    followers = [
        asyncio.create_task(lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0))
        for _ in range(2)
    ]
    await asyncio.sleep(0.05)
    assert len(fake_lx.url_calls) == 1  # 等待期间未新增解析
    gate.set()
    results = await asyncio.gather(leader, *followers)
    assert len(fake_lx.url_calls) == 1
    assert all(r["url"] == results[0]["url"] for r in results)
    assert lxapp._URL_INFLIGHT == {}
    await probe_client.aclose()


@pytest.mark.asyncio
async def test_track_url_ttl_expiry_renews_via_probe(fake_lx, fresh_url_cache):
    """TTL 到期先探活旧链续期（零额度消耗），不重新解析；续期后缓存刷新继续命中。"""
    probe_client = mock_client(_flac_probe_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    key = ("lx:kw:5886682", "lossless")
    r1 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 1
    lxapp._URL_CACHE[key]["ts"] -= lxapp.CONF["url_cache_ttl"] + 1.0
    r2 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 1  # 旧链探活通过 → 续期，不消耗解析额度
    assert r2["url"] == r1["url"]
    r3 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 1
    assert r3["url"] == r1["url"]
    await probe_client.aclose()


@pytest.mark.asyncio
async def test_track_url_ttl_expiry_dead_url_reresolves(fake_lx, fresh_url_cache):
    """TTL 到期且旧链探活失败：重新解析并覆盖缓存。"""
    probe_client = mock_client(_flac_probe_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    key = ("lx:kw:5886682", "lossless")
    r1 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 1
    entry = lxapp._URL_CACHE[key]
    entry["data"] = {**entry["data"], "url": "https://media.test/dead.flac"}
    entry["ts"] -= lxapp.CONF["url_cache_ttl"] + 1.0
    r2 = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert len(fake_lx.url_calls) == 2
    assert r2["url"] == "https://media.test/song.flac"
    await probe_client.aclose()


def test_track_url_negative_cache_and_fresh_bypass(test_app_client, fresh_url_cache, fake_lx):
    """解析失败写负缓存：短时间内重复请求不再打音源；fresh=1 旁路强制重新解析。

    一次失败解析会走完整七档阶梯中的四档（flac→320k→192k→128k）。"""
    fake_lx.url_result = None
    r1 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    r2 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    assert r1.status_code == 404 and r2.status_code == 404
    assert len(fake_lx.url_calls) == 4  # 第二次请求负缓存命中，未再走阶梯

    r3 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless&fresh=1")
    assert r3.status_code == 404
    assert len(fake_lx.url_calls) == 8  # fresh 旁路负缓存，重新走一遍阶梯

    # 音源恢复后 fresh 重取成功并覆盖负缓存，后续请求恢复命中
    fake_lx.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "test"}
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_probe_handler)
    try:
        r4 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless&fresh=1")
        assert r4.status_code == 200
        r5 = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
        assert r5.status_code == 200
    finally:
        lxapp.app.state.client = orig_client
    assert len(fake_lx.url_calls) == 9  # 成功解析在首档即命中，仅 +1


def _ua_gated_cdn_handler(request: httpx.Request) -> httpx.Response:
    """issue #54：模拟只认探活 UA 的 CDN——UA 不符直接 403（真实酷我行为）。"""
    if request.headers.get("user-agent") != lxapp.UA_PC:
        return httpx.Response(403, content=b"")
    return httpx.Response(
        206,
        headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
        content=b"fLaC" + b"\x00" * 4092,
    )


def test_track_url_resolution_ua_gated_cdn(test_app_client, fresh_url_cache):
    """issue #54：UA 敏感 CDN（如酷我）探活成功后，解析结果 headers 必须携带与
    探活一致的 User-Agent——代理实际播放复用它，不再回退 httpx 默认 UA 被 403。"""
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_ua_gated_cdn_handler)
    try:
        res = test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless")
    finally:
        lxapp.app.state.client = orig_client
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["headers"].get("User-Agent") == lxapp.UA_PC


@pytest.mark.asyncio
async def test_track_url_refresh_backfills_ua(fake_lx, fresh_url_cache):
    """issue #54：空 headers 的旧缓存条目经探活续期后补上探活 UA——续期与播放
    共用同一请求上下文，续期不再延续无 UA 的结果。"""
    probe_client = mock_client(_ua_gated_cdn_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    key = ("lx:kw:5886682", "lossless")
    lxapp._URL_CACHE[key] = {
        "data": {"id": "lx:kw:5886682", "url": "https://media.test/song.flac",
                 "ext": "flac", "file_size": 30000000, "headers": {}},
        "ts": time.monotonic() - lxapp.CONF["url_cache_ttl"] - 1.0,
    }
    try:
        r = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    finally:
        await probe_client.aclose()
    assert r is not None
    assert len(fake_lx.url_calls) == 0  # 探活续期成功，不消耗解析额度
    assert r["headers"].get("User-Agent") == lxapp.UA_PC


@pytest.mark.asyncio
async def test_circuit_breaker_scoped_per_platform(fake_lx, fresh_url_cache):
    """脚本+平台级熔断：tx 上的脚本连续失败只熔断该脚本/tx，kw 解析不受牵连。

    2026-10-10 回归：全局 user_source 熔断下 tx/mg 第三方源故障（tx 只产出加密
    mflac、mg 后端全挂）连累 kw/kg/wy 全平台 404，表现为"几乎所有洛雪曲目不可播"。"""
    probe_client = mock_client(_flac_probe_handler)
    fake_lx.url_result = None
    fake_lx.sources[0]["supportedSources"] = ["kw", "kg", "wy", "tx"]
    try:
        # 3 首不同 tx 曲目各失败一次（同曲会命中失败负缓存不再打音源）→ 触发该脚本 tx 熔断
        for i, songmid in enumerate(["001abc", "002def", "003ghi"]):
            r = await lxapp._resolve_url_cached(
                probe_client, "tx", {"id": f"lx:tx:{songmid}", "lx_source": "tx"}, "lossless", 20.0)
            assert r is None
        tx_calls = len(fake_lx.url_calls)
        assert tx_calls == 12  # 3 次解析 × 七档阶梯中的四档（flac/320k/192k/128k）

        # 第 4 首 tx 曲目被该脚本+平台熔断直接跳过：不再消耗音源调用
        r4 = await lxapp._resolve_url_cached(
            probe_client, "tx", {"id": "lx:tx:004jkl", "lx_source": "tx"}, "lossless", 20.0)
        assert r4 is None
        assert len(fake_lx.url_calls) == tx_calls

        # kw 是不同平台、不同熔断键，不受 tx 脚本熔断影响，正常解析成功
        fake_lx.url_result = {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "test"}
        rw = await lxapp._resolve_url_cached(
            probe_client, "kw", {"id": "lx:kw:5886682", "lx_source": "kw"}, "lossless", 20.0)
        assert rw and rw["url"] == "https://media.test/song.flac"
        assert len(fake_lx.url_calls) == tx_calls + 1
    finally:
        await probe_client.aclose()
        fake_lx.url_script.clear()


@pytest.mark.asyncio
async def test_probe_reject_rotates_to_next_source(fake_lx, fresh_url_cache):
    """同档直链探活失败：excludeApiSources 排除当前脚本后换下一个就绪脚本重试。"""
    probe_client = mock_client(_flac_probe_handler)
    fake_lx.sources = [
        {"id": "bad.js", "name": "坏链源", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
        {"id": "good.js", "name": "好链源", "enabled": True, "status": "success",
         "supportedSources": ["kw"]},
    ]
    fake_lx.url_script = [
        {"url": "https://media.test/dead.flac", "type": "flac",
         "sourceName": "坏链源", "sourceId": "bad.js", "hasMoreSources": True},
        {"url": "https://media.test/song.flac", "type": "flac",
         "sourceName": "好链源", "sourceId": "good.js"},
    ]
    try:
        r = await lxapp._resolve_url_cached(
            probe_client, "kw", {"id": "lx:kw:5886682", "lx_source": "kw"}, "lossless", 20.0)
        assert r and r["url"] == "https://media.test/song.flac"
        # 每次请求都排除其他脚本的稳定 ID，把上游自动切换固定到当前脚本
        assert fake_lx.url_excludes == [["good.js"], ["bad.js"]]
    finally:
        await probe_client.aclose()
        fake_lx.url_script.clear()


@pytest.mark.asyncio
async def test_probe_reject_no_more_sources_falls_to_next_tier(fake_lx, fresh_url_cache):
    """服务端确认无更多候选源（hasMoreSources=false）：同档不再轮换，直接降档。"""
    probe_client = mock_client(_flac_probe_handler)
    # 每档都返回探活失败且无更多源；脚本标识不重复计数（tried 已全在 excluded 时停止）
    fake_lx.url_script = [
        {"url": "https://media.test/dead.flac", "type": "flac",
         "sourceName": "唯一源", "sourceId": "source1", "hasMoreSources": False},
        {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "唯一源",
         "sourceId": "source1"},
    ]
    try:
        r = await lxapp._resolve_url_cached(
            probe_client, "wy", {"id": "lx:wy:1970560262", "lx_source": "wy"}, "lossless", 20.0)
        assert r and r["url"] == "https://media.test/song.flac"
        # lossless 档 1 次调用后即降档（无轮换），high 档成功：共 2 次
        assert [q for _, q in fake_lx.url_calls] == ["flac", "320k"]
    finally:
        await probe_client.aclose()
        fake_lx.url_script.clear()


@pytest.mark.asyncio
async def test_empty_url_response_rotates_to_next_source(fake_lx, fresh_url_cache):
    """脚本"成功"却拿不出直链（200 无 url，如平台未激活的静默空返回）：同档换源重试。"""
    probe_client = mock_client(_flac_probe_handler)
    fake_lx.sources = [
        {"id": "empty.js", "name": "空手源", "enabled": True, "status": "success",
         "supportedSources": ["kg"]},
        {"id": "good.js", "name": "好链源", "enabled": True, "status": "success",
         "supportedSources": ["kg"]},
    ]
    fake_lx.url_script = [
        {"type": "flac", "sourceName": "空手源", "sourceId": "empty.js", "hasMoreSources": True},
        {"url": "https://media.test/song.flac", "type": "flac", "sourceName": "好链源",
         "sourceId": "good.js"},
    ]
    try:
        r = await lxapp._resolve_url_cached(
            probe_client, "kg", {"id": "lx:kg:abc123", "lx_source": "kg"}, "lossless", 20.0)
        assert r and r["url"] == "https://media.test/song.flac"
        assert fake_lx.url_excludes == [["good.js"], ["empty.js"]]
    finally:
        await probe_client.aclose()
        fake_lx.url_script.clear()


def test_chain_half_open_stuck_guard(fresh_url_cache):
    """半开试探超时未回填（解析被取消等）自动放行新试探，避免平台熔断被永久卡死。"""
    import time as _time
    lxapp._CHAIN_HEALTH["user_source:tx"] = {
        "fails": 5, "open_until": _time.time() + 600, "next_try": 0,
        "half_open": True, "half_open_since": _time.time() - 120,
    }
    # 超过一个试探周期：放行新的半开试探
    assert lxapp._chain_available("user_source:tx") is True
    h = lxapp._CHAIN_HEALTH["user_source:tx"]
    assert h["half_open"] is True and h["half_open_since"] > _time.time() - 5
    # 新试探在途期间（未超时）其他请求被挡
    assert lxapp._chain_available("user_source:tx") is False


def test_track_url_quality_isolation(test_app_client, fresh_url_cache, fake_lx):
    """不同音质档各自解析与缓存，同音质命中缓存（音质变化不串缓存）。"""
    fake_lx.url_script = [
        {"url": "https://media.test/song.flac", "type": "flac", "sourceId": "source1"},
        {"url": "https://media.test/song.mp3", "type": "320k", "sourceId": "source1"},
    ]
    def media(request):
        if request.url.path.endswith('.mp3'):
            return httpx.Response(206, content=b'\xff\xfb\x90\x44' + b'\x00' * 4096,
                                  headers={'Content-Type': 'audio/mpeg'})
        return _flac_probe_handler(request)
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(media)
    try:
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless").status_code == 200
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless").status_code == 200
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=high").status_code == 200
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=high").status_code == 200
    finally:
        lxapp.app.state.client = orig_client
    assert [q for _, q in fake_lx.url_calls] == ["flac", "320k"]


def test_track_url_cache_cleared_on_source_change(test_app_client, fresh_url_cache, fake_lx):
    """切换音源后直链缓存清空：后续请求重新解析，杜绝串源。"""
    orig_client = lxapp.app.state.client
    lxapp.app.state.client = mock_client(_flac_probe_handler)
    try:
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless").status_code == 200
        assert len(fake_lx.url_calls) == 1
        res = test_app_client.post("/api/v1/source", json={"url": "file:///data/lxserver/users/source/_open/source1"})
        assert res.status_code == 200 and res.json()["ok"] is True
        assert lxapp._URL_CACHE == {}
        assert test_app_client.get("/api/v1/track/url?id=lx:kw:5886682&quality=lossless").status_code == 200
        assert len(fake_lx.url_calls) == 2
    finally:
        lxapp.app.state.client = orig_client


@pytest.mark.asyncio
async def test_url_inflight_leader_failure_cleans_up(monkeypatch, fresh_url_cache):
    """领头解析异常传播给等待者，在途状态清理且不写缓存（含负缓存），随后可重试。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def flaky_resolve(client, src, item, quality="lossless", budget=20.0, **kwargs):
        started.set()
        await release.wait()
        raise RuntimeError("boom")

    monkeypatch.setattr(lxapp, "resolve_and_probe", flaky_resolve)
    probe_client = mock_client(_flac_probe_handler)
    item = {"id": "lx:kw:5886682", "lx_source": "kw"}
    leader = asyncio.create_task(lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0))
    await started.wait()
    follower = asyncio.create_task(lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0))
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(RuntimeError, match="boom"):
        await leader
    with pytest.raises(RuntimeError, match="boom"):
        await follower
    assert lxapp._URL_INFLIGHT == {}
    assert lxapp._URL_CACHE == {}

    async def ok_resolve(client, src, item, quality="lossless", budget=20.0, **kwargs):
        return {"id": item.get("id"), "url": "https://media.test/song.flac"}

    monkeypatch.setattr(lxapp, "resolve_and_probe", ok_resolve)
    r = await lxapp._resolve_url_cached(probe_client, "kw", item, "lossless", 20.0)
    assert r["url"] == "https://media.test/song.flac"
    await probe_client.aclose()




@pytest.mark.asyncio
async def test_url_inflight_cancelled_leader_does_not_cancel_follower(monkeypatch, fresh_url_cache):
    started = asyncio.Event()
    async def resolve(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(lxapp, "resolve_and_probe", resolve)
    item = {"id": "lx:kw:cancel", "lx_source": "kw"}
    leader = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item, "lossless", 20))
    await started.wait()
    follower = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item, "lossless", 20))
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    with pytest.raises(RuntimeError, match="leader cancelled"):
        await follower
    assert not follower.cancelled()
    assert not lxapp._URL_INFLIGHT


def test_source_deactivate_filename_differs_from_name(test_app_client, fake_lx, tmp_path, monkeypatch):
    script = tmp_path / "local.js"
    script.write_text("/**\n * @name Real Source\n */")
    calls = []
    async def enabled(source_id, value):
        calls.append((source_id, value))
        return source_id == "real-id"
    async def find(**kwargs):
        return {"id": "real-id"} if kwargs.get("by_name") == "Real Source" else None
    monkeypatch.setattr(lxapp.LXSERVER, "set_source_enabled", enabled)
    monkeypatch.setattr(lxapp, "_find_lxserver_source", find)
    r = test_app_client.post("/api/v1/source", json={"url": script.as_uri(), "enabled": False})
    assert r.status_code == 200, r.text
    assert ("real-id", False) in calls


@pytest.mark.asyncio
async def test_url_inflight_isolated_across_source_generations(monkeypatch, fresh_url_cache):
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = []
    async def resolve(*args, **kwargs):
        n = len(calls)
        calls.append(lxapp._URL_CACHE_GEN)
        started[n].set()
        await release[n].wait()
        return {"url": f"https://media.test/gen-{calls[n]}"}
    monkeypatch.setattr(lxapp, "resolve_and_probe", resolve)
    item = {"id": "lx:kw:gen"}
    old = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
    await started[0].wait()
    lxapp._url_cache_reset()
    new = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
    try:
        await asyncio.wait_for(started[1].wait(), 1)
        release[0].set()
        await old
        assert len(lxapp._URL_INFLIGHT) == 1  # 旧任务不能清理新代任务
        follower = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
        await asyncio.sleep(0)
        release[1].set()
        r, f = await asyncio.gather(new, follower)
        assert r == f == {"url": f"https://media.test/gen-{calls[1]}"}
        assert calls[1] == calls[0] + 1
        assert lxapp._URL_CACHE[(item["id"], "lossless")]["data"] == r
    finally:
        for event in release:
            event.set()
        await asyncio.gather(old, new, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("usable", [False, True])
async def test_expired_url_probe_and_resolution_are_singleflight(monkeypatch, fresh_url_cache, usable):
    item = {"id": "lx:kw:expired"}
    key = (item["id"], "lossless")
    lxapp._URL_CACHE[key] = {"data": {"url": "https://media.test/old"},
                            "ts": lxapp.time.monotonic() - 1000}
    probing, release = asyncio.Event(), asyncio.Event()
    counts = {"probe": 0, "resolve": 0}
    async def refresh(*args):
        counts["probe"] += 1
        probing.set()
        await release.wait()
        return {"url": "https://media.test/old"} if usable else None
    async def resolve(*args, **kwargs):
        counts["resolve"] += 1
        return {"url": "https://media.test/new"}
    monkeypatch.setattr(lxapp, "_refresh_cached_url", refresh)
    monkeypatch.setattr(lxapp, "resolve_and_probe", resolve)
    leader = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
    await probing.wait()
    follower = asyncio.create_task(lxapp._resolve_url_cached(None, "kw", item))
    await asyncio.sleep(0)
    release.set()
    a, b = await asyncio.gather(leader, follower)
    assert a == b
    assert counts == {"probe": 1, "resolve": 0 if usable else 1}


def test_bootstrap_recovers_partial_failure_and_uses_latest_env(tmp_path, monkeypatch):
    from conftest import FakeLxServerClient
    fake = FakeLxServerClient()
    for s in fake.sources:
        s["enabled"] = False
    monkeypatch.setattr(lxapp, "LXSERVER", fake)
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path / "data"))
    env_path = tmp_path / ".env"
    monkeypatch.setenv("LX_ENV_PATH", str(env_path))
    urls = ["https://s/a.js", "https://s/b.js"]
    def save(active_urls):
        env_path.write_text("LX_SOURCE_LIST='" + json.dumps([
            {"url": u, "active": u in active_urls} for u in urls
        ]) + "'\nLX_SOURCE_URL='" + (active_urls[0] if active_urls else "") + "'\n")
    save(urls)
    original_import = fake.import_custom_source
    async def fail_b(url):
        if url == urls[1]:
            raise RuntimeError("temporarily unavailable")
        return await original_import(url)
    monkeypatch.setattr(fake, "import_custom_source", fail_b)
    asyncio.run(lxapp._bootstrap_migration())
    assert {s["id"] for s in fake.sources if s["enabled"]} == {"a.js"}
    monkeypatch.setattr(fake, "import_custom_source", original_import)
    asyncio.run(lxapp._bootstrap_migration())
    assert {s["id"] for s in fake.sources if s["enabled"]} == {"a.js", "b.js"}
    # 旧进程环境仍保留 A/B；最新磁盘目标只启用 A，随后全部显式关闭。
    monkeypatch.setenv("LX_SOURCE_LIST", json.dumps([{"url": u, "active": True} for u in urls]))
    save([urls[0]])
    asyncio.run(lxapp._bootstrap_migration())
    assert {s["id"] for s in fake.sources if s["enabled"]} == {"a.js"}
    save([])
    asyncio.run(lxapp._bootstrap_migration())
    assert not any(s["enabled"] for s in fake.sources)


def test_bootstrap_failed_new_target_keeps_old_available_source(tmp_path, monkeypatch):
    from conftest import FakeLxServerClient
    fake = FakeLxServerClient()
    monkeypatch.setattr(lxapp, "LXSERVER", fake)
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path / "data"))
    env = tmp_path / ".env"
    env.write_text('LX_SOURCE_LIST=\'[{"url":"https://s/new.js","active":true}]\'\n')
    monkeypatch.setenv("LX_ENV_PATH", str(env))
    async def fail(*args):
        raise RuntimeError("temporary import failure")
    monkeypatch.setattr(fake, "import_custom_source", fail)
    asyncio.run(lxapp._bootstrap_migration())
    assert fake.sources[0]["enabled"] is True
    assert not fake.toggle_calls


# =========================================================================
# issue #49：songInfo 磁盘持久化 + 播放元数据透传
# =========================================================================

def test_songinfo_store_roundtrip_across_restart(tmp_path):
    """存储启用时 cache_song_info 落盘；新实例（模拟重启）加载后反查命中。"""
    store = str(tmp_path / "songinfo_store.json")
    raw = {"name": "海阔天空", "singer": "Beyond", "source": "kw",
           "songmid": "5886682", "albumName": "乐与怒", "interval": "05:24", "img": ""}

    client = LxServerClient(base_url="http://lxserver-test", store_path=store)
    client.cache_song_info("lx:kw:5886682", raw)
    assert client._store_dirty
    client.flush_song_info_store()
    assert client._store_dirty is False

    restarted = LxServerClient(base_url="http://lxserver-test", store_path=store)
    assert restarted.get_cached_song_info("lx:kw:5886682") == raw


def test_songinfo_store_corrupt_file_resets(tmp_path):
    """存储文件损坏：启动重置为空，服务照常起，不抛异常。"""
    store = str(tmp_path / "songinfo_store.json")
    with open(store, "w", encoding="utf-8") as f:
        f.write("{corrupted json!")
    client = LxServerClient(base_url="http://lxserver-test", store_path=store)
    assert client.get_cached_song_info("lx:kw:whatever") is None


def test_songinfo_store_capacity_eviction(tmp_path):
    """容量上限：超限淘汰最旧条目，落盘文件同步收缩。"""
    store = str(tmp_path / "songinfo_store.json")
    client = LxServerClient(base_url="http://lxserver-test", store_path=store)
    client._cache_max = 4
    for i in range(6):
        client.cache_song_info(f"lx:kw:{i}", {"name": f"s{i}", "songmid": str(i), "source": "kw"})
    assert "lx:kw:0" not in client._song_info_cache
    assert "lx:kw:5" in client._song_info_cache
    client.flush_song_info_store()
    restarted = LxServerClient(base_url="http://lxserver-test", store_path=store)
    restarted._cache_max = 4
    assert restarted.get_cached_song_info("lx:kw:0") is None
    assert restarted.get_cached_song_info("lx:kw:5") is not None


def _install_probe_client():
    """按夹具媒体格式返回Range 206前缀，不把MP3请求伪装为FLAC。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('.mp3'):
            return httpx.Response(206, content=b'\xff\xfb\x90\x44' + b'\x00' * 4096,
                                  headers={'Content-Type': 'audio/mpeg'})
        return httpx.Response(
            206,
            headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"},
            content=b"fLaC" + b"\x00" * 4092,
        )
    had_client = "client" in lxapp.app.state._state
    orig_client = lxapp.app.state._state.get("client")
    lxapp.app.state.client = mock_client(handler)

    def restore():
        if had_client:
            lxapp.app.state.client = orig_client
        else:
            lxapp.get_http(lxapp.app)

    return restore


def _spy_get_music_url(fake_lx, seen):
    original = fake_lx.get_music_url

    async def spy(song_info, quality="128k", exclude_api_sources=None, request_timeout=None,
                  auto_switch_api_source=True):
        seen.append(dict(song_info))
        return await original(song_info, quality, exclude_api_sources, request_timeout,
                              auto_switch_api_source)

    fake_lx.get_music_url = spy


def test_track_url_meta_params_synthesize_and_writeback(test_app_client, fake_lx):
    """反查全冷 + 代理透传真实标签：合成 songInfo 用传入值并写回反查缓存，
    关键词型源脚本拿到的是真实歌名歌手而非"未知曲目/未知歌手"。"""
    seen: "list[dict]" = []
    _spy_get_music_url(fake_lx, seen)
    restore_client = _install_probe_client()
    try:
        res = test_app_client.get(
            "/api/v1/track/url",
            params={"id": "lx:kw:999001", "quality": "lossless",
                    "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
                    "duration": 269.3, "cover": "https://img.test/x.jpg"},
        )
    finally:
        restore_client()
    assert res.status_code == 200
    assert seen, "get_music_url 未被调用"
    assert seen[0]["name"] == "晴天"
    assert seen[0]["singer"] == "周杰伦"
    assert seen[0]["albumName"] == "叶惠美"
    assert seen[0]["rid"] == "999001"
    # 写回反查缓存：同曲后续解析零成本命中真实标签
    assert fake_lx.get_cached_song_info("lx:kw:999001")["name"] == "晴天"


def test_track_url_cold_without_meta_falls_back_unknown(test_app_client, fake_lx):
    """无元数据透传且反查全冷：维持原有兜底语义（桩合成"测试曲目"），不崩。"""
    seen: "list[dict]" = []
    _spy_get_music_url(fake_lx, seen)
    restore_client = _install_probe_client()
    try:
        res = test_app_client.get("/api/v1/track/url", params={"id": "lx:kw:999002", "quality": "lossless"})
    finally:
        restore_client()
    assert res.status_code == 200
    assert seen and seen[0]["name"] == "测试曲目"


def test_track_url_cached_songinfo_not_overridden_by_meta(test_app_client, fake_lx):
    """反查已有真实条目时元数据参数不覆盖既有 songInfo。"""
    fake_lx.cache_song_info("lx:kw:999003", {
        "name": "真名", "singer": "真歌手", "source": "kw", "songmid": "999003",
    })
    seen: "list[dict]" = []
    _spy_get_music_url(fake_lx, seen)
    restore_client = _install_probe_client()
    try:
        res = test_app_client.get(
            "/api/v1/track/url",
            params={"id": "lx:kw:999003", "quality": "lossless", "title": "假名", "artist": "假歌手"},
        )
    finally:
        restore_client()
    assert res.status_code == 200
    assert seen and seen[0]["name"] == "真名" and seen[0]["singer"] == "真歌手"


def test_track_info_rebuilds_from_songinfo_cache(test_app_client, fake_lx):
    """映射缓存冷但 songInfo 反查在（重启后磁盘持久层加载）：/track/info 重建
    映射条目返回真实标题，不再合成"未知曲目"。"""
    fake_lx.cache_song_info("lx:wy:346075", {
        "name": "真的爱你", "singer": "Beyond", "source": "wy", "songmid": "346075",
        "albumName": "秘密警察", "interval": "04:45", "img": "https://img.test/y.jpg",
    })
    res = test_app_client.get("/api/v1/track/info", params={"id": "lx:wy:346075"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["title"] == "真的爱你"
    assert data["artist"] == "Beyond"
    assert data["duration_s"] == 285


def test_track_info_meta_params_synthesize(test_app_client, fake_lx):
    """映射与 songInfo 反查全冷 + 元数据参数：详情直接返回传入标签并写回。"""
    res = test_app_client.get(
        "/api/v1/track/info",
        params={"id": "lx:kw:999004", "title": "晴天", "artist": "周杰伦", "duration": 269},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["title"] == "晴天"
    assert data["artist"] == "周杰伦"
    cached = fake_lx.get_cached_song_info("lx:kw:999004")
    assert cached and cached["name"] == "晴天"


def test_failure_path_timeouts_tightened():
    """issue #55：失败路径预算收紧——单轮解析 8s、探活 3s、端点总预算 15s。

    成功路径 1-3s 内返回不受影响；收紧的是坏链/慢源的失败上限，避免点播
    空等 5-7s 后跳歌。env（LX_URL_TIMEOUT/LX_RESOLVER_TIMEOUT/LX_PROBE_TIMEOUT）
    仍可覆盖。"""
    import app as lxapp
    assert lxapp.CONF["resolver_timeout"] == 8.0
    assert lxapp.CONF["probe_timeout"] == 3.0
    assert lxapp.CONF["url_timeout"] == 15.0


def test_proxy_media_ua_matches_lx_probe_ua():
    """issue #54 收尾：代理出流兜底 UA 与 lxmusic 探活 UA 必须逐字符一致。

    两份常量分属 proxy / lxmusic-service 两个服务无法共享 import，单边改动会
    再次造成"探活通过、实际播放 403"——此处跨服务断言锁死一致性。"""
    import sys
    from pathlib import Path
    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from proxy.app import _LX_MEDIA_UA
    assert _LX_MEDIA_UA == lxapp.UA_PC


def test_lx_timeouts_hot_reload_clears_negative_cache_and_circuit_breaker(tmp_path, monkeypatch, fresh_url_cache):
    """热重载配置时清空失败负缓存与熔断状态，保留成功缓存。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LX_RESOLVER_TIMEOUT=8.0\nLX_URL_TIMEOUT=15.0\nLX_PROBE_TIMEOUT=3.0\n", encoding="utf-8")
    monkeypatch.setenv("LX_ENV_PATH", str(env_file))

    lxapp._reload_timeout_settings()
    assert lxapp.CONF["resolver_timeout"] == 8.0
    assert lxapp.CONF["url_timeout"] == 15.0
    assert lxapp.CONF["probe_timeout"] == 3.0

    # 填充正缓存、负缓存与熔断状态
    lxapp._URL_CACHE[("lx:kg:good", "high")] = {"data": {"url": "https://ok.test"}, "ts": 1000.0}
    lxapp._URL_CACHE[("lx:kg:bad", "high")] = {"data": None, "ts": 1000.0}
    lxapp._CHAIN_HEALTH["user_source:kg"] = {"state": "open", "failures": 5}
    old_gen = lxapp._URL_CACHE_GEN

    # 修改配置
    env_file.write_text("LX_RESOLVER_TIMEOUT=12.0\nLX_URL_TIMEOUT=25.0\nLX_PROBE_TIMEOUT=5.0\n", encoding="utf-8")
    lxapp._reload_timeout_settings()

    assert lxapp.CONF["resolver_timeout"] == 12.0
    assert lxapp.CONF["url_timeout"] == 25.0
    assert lxapp.CONF["probe_timeout"] == 5.0
    # 负缓存清除，正缓存保留
    assert ("lx:kg:bad", "high") not in lxapp._URL_CACHE
    assert ("lx:kg:good", "high") in lxapp._URL_CACHE
    # 熔断清空，代数递增
    assert lxapp._CHAIN_HEALTH == {}
    assert lxapp._URL_CACHE_GEN > old_gen


@pytest.mark.anyio
async def test_slow_source_exceeds_default_resolver_timeout_then_succeeds_with_enlarged_timeout(fake_lx, monkeypatch, fresh_url_cache):
    """慢源解析超时保护：超出单次解析超时放弃/接力，调大参数后成功取链。"""
    async def slow_get_music_url(song_info, quality="128k", exclude_api_sources=None,
                                 request_timeout=None, auto_switch_api_source=True):
        fake_lx.url_timeouts.append(request_timeout)
        await asyncio.sleep(0.1)
        return {"url": "https://media.test/slow.mp3", "type": "128k", "sourceId": "source1"}

    monkeypatch.setattr(fake_lx, "get_music_url", slow_get_music_url)

    def probe_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            content=b"\xff\xfb\x90\x44" + b"\x00" * 4096,
            headers={"Content-Range": "bytes 0-4099/50000", "Content-Type": "audio/mpeg"},
        )

    client = mock_client(probe_handler)

    # 1. resolver_timeout 设为 0.03s（小于 0.1s），应该超时失败
    res1 = await lxapp.resolve_and_probe(
        client, "kw", {"id": "lx:kw:SLOW", "songmid": "SLOW"}, quality="128k",
        budget=1.0, resolver_timeout=0.03, probe_timeout=0.5,
    )
    assert res1 is None
    assert fake_lx.url_timeouts[-1] == 0.03

    # 2. 调大 resolver_timeout 到 0.5s（大于 0.1s），成功返回
    res2 = await lxapp.resolve_and_probe(
        client, "kw", {"id": "lx:kw:SLOW", "songmid": "SLOW"}, quality="128k",
        budget=1.0, resolver_timeout=0.5, probe_timeout=0.5,
    )
    assert res2 is not None
    assert res2["url"] == "https://media.test/slow.mp3"
    assert fake_lx.url_timeouts[-1] == 0.5


def test_track_url_endpoint_uses_hot_reloaded_timeouts(test_app_client, fake_lx, tmp_path, monkeypatch, fresh_url_cache):
    """GET /api/v1/track/url 每次请求使用配置快照透传单次解析超时。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LX_RESOLVER_TIMEOUT=11.5\nLX_URL_TIMEOUT=22.0\nLX_PROBE_TIMEOUT=4.5\n", encoding="utf-8")
    monkeypatch.setenv("LX_ENV_PATH", str(env_file))
    fake_lx.url_result = {"url": "https://media.test/song.mp3", "type": "128k", "sourceId": "source1"}

    restore = _install_probe_client()
    try:
        resp = test_app_client.get("/api/v1/track/url", params={"id": "lx:kw:5886682", "quality": "128k"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert fake_lx.url_timeouts[-1] == 11.5
    finally:
        restore()
