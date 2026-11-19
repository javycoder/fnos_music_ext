"""歌手搜索原样透传（issue #139 回归防护）。

/search/artist 没有代理侧合并逻辑，走 catch_all 透传：查询串、鉴权头
（cookie/authx）与响应都必须原样往返，不得被搜索相关改动打扰。
"""
import httpx
from fastapi.testclient import TestClient

from proxy.app import app


def test_search_artist_forwards_query_and_headers_verbatim():
    seen: dict = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["cookie"] = request.headers.get("cookie")
        seen["authx"] = request.headers.get("authx")
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {
            "list": [{"guid": "artist-1", "name": "周杰伦"}], "total": 1,
        }}, headers={"x-upstream-test": "yes"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), base_url="http://unix")
    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/search/artist",
            params={"q": "周杰伦", "page": 1, "size": 24},
            headers={"cookie": "music-token=abc", "authx": "v2-signature"},
        )
    assert resp.status_code == 200
    assert seen["path"] == "/music/api/v1/search/artist"
    assert seen["params"] == {"q": "周杰伦", "page": "1", "size": "24"}
    assert seen["cookie"] == "music-token=abc"
    assert seen["authx"] == "v2-signature"
    assert seen["accept_encoding"] == "identity"
    body = resp.json()
    assert body["code"] == 0 and body["data"]["total"] == 1
    assert body["data"]["list"][0]["guid"] == "artist-1"
    assert resp.headers.get("x-upstream-test") == "yes"


def test_search_artist_official_error_passthrough():
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99999, "msg": "login", "data": None})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(deny), base_url="http://unix")
    with TestClient(app) as client:
        body = client.get("/music/api/v1/search/artist", params={"q": "x"}).json()
    assert body["code"] == 99999
