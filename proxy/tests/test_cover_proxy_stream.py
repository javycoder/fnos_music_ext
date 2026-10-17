"""在线封面同源直出（issue #48）：官方手机 App 不跟随跨域 302，封面端点改为
代理直出图片字节；磁盘缓存、失败回退、redirect 逃生模式与取证日志的回归测试。"""
import logging
import os
import re

import httpx
import pytest
from starlette.testclient import TestClient

from proxy import app as proxy_app
from proxy.app import CONF, app, fake_official_guid

IMG_BYTES = b"\xFF\xD8\xFF\xE0" + b"\x00" * 40


@pytest.fixture(autouse=True)
def cover_env(tmp_path, monkeypatch):
    """隔离缓存目录与封面磁盘缓存节流状态；在线封面默认代理直出。"""
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "cover_mode", "proxy")
    monkeypatch.setattr(proxy_app, "_COVER_UNRESOLVED_SEEN", set())
    monkeypatch.setattr(proxy_app, "_cover_disk_last_clean", 0.0)


def _wire_clients(monkeypatch, musicdl_handler, upstream_status=404):
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(upstream_status, text="no upstream")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8770"
    )
    app.state.lx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), base_url="http://127.0.0.1:8772"
    )


def _migu_info_handler(cover_url: str, calls: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls["n"] += 1
        assert request.url.path == "/info"
        return httpx.Response(200, json={"ok": True, "id": "migu:9", "source": "migu",
                                          "title": "歌", "artist": "手", "cover_url": cover_url})
    return handler


@pytest.mark.asyncio
async def test_playlist_cover_candidates_bounded_and_placeholder(monkeypatch):
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [], "query_string": b""})
    tried = []

    async def delivery(url):
        tried.append(url)
        if url.endswith("good"):
            return proxy_app.Response(content=IMG_BYTES, media_type="image/jpeg")
        return None

    monkeypatch.setattr(proxy_app, "_proxied_cover_response", delivery)
    monkeypatch.setattr(proxy_app, "_cover_guid_url_lookup", lambda guid: "")
    tracks = [{"cover_url": "https://image.test/" + name} for name in ("bad", "good", "other", "extra")]
    result = await proxy_app._playlist_cover_response(request, "playlist", tracks)
    assert result.body == IMG_BYTES
    assert len(tried) == 2
    tried.clear()
    result = await proxy_app._playlist_cover_response(request, "playlist", tracks[2:] * 3)
    assert result.headers["cache-control"] == "no-store"
    assert result.body.startswith(b"\x89PNG")
    assert len(tried) == 2
    tried.clear()
    result = await proxy_app._playlist_cover_response(request, "playlist", [
        {"cover_url": f"https://image.test/bad{number}"} for number in range(10)])
    assert len(tried) == 3
    assert result.headers["cache-control"] == "no-store"


def test_cover_proxied_bytes_and_disk_cache(tmp_path, monkeypatch):
    """在线封面 200 直出字节；第二次请求命中磁盘缓存，不再打 info/CDN。"""
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    info_calls = {"n": 0}
    _wire_clients(monkeypatch, _migu_info_handler("http://img.test/migu9.jpg", info_calls))
    img_calls = {"n": 0}

    def img_handler(request: httpx.Request) -> httpx.Response:
        img_calls["n"] += 1
        assert str(request.url) == "http://img.test/migu9.jpg"
        return httpx.Response(200, content=IMG_BYTES, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(proxy_app, "_COVER_FETCH_ASYNC_TRANSPORT", httpx.MockTransport(img_handler))
    with TestClient(app) as client:
        r1 = client.get("/music/api/v1/static/cover?coverId=online:migu:9", follow_redirects=False)
        r2 = client.get("/music/api/v1/static/cover?coverId=online:migu:9", follow_redirects=False)
    assert r1.status_code == 200
    assert r1.headers["content-type"].startswith("image/jpeg")
    assert r1.content == IMG_BYTES
    assert r1.headers["cache-control"] == "public, max-age=86400"
    assert r1.headers.get("etag")
    assert r2.status_code == 200 and r2.content == IMG_BYTES


    # info 只打一次（info 自身有进程内缓存），封面 CDN 也只拉一次（第二次走磁盘缓存）
    assert img_calls["n"] == 1
    covers_dir = os.path.join(str(tmp_path / "cache"), "covers")
    files = os.listdir(covers_dir)
    assert os.path.isdir(covers_dir)
    # 1 张封面字节文件 + 1 个 guid→直链 映射文件（.url，info 抖动时已成功封面不塌）
    assert len([f for f in files if not f.endswith(".url")]) == 1
    assert len([f for f in files if f.endswith(".url")]) == 1


def test_legacy_track_snapshot_cover_without_info(monkeypatch):
    _wire_clients(monkeypatch, lambda request: pytest.fail("must not query song info or stream"))
    monkeypatch.setattr(proxy_app, "_cover_guid_url_lookup", lambda guid: "")
    monkeypatch.setattr(proxy_app, "_lookup_playlist_cache_track", lambda guid: {
        "guid": guid, "cover_url": "https://image.test/snapshot.jpg"})
    monkeypatch.setattr(proxy_app, "_COVER_FETCH_ASYNC_TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(200, content=IMG_BYTES)))
    with TestClient(app) as client:
        cover_id = "track_" + proxy_app.fake_official_guid("online:migu:snapshot")
        response = client.get("/music/api/v1/static/cover/" + cover_id + "_w120.jpg")
        assert response.status_code == 200 and response.content == IMG_BYTES
        response = client.head("/music/api/v1/static/cover", params={"coverId": cover_id + ".jpg"})
        assert response.status_code == 200 and response.content == b""


def test_cover_proxied_head(tmp_path, monkeypatch):
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    _wire_clients(monkeypatch, _migu_info_handler("http://img.test/migu9.jpg"))
    monkeypatch.setattr(proxy_app, "_COVER_FETCH_ASYNC_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(200, content=IMG_BYTES, headers={"content-type": "image/jpeg"})))
    with TestClient(app) as client:
        resp = client.head("/music/api/v1/static/cover?coverId=online:migu:9")
    assert resp.status_code == 200


def test_cover_fetch_failure_falls_to_placeholder(tmp_path, monkeypatch):
    """封面拉取失败（CDN 5xx）：回退链走完落占位图，且占位图 no-store 不被客户端粘住。"""
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    _wire_clients(monkeypatch, _migu_info_handler("http://img.test/dead.jpg"))
    monkeypatch.setattr(proxy_app, "_COVER_FETCH_ASYNC_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(500, text="cdn down")))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/static/cover?coverId=online:migu:9", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.headers["cache-control"] == "no-store"


def test_cover_subpath_suffix_stripped(tmp_path, monkeypatch):
    """客户端按文件形态请求封面（<fake>.jpg / <fake>_w120.jpg）仍能反解直出。"""
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    _wire_clients(monkeypatch, _migu_info_handler("http://img.test/migu9.jpg"))
    monkeypatch.setattr(proxy_app, "_COVER_FETCH_ASYNC_TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(200, content=IMG_BYTES, headers={"content-type": "image/jpeg"})))
    fake = fake_official_guid("online:migu:9")
    with TestClient(app) as client:
        r1 = client.get(f"/music/api/v1/static/cover/track_{fake}.jpg", follow_redirects=False)
        r2 = client.get(f"/music/api/v1/static/cover/track_{fake}_w120.jpg", follow_redirects=False)
    assert r1.status_code == 200 and r1.content == IMG_BYTES
    assert r2.status_code == 200 and r2.content == IMG_BYTES


def test_cover_id_unresolved_diag_logged_once(tmp_path, monkeypatch, caplog):
    """32hex coverId 不在反查表且官方文件未命中：打一次取证日志（重启后热身窗口）。"""
    _wire_clients(monkeypatch, lambda r: httpx.Response(404))
    missing = "a" * 32
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        with TestClient(app) as client:
            client.get(f"/music/api/v1/static/cover?coverId=track_{missing}", follow_redirects=False)
            client.get(f"/music/api/v1/static/cover?coverId=track_{missing}", follow_redirects=False)
    diags = [r.message for r in caplog.records if r.message.startswith("[diag] cover_id_unresolved")]
    assert len(diags) == 1
    assert missing in diags[0]


def test_cover_mode_redirect_keeps_302(tmp_path, monkeypatch):
    """FNMUSIC_COVER_MODE=redirect 逃生：保留旧 302 外链行为。"""
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "cover_mode", "redirect")
    _wire_clients(monkeypatch, _migu_info_handler("http://img.test/migu9.jpg"))
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/static/cover?coverId=online:migu:9", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "http://img.test/migu9.jpg"


def test_cover_disk_cache_roundtrip_and_gc(tmp_path, monkeypatch):
    """封面磁盘缓存：写读一致；TTL 过期被惰性清理剔除。"""
    from proxy.app import _cover_disk_gc, _cover_disk_path, _cover_disk_read, _cover_disk_write

    url = "http://img.test/x.jpg"
    _cover_disk_write(url, IMG_BYTES, "image/jpeg")
    got = _cover_disk_read(url)
    assert got and got[0] == IMG_BYTES and got[1] == "image/jpeg"
    fpath = _cover_disk_path(url, "image/jpeg")
    assert os.path.isfile(fpath)

    # 回拨 mime 模拟 TTL 过期：读取 miss、gc 剔除（重置节流标记让 gc 立即执行）
    old = max(0, os.stat(fpath).st_mtime - proxy_app._COVER_DISK_TTL_S - 10)
    os.utime(fpath, (old, old))
    assert _cover_disk_read(url) is None
    monkeypatch.setattr(proxy_app, "_cover_disk_last_clean", 0.0)
    _cover_disk_gc()
    assert not os.path.isfile(fpath)


def test_cover_proxied_rejects_bad_urls():
    """非 http(s)/酷我文本假图直出拒绝，返回 None 交回退链。"""
    import asyncio

    assert asyncio.run(proxy_app._proxied_cover_response("")) is None
    assert asyncio.run(proxy_app._proxied_cover_response("ftp://x/y.jpg")) is None
    text_url = f"https://{proxy_app._KW_TEXT_COVER_HOST}/pic?rid=1"
    assert asyncio.run(proxy_app._proxied_cover_response(text_url)) is None
