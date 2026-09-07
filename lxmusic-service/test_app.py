"""lxmusic-service 单元测试：ID 契约 / 搜索 / trackercdn hash 解析 / eapi 参数。"""
import httpx
import pytest
from fastapi.testclient import TestClient

import app as lxapp
from app import parse_track_id, normalize_source, _quality_tiers, _kg_hash_for_quality, _eapi_params


@pytest.fixture(autouse=True)
def setup_http(monkeypatch):
    lxapp._SONG_CACHE.clear()

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
    assert parse_track_id("kg:ABC123") == ("kg", "ABC123")
    assert parse_track_id("bad") == ("", "")
    assert parse_track_id("lx:xx:1") == ("", "")


def test_normalize_source():
    assert normalize_source("kugou") == "kg"
    assert normalize_source("KG") == "kg"
    assert normalize_source("netease") == "wy"
    assert normalize_source("migu") == "mg"
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
        assert set(rj["sources"]) == {"kg", "wy", "mg"}


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
    import base64
    from Crypto.Cipher import AES

    raw = base64.b64decode(params)
    plain = AES.new(lxapp._EAPI_KEY, AES.MODE_ECB).decrypt(raw)
    # PKCS7 去填充
    pad = plain[-1]
    plain = plain[:-pad]
    text = plain.decode("utf-8", errors="replace")
    assert text.startswith("/api/song/enhance/player/url-36cd479b6b5-")
    assert "-36cd479b6b5-" in text  # 末段为 md5 摘要
    digest = text.rsplit("-36cd479b6b5-", 1)[-1]
    assert len(digest) == 32 and digest == digest.lower()
