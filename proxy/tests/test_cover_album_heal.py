"""裸曲目 guid 封面归位（album_ 前缀）测试。

背景：无内嵌图曲目的 track.cover_guid 为空，推荐歌单构建器把它的封面引用
回落成曲目自身 guid（裸 32hex）。官方封面按类型前缀定位资源，裸 id 直读
未命中后透传官方必失败。归位逻辑两级：
  ① 同专辑任一有内嵌图曲目的 cover_guid（无网络快速路径）；
  ② 专辑内无曲目封面时经官方 /album/detail 取专辑 coverId（飞牛对已知
     专辑下发在线元数据封面，仅官方侧可解析其资源 guid）。
查不到维持原行为（透传/占位）。
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
LONELY_GUID = "cccc1111222233334444555566667777"    # 无内嵌图，专辑行缺失（无兜底可走）
OFFICIAL_ALBUM_GUID = "dddd1111222233334444555566667777"  # 无内嵌图，专辑封面来自官方在线元数据
ALBUM1_GUID = "1111111122233334444555666a1117777"[:32].ljust(32, "0")
COVER_RES = "e31e4664e744416c86ba8e67f3edde01"      # 专辑封面资源 guid（兄弟曲目直取）
OFFICIAL_COVER_RES = "f31e4664e744416c86ba8e67f3edde02"  # 官方在线元数据封面资源
ALBUM_COVER_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 30
OFFICIAL_COVER_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 40


@pytest.fixture
def heal_env(monkeypatch, tmp_path):
    """官方库（track/album 表）+ 官方封面目录 + 归位缓存清空。"""
    db_path = tmp_path / "music.db"
    con = sqlite3.connect(db_path)
    con.execute(
        """CREATE TABLE album (id INTEGER PRIMARY KEY, guid TEXT, name TEXT)""")
    con.execute(
        """CREATE TABLE track (
             guid TEXT PRIMARY KEY, album_id INTEGER, cover_guid TEXT,
             is_admin_deleted INTEGER DEFAULT 0,
             is_audio_file_deleted INTEGER DEFAULT 0)""")
    con.executemany(
        "INSERT INTO album (id, guid, name) VALUES (?,?,?)",
        [(1, ALBUM1_GUID, "兄弟封面专辑"),
         (3, OFFICIAL_ALBUM_GUID, "官方在线封面专辑")])
    con.executemany(
        "INSERT INTO track (guid, album_id, cover_guid) VALUES (?,?,?)",
        [(ARTFUL_GUID, 1, COVER_RES),
         (ARTLESS_GUID, 1, ""),
         (LONELY_GUID, 2, ""),          # album_id=2 无 album 行
         (OFFICIAL_ALBUM_GUID, 3, "")])  # 专辑内无曲目封面 → 走官方兜底
    con.commit()
    con.close()

    cover_root = tmp_path / "covers"
    for res, data in ((COVER_RES, ALBUM_COVER_BYTES),
                      (OFFICIAL_COVER_RES, OFFICIAL_COVER_BYTES)):
        d = cover_root / "album" / res[:2]
        d.mkdir(parents=True, exist_ok=True)
        (d / res).write_bytes(data)

    monkeypatch.setitem(proxy_app.CONF, "music_db", str(db_path))
    monkeypatch.setenv("FNMUSIC_COVER_DIR", str(cover_root))
    monkeypatch.setattr(proxy_app, "_OFFICIAL_COVER_ROOT_CACHE", str(cover_root))
    proxy_app._OFFICIAL_COVER_MIME_CACHE.clear()
    proxy_app._ALBUM_COVER_HEAL_CACHE.clear()
    yield {"db": db_path, "cover_root": cover_root}
    proxy_app._ALBUM_COVER_HEAL_CACHE.clear()


class _FakeRequest:
    """_album_cover_heal_ref / _playlist_cover_response 直调的最小请求桩。"""

    def __init__(self, params=None):
        self.query_params = dict(params or {})
        self.headers = {}
        self.app = app


def _mock_upstream(handler):
    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://unix")


def _heal(request, guid):
    return asyncio.run(_album_cover_heal_ref(request, guid))


def test_heal_ref_maps_bare_track_to_album_cover(heal_env):
    # 快速路径：同专辑兄弟曲目的 cover_guid
    assert _heal(_FakeRequest(), ARTLESS_GUID) == f"album_{COVER_RES}"
    # 大小写不敏感
    assert _heal(_FakeRequest(), ARTLESS_GUID.upper()) == f"album_{COVER_RES}"


def test_heal_ref_official_album_fallback(heal_env):
    """专辑内无任何曲目封面：经官方 album/detail 取专辑 coverId。"""
    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(str(request.url))
        assert "guid=" + OFFICIAL_ALBUM_GUID in str(request.url)
        return httpx.Response(200, json={
            "code": 0, "msg": "ok",
            "data": {"guid": OFFICIAL_ALBUM_GUID, "name": "在线封面专辑",
                     "coverId": f"album_{OFFICIAL_COVER_RES}"}})

    _mock_upstream(handler)
    assert _heal(_FakeRequest(), OFFICIAL_ALBUM_GUID) == f"album_{OFFICIAL_COVER_RES}"
    assert seen_paths  # 确实走了官方 album/detail


def test_heal_ref_negative_cases(heal_env):
    # 专辑行缺失（无兜底可走）/ 未知 guid / 非裸 32hex 形态
    assert _heal(_FakeRequest(), LONELY_GUID) == ""
    assert _heal(_FakeRequest(), "eeee1111222233334444555566667777") == ""
    assert _heal(_FakeRequest(), f"album_{COVER_RES}") == ""
    assert _heal(_FakeRequest(), f"track_{ARTLESS_GUID}") == ""
    assert _heal(_FakeRequest(), "") == ""


def test_heal_ref_caches_result(heal_env):
    assert _heal(_FakeRequest(), ARTLESS_GUID) == f"album_{COVER_RES}"
    heal_env["db"].unlink()  # 缓存生效：删库后仍拿得到
    assert _heal(_FakeRequest(), ARTLESS_GUID) == f"album_{COVER_RES}"
    proxy_app._ALBUM_COVER_HEAL_CACHE.clear()
    assert _heal(_FakeRequest(), ARTLESS_GUID) == ""


def test_static_cover_bare_track_healed_to_album_cover(heal_env):
    """裸曲目 guid 请求 → 兄弟封面资源直读命中，不触达上游。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach upstream")

    _mock_upstream(upstream_handler)

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/static/cover?coverId={ARTLESS_GUID}")
        assert resp.status_code == 200
        assert resp.content == ALBUM_COVER_BYTES
        assert resp.headers.get("content-type") == "image/webp"


def test_static_cover_heal_official_fallback(heal_env):
    """无曲目封面的专辑：归位经官方 album/detail 拿 coverId 后直读官方封面文件。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert "/music/api/v1/album/detail" in str(request.url)
        return httpx.Response(200, json={
            "code": 0, "msg": "ok",
            "data": {"coverId": f"album_{OFFICIAL_COVER_RES}"}})

    _mock_upstream(upstream_handler)

    with TestClient(app) as client:
        resp = client.get(
            f"/music/api/v1/static/cover?coverId={OFFICIAL_ALBUM_GUID}&size=160")
        assert resp.status_code == 200
        assert resp.content == OFFICIAL_COVER_BYTES
        assert resp.headers.get("content-type") == "image/jpeg"


def test_static_cover_coverless_album_passthrough(heal_env):
    """归位失败（无专辑行/无封面）：维持原行为——裸 id 原样透传官方。"""
    called_cover_id = None

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_cover_id
        called_cover_id = request.url.params.get("coverId")
        return httpx.Response(
            200, content=b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 20,
            headers={"content-type": "image/jpeg"})

    _mock_upstream(upstream_handler)

    with TestClient(app) as client:
        resp = client.get(f"/music/api/v1/static/cover?coverId={LONELY_GUID}")
        assert resp.status_code == 200
        assert called_cover_id == LONELY_GUID


@pytest.mark.parametrize("album_data", [["unexpected"], {"coverId": "album_invalid"}])
def test_static_cover_malformed_album_keeps_original_fallback(heal_env, album_data):
    """专辑详情异常时，封面端点仍按原曲目标识回退。"""
    seen_cover_ids = []

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/album/detail":
            return httpx.Response(200, json={"code": 0, "data": album_data})
        seen_cover_ids.append(request.url.params.get("coverId"))
        return httpx.Response(200, content=OFFICIAL_COVER_BYTES,
                              headers={"content-type": "image/jpeg"})

    _mock_upstream(upstream_handler)

    with TestClient(app) as client:
        response = client.get("/music/api/v1/static/cover",
                              params={"coverId": OFFICIAL_ALBUM_GUID})
        assert response.status_code == 200
        assert response.content == OFFICIAL_COVER_BYTES
        assert seen_cover_ids == [OFFICIAL_ALBUM_GUID]
        assert OFFICIAL_ALBUM_GUID not in proxy_app._ALBUM_COVER_HEAL_CACHE


def test_playlist_cover_response_heals_bare_candidate(heal_env):
    """歌单卡片封面候选里的裸 coverId 同样归位（候选封面取专辑资源）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach upstream")

    _mock_upstream(upstream_handler)

    async def run():
        return await _playlist_cover_response(
            _FakeRequest({"size": "120"}), "pl-anything",
            [{"guid": ARTLESS_GUID, "coverId": ARTLESS_GUID}], "")

    resp = asyncio.run(run())
    assert resp.status_code == 200
    # FileResponse：按官方封面资源文件直出断言
    assert str(getattr(resp, "path", "")).endswith(COVER_RES)
