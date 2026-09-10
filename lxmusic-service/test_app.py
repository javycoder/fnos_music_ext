"""lxmusic-service 单元测试：ID 契约 / 搜索 / 链路探活 / 熔断 / trackercdn hash 解析 / eapi 参数。"""
import sys
import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

# 以独立模块名加载服务本体，避免与其他服务的顶层 `app` 模块在同一 pytest 会话中冲突
_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("lxmusic_service_app", _HERE / "app.py")
lxapp = importlib.util.module_from_spec(_spec)
sys.modules["lxmusic_service_app"] = lxapp
_spec.loader.exec_module(lxapp)

parse_track_id = lxapp.parse_track_id
normalize_source = lxapp.normalize_source
_quality_tiers = lxapp._quality_tiers
_kg_hash_for_quality = lxapp._kg_hash_for_quality
_eapi_params = lxapp._eapi_params


@pytest.fixture(autouse=True)
def setup_http(monkeypatch):
    lxapp._SONG_CACHE.clear()
    lxapp._CHAIN_HEALTH.clear()
    lxapp.CONF["third_party"] = True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    lxapp.app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772"
    )


# ------------------------------------------------------------- id contract --

def test_parse_track_id():
    assert parse_track_id("lx:kg:ABC123") == ("kg", "ABC123")
    assert parse_track_id("lx:wy:186016") == ("wy", "186016")
    assert parse_track_id("lx:mg:600902") == ("mg", "600902")
    assert parse_track_id("lx:tx:0039MnYb0qxYhV") == ("tx", "0039MnYb0qxYhV")
    assert parse_track_id("lx:kw:228908") == ("kw", "228908")
    assert parse_track_id("kg:ABC123") == ("kg", "ABC123")
    assert parse_track_id("bad") == ("", "")
    assert parse_track_id("lx:xx:1") == ("", "")


def test_normalize_source():
    assert normalize_source("kugou") == "kg"
    assert normalize_source("KG") == "kg"
    assert normalize_source("netease") == "wy"
    assert normalize_source("migu") == "mg"
    assert normalize_source("qq") == "tx"
    assert normalize_source("tencent") == "tx"
    assert normalize_source("kuwo") == "kw"
    assert normalize_source("zzz") == ""


def test_quality_tiers():
    assert _quality_tiers("lossless") == ["lossless", "high", "standard"]
    assert _quality_tiers("high") == ["high", "standard"]
    assert _quality_tiers("") == ["standard"]


def test_kg_hash_for_quality():
    item = {"hash": "H128", "hash_hq": "H320", "hash_sq": "HFLAC"}
    assert _kg_hash_for_quality(item, "lossless") == "HFLAC"
    assert _kg_hash_for_quality(item, "high") == "H320"
    assert _kg_hash_for_quality(item, "standard") == "H128"
    # 缺失高音质 hash 时降级
    assert _kg_hash_for_quality({"hash": "H128"}, "lossless") == "H128"


# ------------------------------------------------------------------- healthz --

def test_healthz():
    with TestClient(lxapp.app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["ok"] is True
        assert rj["service"] == "fnmusic-lxmusic"
        assert set(rj["sources"]) == {"kg", "wy", "mg", "kw"}
        assert rj["third_party"] is True
        assert isinstance(rj["chains"], dict)


# -------------------------------------------------------------------- search --

def test_search_aggregates_sources():
    def handler(request: httpx.Request) -> httpx.Response:
        if "mobilecdn.kugou.com" in str(request.url):
            assert request.url.params.get("keyword") == "晴天"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "info": [
                            {
                                "hash": "KGHASH_VIP",
                                "sqhash": "KGSQ_VIP",
                                "songname": "晴天(VIP专享)",
                                "singername": "周杰伦",
                                "pay_type": 3,  # VIP 曲目，应被过滤
                            },
                            {
                                "hash": "KGHASH1",
                                "sqhash": "KGSQ1",
                                "hqhash": "KGHQ1",
                                "songname": "晴天",
                                "singername": "周杰伦",
                                "album_name": "叶惠美",
                                "duration": 269000,
                                "pay_type": 0,  # 免费曲目，应保留
                            },
                        ]
                    }
                },
            )
        if "music.163.com" in str(request.url):
            assert "s=%E6%99%B4%E5%A4%A9" in request.read().decode() or request.url.params.get("s") == "晴天"
            return httpx.Response(
                200,
                json={
                    "result": {
                        "songs": [
                            {
                                "id": 999999,
                                "name": "晴天(VIP原版)",
                                "artists": [{"name": "周杰伦"}],
                                "fee": 1,  # VIP 曲目，应被过滤
                            },
                            {
                                "id": 186016,
                                "name": "晴天",
                                "artists": [{"name": "周杰伦"}],
                                "album": {"name": "叶惠美", "picUrl": "https://img.test/wy.jpg"},
                                "duration": 269000,
                                "fee": 0,  # 免费曲目，应保留
                            },
                        ]
                    }
                },
            )
        if "migu.cn" in str(request.url):
            if "player_get_song_info" in str(request.url):
                assert request.url.params.get("copyrightId") == "600902"
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "play_url": "https://migu.test/600902.mp3",
                            "format_type": "mp3",
                            "fileSize": 8000000,
                        }
                    },
                )
            assert request.url.params.get("text") == "晴天"
            return httpx.Response(
                200,
                json={
                    "songs": [
                        {
                            "copyrightId": "600902",
                            "songName": "晴天",
                            "singers": [{"name": "周杰伦"}],
                            "albums": [{"albumName": "叶惠美"}],
                            "length": 269000,
                            "toneFlags": [{"toneType": "SQ"}],
                            "lrcUrl": "https://lrc.test/600902.lrc",
                        }
                    ]
                },
            )
        if "migu.test" in str(request.url):
            # mg 直链探活（Range 0-1）：返回 206 音频与全量大小
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 0-1/8000000"},
                content=b"ID3",
            )
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kg,wy,mg"})
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["ok"] is True
        assert rj["errors"] == {}
        ids = {it["id"] for it in rj["items"]}
        # 确保 VIP 曲目被剔除，只有免费曲目入选
        assert "lx:kg:KGHASH_VIP" not in ids
        assert "lx:wy:999999" not in ids
        assert ids == {"lx:kg:KGHASH1", "lx:wy:186016", "lx:mg:600902"}
        by_id = {it["id"]: it for it in rj["items"]}
        assert by_id["lx:kg:KGHASH1"]["ext"] == "flac"
        assert by_id["lx:kg:KGHASH1"]["lx_source"] == "kg"
        assert by_id["lx:kg:KGHASH1"]["pay_type"] == 0
        assert by_id["lx:wy:186016"]["duration_s"] == 269.0
        assert by_id["lx:wy:186016"]["fee"] == 0
        assert by_id["lx:mg:600902"]["lrc_url"] == "https://lrc.test/600902.lrc"
        # mg 条目经真实 Range 探活通过，带 verified 标记
        assert by_id["lx:mg:600902"]["verified"] is True


def test_search_requires_keyword():
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search")
        assert resp.status_code == 400


# ------------------------------------------------------------------ track url --

def test_track_url_kg_playinfo_resolution():
    def handler(request: httpx.Request) -> httpx.Response:
        if "m.kugou.com" in str(request.url):
            assert request.url.params.get("hash") == "KGSQ1"
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text='{"errcode":0,"url":"https://sharefs.kugou.com/mp3_track.mp3","fileSize":4085749,"bitRate":128,"extName":"mp3"}',
            )
        if "sharefs.kugou.com" in str(request.url):
            return httpx.Response(206, content=b"ID3")
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    lxapp._cache_put(
        {
            "id": "lx:kg:KGHASH1",
            "hash": "KGHASH1",
            "hash_hq": "KGHQ1",
            "hash_sq": "KGSQ1",
        }
    )

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kg:KGHASH1", "quality": "lossless"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["url"] == "https://sharefs.kugou.com/mp3_track.mp3"
        assert data["ext"] == "mp3"
        assert data["file_size"] == 4085749
        assert data["headers"]["User-Agent"]


def test_track_url_kg_trackercdn_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        if "m.kugou.com" in str(request.url):
            return httpx.Response(404)
        if "trackercdn" in str(request.url):
            assert request.url.params.get("hash") == "KGSQ1"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "url": "https://cdn.kugou.com/flac_track.flac",
                    "ext": "flac",
                    "file_size": 28936190,
                    "bitRate": 998,
                },
            )
        if "cdn.kugou.com" in str(request.url):
            return httpx.Response(206, content=b"fLaC")
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    lxapp._cache_put(
        {
            "id": "lx:kg:KGHASH1",
            "hash": "KGHASH1",
            "hash_hq": "KGHQ1",
            "hash_sq": "KGSQ1",
        }
    )

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kg:KGHASH1", "quality": "lossless"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["url"] == "https://cdn.kugou.com/flac_track.flac"


def test_track_url_wy_outer_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        if "interface3.music.163.com" in str(request.url):
            # eapi 未返回 url
            return httpx.Response(200, json={"data": [{"url": ""}]})
        if "outer/url" in str(request.url):
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/mpeg", "Content-Length": "1024"},
                content=b"ID3" + b"\x00" * 1021,
            )
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:wy:186016", "quality": "standard"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "outer/url" in data["url"]
        assert data["ext"] == "mp3"
        assert data["br"] == 128000


def test_track_url_invalid_id():
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "bogus"})
        assert resp.status_code == 400


def test_track_url_no_url_404():
    def handler(request: httpx.Request) -> httpx.Response:
        if "trackercdn" in str(request.url):
            return httpx.Response(200, json={"code": 3001, "url": ""})
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:kg:MISSING"})
        assert resp.status_code == 404


# --------------------------------------------------------------------- eapi ---

@pytest.mark.skipif(not lxapp.HAS_CRYPTO, reason="pycryptodome not installed")
def test_eapi_params_shape():
    params = _eapi_params("/api/song/enhance/player/url", {"header": {"os": "pc"}, "ids": [1], "br": 999000})
    assert isinstance(params, str) and len(params) > 32
    from Crypto.Cipher import AES

    raw = bytes.fromhex(params)
    plain = AES.new(lxapp._EAPI_KEY, AES.MODE_ECB).decrypt(raw)
    # PKCS7 去填充
    pad = plain[-1]
    plain = plain[:-pad]
    text = plain.decode("utf-8", errors="replace")
    assert text.startswith("/api/song/enhance/player/url-36cd479b6b5-")
    assert "-36cd479b6b5-" in text  # 末段为 md5 摘要
    digest = text.rsplit("-36cd479b6b5-", 1)[-1]
    assert len(digest) == 32 and digest == digest.lower()


# ------------------------------------------------------- tx / kw 新源与链路 ---

_KW_RS_BODY = (
    "{'abslist':["
    "{'MUSICRID':'MUSIC_228908','SONGNAME':'晴天','ARTIST':'周杰伦','ALBUM':'叶惠美',"
    "'DURATION':269,'PAY':1,'web_albumpic_short':'120/85/1/4091887608.jpg',"
    "'payInfo':{'cannotOnlinePlay':'0','cannotDownload':'1'}},"
    "{'MUSICRID':'MUSIC_111222','SONGNAME':'晴天 (DJ版)','ARTIST':'路人','DURATION':130,'PAY':0,"
    "'payInfo':{'cannotOnlinePlay':'1'}}"
    "]}"
)


def _kw_chain_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "search.kuwo.cn" in url:
        assert request.url.params.get("all") == "晴天"
        return httpx.Response(200, text=_KW_RS_BODY)
    if "musicapi.haitangw.net" in url:
        # 长青 kw 链路：302 → 酷我 CDN FLAC
        return httpx.Response(
            302,
            headers={"Location": "https://car-er.kuwo.cn/abc123/resource/F228908.flac"},
        )
    if "car-er.kuwo.cn" in url:
        return httpx.Response(
            206,
            headers={"Content-Type": "audio/x-flac", "Content-Range": "bytes 0-1/38210000"},
            content=b"fLaC",
        )
    return httpx.Response(404)


def test_search_kw_verified_flac_via_chain():
    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(_kw_chain_handler), follow_redirects=True)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        rj = resp.json()
        # cannotOnlinePlay=1 的条目剔除；VIP 曲（PAY=1）经长青链路探活通过后保留
        ids = [it["id"] for it in rj["items"]]
        assert ids == ["lx:kw:228908"]
        item = rj["items"][0]
        assert item["verified"] is True
        assert item["ext"] == "flac"
        assert item["file_size"] == 38210000
        assert item["_probe"]["url"].endswith(".flac")
        assert item["cover_url"].startswith("https://img1.kuwo.cn/star/albumcover/")

        # track/url 复用探活缓存（15 分钟内不再回源）
        resp2 = client.get("/api/v1/track/url", params={"id": "lx:kw:228908", "quality": "lossless"})
        assert resp2.status_code == 200
        data = resp2.json()["data"]
        assert data["url"].endswith(".flac")
        assert data["ext"] == "flac"


def test_search_kw_empty_when_third_party_disabled():
    lxapp.CONF["third_party"] = False
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "search.kuwo.cn" in str(request.url):
            return httpx.Response(200, text=_KW_RS_BODY)
        hits["n"] += 1  # 任何链路/探活请求都不应发生
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert hits["n"] == 0  # 关闭第三方后零链路请求，快速空返回


_TX_SEARCH_RESP = {
    "req_1": {
        "code": 0,
        "data": {
            "body": {
                "song": {
                    "list": [
                        {
                            "mid": "0039MnYb0qxYhV",
                            "title": "晴天",
                            "singer": [{"name": "周杰伦"}],
                            "album": {"mid": "000MkMni19ClKG", "name": "叶惠美"},
                            "interval": 269,
                            "pay": {"pay_play": 1},
                        },
                        {
                            "mid": "0042rlGx2WHBrG",
                            "title": "晴天 (深情版)",
                            "singer": [{"name": "Lucky小爱"}],
                            "album": {"mid": "004QUu810PIQis", "name": "翻唱集"},
                            "interval": 278,
                            "pay": {"pay_play": 0},
                        },
                    ]
                }
            }
        },
    }
}


def test_search_tx_empty_without_alive_chain():
    """tx 搜索接口正常但无存活第三方链路：探活全部失败 → 返回空且不报错。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "u.y.qq.com" in str(request.url):
            body = request.read().decode()
            assert "DoSearchForQQMusicDesktop" in body  # httpx json 序列化中文为 \uXXXX
            return httpx.Response(200, json=_TX_SEARCH_RESP)
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "tx"})
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["items"] == []
        assert rj["errors"] == {"tx": "no_resolver_registered"}

        # track/url 同样 404（无链路）
        resp2 = client.get("/api/v1/track/url", params={"id": "lx:tx:0039MnYb0qxYhV"})
        assert resp2.status_code == 404

        # 歌词走 QQ 官方接口仍可用
        resp3 = client.get("/api/v1/track/lyric", params={"id": "lx:tx:0039MnYb0qxYhV"})
        assert resp3.status_code == 200


def test_search_vip_verified_when_resolvable():
    """kg VIP 曲目在官方接口可解析且探活通过时应保留并带 verified 标记。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "mobilecdn.kugou.com" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "info": [
                            {
                                "hash": "VIPHASH",
                                "songname": "晴天",
                                "singername": "周杰伦",
                                "duration": 269000,
                                "pay_type": 3,  # VIP
                            }
                        ]
                    }
                },
            )
        if "m.kugou.com" in url:
            # 官方接口对 VIP hash 也返回了可用直链
            return httpx.Response(
                200,
                json={"errcode": 0, "url": "https://sharefs.kugou.com/vip.mp3", "fileSize": 4300000, "bitRate": 128},
            )
        if "sharefs.kugou.com" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 0-1/4300000"},
                content=b"ID3",
            )
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kg", "limit": 3})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [it["id"] for it in items] == ["lx:kg:VIPHASH"]
        assert items[0]["verified"] is True
        assert items[0]["pay_type"] == 3  # 元数据保留（诚实标记），可播性由探活实证


def test_search_empty_media_rejected_even_with_audio_mime():
    """A MIME label and file size cannot make an empty body valid media."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search.kuwo.cn" in url:
            return httpx.Response(200, text=_KW_RS_BODY)
        if "musicapi.haitangw.net" in url:
            return httpx.Response(302, headers={"Location": "https://car-er.kuwo.cn/trial.flac"})
        if "car-er.kuwo.cn/trial.flac" in url:
            # 269s 的歌只有 400KB（试听片段）
            return httpx.Response(
                206,
                headers={"Content-Type": "audio/x-flac", "Content-Range": "bytes 0-1/400000"},
            )
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "kw"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []  # 试听碎片被防护剔除


def test_circuit_breaker_opens_after_consecutive_failures():
    """链路连续失败 3 次后熔断，后续解析直接跳过不再发请求。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "musicapi.haitangw.net" in str(request.url):
            calls["n"] += 1
            return httpx.Response(500)
        return httpx.Response(404)

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            for _ in range(5):
                assert await lxapp.resolve_third_party(http, "kw", "228908", "lossless") is None
        finally:
            await http.aclose()
        return calls["n"]

    import asyncio

    made = asyncio.run(run())
    assert made == 3  # 第 4、5 次已被熔断跳过
    snap = lxapp.chain_health_snapshot()
    assert snap["changqing_kw"]["open"] is True
    assert snap["changqing_kw"]["breaks"] == 1


def test_track_url_tx_kw_invalid_source_alias():
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:xx:1"})
        assert resp.status_code == 400


# ------------------------------------------------- 链路层/探活器 单元覆盖 ---

def test_lenient_pydict_parses_python_literal():
    """酷我 r.s 返回单引号 Python 字面量，应能解析。"""

    class _FakeResp:
        text = "{'abslist':[{'MUSICRID':'MUSIC_1','SONGNAME':'A'}]}"

        def json(self):
            raise ValueError("not json")

    parsed = lxapp._lenient_pydict(_FakeResp(), "kw")
    assert parsed == {"abslist": [{"MUSICRID": "MUSIC_1", "SONGNAME": "A"}]}


def test_fresh_probe_tier_and_expiry():
    """探活缓存：低音质缓存不能满足高音质请求；过期不复用。"""
    import time as _time

    base = {"url": "https://cdn.test/a.flac", "ext": "flac", "headers": {},
            "probed": True, "validation_status": "media_verified"}
    # standard 缓存 → lossless 请求拒绝（需重新解析高音质）
    item = {"_probe": dict(base, ts=_time.time(), actual_tier="standard")}
    assert lxapp._fresh_probe(item, "lossless") is None
    # standard 缓存 → standard 请求复用
    got = lxapp._fresh_probe(item, "standard")
    assert got and got["url"].endswith(".flac") and "ts" not in got and "tier" not in got
    # lossless 缓存 → standard 请求也可复用（音质只高不低）
    item = {"_probe": dict(base, ts=_time.time(), actual_tier="lossless")}
    assert lxapp._fresh_probe(item, "standard") is not None
    # 过期缓存拒绝
    item = {"_probe": dict(base, ts=_time.time() - lxapp.CONF["probe_fresh_s"] - 1, tier="lossless")}
    assert lxapp._fresh_probe(item, "lossless") is None
    # 无缓存 / 非 dict
    assert lxapp._fresh_probe({}, "standard") is None
    assert lxapp._fresh_probe(None, "standard") is None


def test_title_relevance_ranking():
    """kw 候选排序：原版（title 即关键词主体）优先于含关键词的串烧，无关曲最后。"""
    r = lxapp._title_relevance
    assert r("晴天", "晴天 周杰伦") == 1  # 原版
    assert r("晴天 (KTV版伴奏)", "晴天 周杰伦") == 1  # 去括号后与原版同级
    assert r("晴天周杰伦串烧版", "晴天 周杰伦") == 1  # 标题以完整关键词开头，同级高相关
    assert r("超好听晴天周杰伦remix", "晴天 周杰伦") == 2  # 关键词在标题中间
    assert r("志明与春娇+晴天+双截棍", "晴天 周杰伦") == 3  # 仅含部分关键词，视同无关
    assert r("花海", "晴天 周杰伦") == 3  # 无关
    assert r("晴天", "晴天") == 0  # 精确匹配
    assert r("任意", "") == 3  # 空关键词兜底


def test_probe_url_rejects_html(monkeypatch):
    """探活器：HTML 响应（即使 200）必须拒绝；octet-stream 音频接受。"""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "html.test" in url:
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<html>404</html>")
        if "audio.test" in url:
            return httpx.Response(
                206,
                headers={"Content-Type": "application/octet-stream", "Content-Range": "bytes 0-1/1000"},
                content=b"ID3",
            )
        if "redir.test" in url:
            return httpx.Response(302, headers={"Location": "https://audio.test/x.mp3"})
        return httpx.Response(500)

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        try:
            ok_html, _, _, _ = await lxapp.probe_url(http, "https://html.test/x")
            ok_audio, final, ct, size = await lxapp.probe_url(http, "https://audio.test/x.mp3")
            ok_redir, final_r, _, _ = await lxapp.probe_url(http, "https://redir.test/x")
            return ok_html, ok_audio, final, ct, size, ok_redir, final_r
        finally:
            await http.aclose()

    ok_html, ok_audio, final, ct, size, ok_redir, final_r = asyncio.run(run())
    assert ok_html is False
    assert ok_audio is True and size == 1000
    assert ok_redir is True and final_r.endswith("x.mp3")


def test_resolve_third_party_skips_keyword_chain_without_meta():
    """溯音链路 needs_keyword：无 title/artist 时不发请求直接跳过。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 200, "title": "x", "music_url": "https://y.test/a.mp3"})

    import asyncio

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        try:
            return await lxapp.resolve_third_party(http, "mg", "600902", "standard", "", "")
        finally:
            await http.aclose()

    assert asyncio.run(run()) is None
    assert calls["n"] == 0  # 零请求


def test_search_tx_item_mapping_with_registered_chain(monkeypatch):
    """注册 tx 链路后 tx 搜索自动恢复：验证条目字段映射（封面/歌手拼接/元数据）。"""

    async def fake_tx_chain(client, ctx):
        return {
            "url": "https://audio.test/qq.flac",
            "ext": "flac",
            "file_size": 40000000,
            "br": 900000,
            "headers": dict(lxapp.TX_HEADERS),
            "probed": True,
        }

    monkeypatch.setattr(
        lxapp,
        "THIRD_PARTY_CHAIN",
        [{"name": "fake_tx", "platforms": {"tx"}, "needs_keyword": False, "fn": fake_tx_chain}],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "u.y.qq.com" in str(request.url):
            return httpx.Response(200, json=_TX_SEARCH_RESP)
        if "audio.test" in str(request.url):
            return httpx.Response(
                206, headers={"Content-Type": "audio/x-flac", "Content-Range": "bytes 0-1/40000000"}
            )
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "tx"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 2
        first = items[0]
        assert first["id"] == "lx:tx:0039MnYb0qxYhV"
        assert first["lx_source"] == "tx"
        assert first["verified"] is True
        assert first["ext"] == "flac"
        assert first["artist"] == "周杰伦"
        assert first["pay_type"] == 1  # pay.pay_play 映射
        assert first["duration_s"] == 269
        assert first["cover_url"] == (
            "https://y.gtimg.cn/music/photo_new/T002R300x300M000000MkMni19ClKG.jpg"
        )


def test_track_url_mg_falls_back_to_suyin():
    """mg 官方接口失败 → 溯音咪咕链路回退 → 探活通过。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "player_get_song_info" in url:
            return httpx.Response(404)  # 官方接口挂
        if "api.xcvts.cn" in url:
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "title": "晴天",
                    "singer": "周杰伦",
                    "music_url": "https://suyin.test/mg.mp3",
                },
            )
        if "suyin.test" in url:
            return httpx.Response(
                206, headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 0-1/9000000"}, content=b"ID3"
            )
        return httpx.Response(404)

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    lxapp._cache_put(
        {"id": "lx:mg:600902", "lx_source": "mg", "title": "晴天", "artist": "周杰伦", "duration_s": 269.0}
    )

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/url", params={"id": "lx:mg:600902", "quality": "standard"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["url"] == "https://suyin.test/mg.mp3"
        assert data["ext"] == "mp3"


def test_search_wy_vip_excluded_when_unresolvable():
    """wy VIP 候选（fee=1）在解析/探活全失败时仍被剔除（回归保护）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "music.163.com/api/search" in url:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "songs": [
                            {
                                "id": 777888,  # VIP 曲
                                "name": "晴天",
                                "artists": [{"name": "周杰伦"}],
                                "duration": 269000,
                                "fee": 1,
                            },
                            {
                                "id": 186016,  # 免费曲
                                "name": "晴天",
                                "artists": [{"name": "周杰伦"}],
                                "duration": 269000,
                                "fee": 0,
                            },
                        ]
                    }
                },
            )
        return httpx.Response(404)  # eapi/outer/链路全部失败

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/search", params={"keyword": "晴天", "sources": "wy"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [it["id"] for it in items] == ["lx:wy:186016"]  # VIP 曲未混入


def test_track_lyric_tx_base64_decode():
    """tx 歌词：c.y.qq.com 返回 base64，应解码并反转义 HTML 实体。"""
    import base64 as _b64

    lrc = "[00:01.00]晴天 - 周杰伦\n[00:05.30]故事的小黄花&#58;"
    encoded = _b64.b64encode(lrc.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("songmid") == "0039MnYb0qxYhV"
        return httpx.Response(200, json={"retcode": 0, "lyric": encoded})

    lxapp.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/lyric", params={"id": "lx:tx:0039MnYb0qxYhV"})
        assert resp.status_code == 200
        text = resp.json()["data"]["lyric"]
        assert text.startswith("[00:01.00]晴天 - 周杰伦")
        assert text.endswith("故事的小黄花:")  # &#58; → :


def test_track_lyric_kw_returns_empty():
    """kw 歌词接口已失效，返回空字符串而非报错。"""
    with TestClient(lxapp.app) as client:
        resp = client.get("/api/v1/track/lyric", params={"id": "lx:kw:228908"})
        assert resp.status_code == 200
        assert resp.json()["data"]["lyric"] == ""
