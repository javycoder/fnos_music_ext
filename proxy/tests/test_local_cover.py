import os
import tempfile
import pytest
import httpx
from starlette.testclient import TestClient

from proxy import app as proxy_app
from proxy.app import (
    app,
    _sniff_image_mime,
    _official_cover_root,
    _official_cover_file_response,
)


@pytest.fixture
def mock_cover_dir(monkeypatch, tmp_path):
    """创建模拟的官方封面目录结构。"""
    cover_root = tmp_path / "mock_covers"
    track_dir = cover_root / "track" / "83"
    track_dir.mkdir(parents=True)
    album_dir = cover_root / "album" / "e3"
    album_dir.mkdir(parents=True)

    # 1. track jpeg 原始封面 (FF D8 FF)
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100
    (track_dir / "8320f3c187014232a3ee4e1fce68bd34").write_bytes(jpeg_bytes)

    # 2. track 缩略图 (带 _w120.jpg 后缀)
    thumb_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF_THUMB" + b"\x00" * 50
    (track_dir / "8320f3c187014232a3ee4e1fce68bd34_w120.jpg").write_bytes(thumb_bytes)

    # 3. album webp 原始封面 (RIFF....WEBP)
    webp_bytes = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 30
    (album_dir / "e31e4664e744416c86ba8e67f3edde01").write_bytes(webp_bytes)

    monkeypatch.setenv("FNMUSIC_COVER_DIR", str(cover_root))
    monkeypatch.setattr(proxy_app, "_OFFICIAL_COVER_ROOT_CACHE", str(cover_root))
    proxy_app._OFFICIAL_COVER_MIME_CACHE.clear()

    return {
        "root": cover_root,
        "jpeg_bytes": jpeg_bytes,
        "thumb_bytes": thumb_bytes,
        "webp_bytes": webp_bytes,
    }


def test_sniff_image_mime():
    assert _sniff_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert _sniff_image_mime(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert _sniff_image_mime(b"RIFF\x20\x00\x00\x00WEBPVP8") == "image/webp"
    assert _sniff_image_mime(b"UNKNOWN_BYTES") == ""


def test_official_cover_root_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("FNMUSIC_COVER_DIR", str(tmp_path))
    monkeypatch.setattr(proxy_app, "_OFFICIAL_COVER_ROOT_CACHE", None)
    assert _official_cover_root() == str(tmp_path)


def test_official_cover_file_response_validation():
    # 非法字符 / 目录穿越
    assert _official_cover_file_response("../../etc/passwd") is None
    assert _official_cover_file_response("not_a_hex_guid_12345678901234567") is None
    assert _official_cover_file_response("8320f3c1") is None  # 长度不足32


def test_static_cover_track_jpeg_hit(mock_cover_dir):
    """官方封面存在时直接返回 200 FileResponse，不触达上游。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach upstream")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        # 1. 携带 track_ 前缀
        resp = client.get(
            "/music/api/v1/static/cover?coverId=track_8320f3c187014232a3ee4e1fce68bd34",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.content == mock_cover_dir["jpeg_bytes"]
        assert resp.headers.get("content-type") == "image/jpeg"
        assert resp.headers.get("etag") == '"8320f3c187014232a3ee4e1fce68bd34"'
        assert "max-age=86400" in resp.headers.get("cache-control", "")

        # 2. 携带 size=120 参数，优先命中缩略图
        resp_thumb = client.get(
            "/music/api/v1/static/cover?coverId=track_8320f3c187014232a3ee4e1fce68bd34&size=120",
            follow_redirects=False,
        )
        assert resp_thumb.status_code == 200
        assert resp_thumb.content == mock_cover_dir["thumb_bytes"]
        assert resp_thumb.headers.get("content-type") == "image/jpeg"


def test_static_cover_album_webp_hit(mock_cover_dir):
    """album 子目录 WebP 文件直读命中。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach upstream")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/static/cover?coverId=album_e31e4664e744416c86ba8e67f3edde01",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.content == mock_cover_dir["webp_bytes"]
        assert resp.headers.get("content-type") == "image/webp"


def test_static_cover_head_request(mock_cover_dir):
    """HEAD 请求支持。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach upstream")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        resp = client.head("/music/api/v1/static/cover?coverId=track_8320f3c187014232a3ee4e1fce68bd34")
        assert resp.status_code == 200
        assert resp.headers.get("content-type") == "image/jpeg"
        assert len(resp.content) == 0


def test_static_cover_missing_file_fallback_upstream(mock_cover_dir):
    """合法 32hex 但本地文件不存在，平滑落回上游透传，原样保留 coverId。"""
    called_cover_id = None

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_cover_id
        called_cover_id = request.url.params.get("coverId")
        return httpx.Response(200, content=b"upstream_img", headers={"content-type": "image/jpeg"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        # 该 32hex 并不存在于 mock_cover_dir
        missing_id = "track_ffffffffffffffffffffffffffffffff"
        resp = client.get(f"/music/api/v1/static/cover?coverId={missing_id}")
        assert resp.status_code == 200
        assert resp.content == b"upstream_img"
        assert called_cover_id == missing_id


def test_static_cover_non_hex_passthrough(mock_cover_dir):
    """非 32hex 标识符（如 local:track:999）直接透传上游。"""
    called_cover_id = None

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_cover_id
        called_cover_id = request.url.params.get("coverId")
        return httpx.Response(200, content=b"upstream_img", headers={"content-type": "image/jpeg"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/static/cover?coverId=local:track:999&size=120")
        assert resp.status_code == 200
        assert resp.content == b"upstream_img"
        assert called_cover_id == "local:track:999"
