"""裸曲目 guid 封面归位（album_ 前缀）测试。

背景：无内嵌图曲目的 track.cover_guid 为空，推荐歌单构建器把它的封面引用
回落成曲目自身 guid（裸 32hex）。官方封面按类型前缀定位资源，裸 id 直读
未命中后透传官方必失败。归位逻辑：裸曲目 guid → 同专辑任一有内嵌图曲目
的 cover_guid（album_ 前缀）；查不到维持原行为（透传/占位）。
"""
import asyncio
import sqlite3

import httpx
import pytest
from starlette.testclient import TestClient

from proxy import app as proxy_app
from proxy.app import (
    app,
    _album_cover_heal_ref,
    _playlist_cover_response,
)


ARTFUL_GUID = "aaaa1111222233334444555566667777"      # 有内嵌图（cover_guid 非空）
ARTLESS_GUID = "bbbb1111222233334444555566667777"    # 无内嵌图，同专辑有封面
LONELY_GUID = "cccc1111222233334444555566667777"    # 无内嵌图，全专辑无封面
COVER_RES = "e31e4664e744416c86ba8e67f3edde01"      # 专辑封面资源 guid
ALBUM_COVER_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 30


@pytest.fixture
def heal_env(monkeypatch, tmp_path):
    """官方库（track 表）+ 官方封面目录 + 归位缓存清空。"""
    db_path = tmp_path / "music.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """CREATE TABLE track (
             guid TEXT PRIMARY KEY, album_id INTEGER, cover_guid TEXT,
             is_admin_deleted INTEGER DEFAULT 0,
             is_audio_file_deleted INTEGER DEFAULT 0)""")
    con.executemany(
        "INSERT INTO track (guid, album_id, cover_guid) VALUES (?,?,?)",
        [(ARTFUL_GUID, 1, COVER_RES),
         (ARTLESS_GUID, 1, ""),
         (LONELY_GUID, 2, "")])
    con.commit()
    con.close()

    cover_root = tmp_path / "covers"
    album_dir = cover_root / "album" / COVER_RES[:2]
    album_dir.mkdir(parents=True)
    (album_dir / COVER_RES).write_bytes(ALBUM_COVER_BYTES)

    monkeypatch.setitem(proxy_app.CONF, "music_db", str(db_path))
    monkeypatch.setenv("FNMUSIC_COVER_DIR", str(cover_root))
    monkeypatch.setattr(proxy_app, "_OFFICIAL_COVER_ROOT_CACHE", str(cover_root))
    proxy_app._OFFICIAL_COVER_MIME_CACHE.clear()
    proxy_app._ALBUM_COVER_HEAL_CACHE.clear()
    yield {"db": db_path, "cover_root": cover_root}
    proxy_app._ALBUM_COVER_HEAL_CACHE.clear()


class _FakeRequest:
    """_playlist_cover_response 直调所需的最小请求桩。"""

    def __init__(self, params=None):
        self.query_params = dict(params or {})
        self.app = app


def test_heal_ref_maps_bare_track_to_album_cover(heal_env):
    assert _album_cover_heal_ref(ARTLESS_GUID) == f"album_{COVER_RES}"
    # 大小写不敏感
    assert _album_cover_heal_ref(ARTLESS_GUID.upper()) == f"album_{COVER_RES}"


def test_heal_ref_negative_cases(heal_env):
    # 全专辑无封面 / 未知 guid / 非裸 32hex 形态
    assert _album_cover_heal_ref(LONELY_GUID) == ""
    assert _album_cover_heal_ref("dddd1111222233334444555566667777") == ""
    assert _album_cover_heal_ref(f"album_{COVER_RES}") == ""
    assert _album_cover_heal_ref(f"track_{ARTLESS_GUID}") == ""
    assert _album_cover_heal_ref("") == ""


def test_heal_ref_caches_result(heal_env):
    assert _album_cover_heal_ref(ARTLESS_GUID) == f"album_{COVER_RES}"
    heal_env["db"].unlink()  # 缓存生效：删库后仍拿得到
    assert _album_cover_heal_ref(ARTLESS_GUID) == f"album_{COVER_RES}"
    proxy_app._ALBUM_COVER_HEAL_CACHE.clear()
    assert _album_cover_heal_ref(ARTLESS_GUID) == ""


def test_static_cover_bare_track_healed_to_album_cover(heal_env):
    """裸曲目 guid 请求 → 归位到专辑封面资源直读命中，不触达上游。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach upstream")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/static/cover?coverId={ARTLESS_GUID}")
        assert resp.status_code == 200
        assert resp.content == ALBUM_COVER_BYTES
        assert resp.headers.get("content-type") == "image/webp"


def test_static_cover_heal_falls_back_to_prefixed_upstream(heal_env):
    """归位成功但封面文件缺失（如缩略图变体未提取）：带 album_ 前缀转发官方。"""
    (heal_env["cover_root"] / "album" / COVER_RES[:2] / COVER_RES).unlink()
    called_cover_id = None
    upstream_img = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 40

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_cover_id
        called_cover_id = request.url.params.get("coverId")
        return httpx.Response(
            200, content=upstream_img, headers={"content-type": "image/jpeg"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/static/cover?coverId={ARTLESS_GUID}&size=160")
        assert resp.status_code == 200
        assert resp.content == upstream_img
        assert called_cover_id == f"album_{COVER_RES}"


def test_static_cover_coverless_album_passthrough(heal_env):
    """归位失败（全专辑无封面）：维持原行为——裸 id 原样透传官方。"""
    called_cover_id = None

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_cover_id
        called_cover_id = request.url.params.get("coverId")
        return httpx.Response(
            200, content=b"upstream_img", headers={"content-type": "image/jpeg"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/static/cover?coverId={LONELY_GUID}")
        assert resp.status_code == 200
        assert resp.content == b"upstream_img"
        assert called_cover_id == LONELY_GUID


def test_playlist_cover_response_heals_bare_candidate(heal_env):
    """歌单卡片封面候选里的裸 coverId 同样归位（候选封面取专辑资源）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach upstream")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )

    async def run():
        return await _playlist_cover_response(
            _FakeRequest({"size": "120"}), "pl-anything",
            [{"guid": ARTLESS_GUID, "coverId": ARTLESS_GUID}], "")

    resp = asyncio.run(run())
    assert resp.status_code == 200
    # FileResponse：按官方封面资源文件直出断言
    assert str(getattr(resp, "path", "")).endswith(COVER_RES)
