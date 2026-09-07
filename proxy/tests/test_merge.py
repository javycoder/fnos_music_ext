"""Tests for fnmusic-ext proxy (search merge, streaming tee cache, passthrough)."""
import os
import pytest
import httpx
from fastapi.testclient import TestClient

from proxy.app import (
    app,
    CONF,
    _SEARCH_CACHE,
    find_cache_file,
    library_basename,
    remember_media_path,
    write_audio_tags,
)


def _assert_playback_metadata_shape(data: dict, guid: str) -> None:
    """飞牛 _h()：data.track.genres.join / album 对象 / artists 列表，缺一即跳过播放。"""
    track = data["track"]
    assert track["guid"] == guid
    assert isinstance(track["artists"], list)
    assert isinstance(track["genres"], list)
    " / ".join(track["genres"])
    assert isinstance(track["album"], dict)
    assert "name" in track["album"]
    spec = data["audioSpec"]
    assert spec.get("format")
    assert spec.get("channel") == 2
    assert "size" in spec


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    cache_dir = str(tmp_path / "cache")
    library_dir = str(tmp_path / "library")
    fav_dir = str(tmp_path / "online_favorites")
    os.makedirs(library_dir, exist_ok=True)
    os.makedirs(fav_dir, exist_ok=True)
    monkeypatch.setitem(CONF, "cache_dir", cache_dir)
    monkeypatch.setitem(CONF, "library_dir", library_dir)
    monkeypatch.setitem(CONF, "fav_dir", fav_dir)
    monkeypatch.setitem(CONF, "search_list_path", "data.list")
    monkeypatch.setitem(CONF, "online_limit", 30)
    monkeypatch.setitem(CONF, "netease_search_limit", 50)
    monkeypatch.setitem(CONF, "merge_suggest", False)
    monkeypatch.setitem(CONF, "lyric_field", "data.lyric")
    monkeypatch.setitem(CONF, "musicdl_enabled", True)
    monkeypatch.setitem(CONF, "netease_enabled", True)
    monkeypatch.setitem(CONF, "lx_enabled", False)
    monkeypatch.setitem(CONF, "netease_wait_s", 2.5)
    monkeypatch.setitem(CONF, "netease_quality", "lossless")
    monkeypatch.setitem(CONF, "search_cache_ttl", 300.0)
    monkeypatch.setitem(CONF, "late_page_wait_s", 5.0)

    def default_musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"ok": False, "data": []})

    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(default_musicbox_handler), base_url="http://127.0.0.1:8770"
    )


def test_library_basename_omits_source_id():
    assert library_basename("晴天", "周杰伦") == "周杰伦 - 晴天"
    assert library_basename("不再犹豫", "BEYOND") == "BEYOND - 不再犹豫"
    assert library_basename("晴天", "") == "晴天"
    assert "600902" not in library_basename("晴天", "周杰伦")


def test_find_cache_file_legacy_id_name_and_ref(tmp_path):
    guid = "online:migu:600902000006889366"
    legacy = os.path.join(CONF["library_dir"], "晴天 - 600902000006889366.mp3")
    with open(legacy, "wb") as f:
        f.write(b"x" * 2048)
    assert find_cache_file(guid) == legacy

    renamed = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.mp3")
    os.rename(legacy, renamed)
    remember_media_path(guid, renamed)
    assert find_cache_file(guid) == renamed


def test_write_audio_tags_id3(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = str(tmp_path / "sample.mp3")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "0.2",
            "-q:a",
            "9",
            path,
        ],
        check=True,
        capture_output=True,
    )
    write_audio_tags(path, title="晴天", artist="周杰伦", album="叶惠美")
    from mutagen import File as MutagenFile

    tagged = MutagenFile(path, easy=True)
    assert tagged is not None
    assert tagged["title"] == ["晴天"]
    assert tagged["artist"] == ["周杰伦"]
    assert tagged["album"] == ["叶惠美"]


def test_search_track_merge_success():
    """用例 a: 上游 code 0 + 列表 → 合并追加 online 条目、guid 前缀正确。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "code": 0,
            "msg": "OK",
            "data": {
                "list": [
                    {
                        "guid": "local:101",
                        "title": "夜曲",
                        "artist": "周杰伦",
                        "album": "十一月的萧邦",
                        "duration": 226000,
                    }
                ],
                "total": 1,
            },
        }
        return httpx.Response(200, json=data)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "ok": True,
            "data": [
                {
                    "song_id": "228908",
                    "song_name": "晴天",
                    "artist": "周杰伦",
                    "album_name": "叶惠美",
                    "duration": 269,
                    "quality": "SQ 2.4M",
                }
            ],
        }
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=夜曲")
        assert resp.status_code == 200
        res_json = resp.json()
        assert res_json["code"] == 0
        items = res_json["data"]["list"]
        assert len(items) == 2
        # 本地条目保持不变
        assert items[0]["guid"] == "local:101"
        assert items[0]["title"] == "夜曲"
        # 在线条目合并追加且格式正确
        assert items[1]["guid"] == "online:netease:228908"
        assert items[1]["title"] == "晴天"
        assert items[1]["artist"] == "周杰伦"
        assert items[1]["albumName"] == "叶惠美" and items[1]["album"]["name"] == "叶惠美"
        assert items[1]["duration_ms"] == 269000
        assert items[1]["durationMs"] == 269000
        assert items[1]["codec"] == "flac"
        assert items[1]["format"] == "flac"
        assert items[1]["is_online"] is True
        assert items[1]["artists"][0]["name"] == "周杰伦"
        assert res_json["data"]["total"] == 2


def test_search_track_merge_with_q_param():
    """前端打包使用 q 而不是 keyword，在线合并仍要生效。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "msg": "OK", "data": {"list": [], "total": 0}},
        )

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("keyword") == "晴天"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "1",
                        "song_name": "晴天",
                        "artist": "周杰伦",
                        "album_name": "叶惠美",
                        "duration": 269,
                        "quality": "LD 128k",
                    }
                ],
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=晴天&page=1&size=20")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert len(items) == 1
        assert items[0]["guid"] == "online:netease:1"


def test_search_track_preserves_lossless_and_common_formats():
    """在线结果按源站真实格式声明 audioSpec（flac/wav/m4a），不再一律伪装 mp3。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "msg": "", "data": {"list": [], "total": 0}},
        )

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "flac1",
                        "song_name": "不再犹豫",
                        "artist": "Beyond",
                        "album_name": "犹豫",
                        "duration": 240,
                        "quality": "SQ 2.4M",
                    }
                ],
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {
                        "id": "migu:m4a1",
                        "source": "migu",
                        "title": "海阔天空",
                        "artist": "Beyond",
                        "duration_s": 326,
                        "ext": "aac",
                    },
                    {
                        "id": "kuwo:wav1",
                        "source": "kuwo",
                        "title": "光辉岁月",
                        "artist": "Beyond",
                        "duration_s": 300,
                        "ext": "wav",
                    },
                ],
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=不再犹豫&page=1&size=50")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert resp.json()["data"]["total"] == 3
        by_guid = {it["guid"]: it for it in items}
        flac = by_guid["online:netease:flac1"]
        assert flac["format"] == "flac"
        assert flac["audioSpec"]["format"] == "flac"
        assert flac["audioSpec"]["path"].endswith(".flac")
        assert flac["coverId"] == "online:netease:flac1"
        assert by_guid["online:migu:m4a1"]["format"] == "m4a"
        assert by_guid["online:kuwo:wav1"]["format"] == "wav"
        assert by_guid["online:kuwo:wav1"]["audioSpec"]["bitDepth"] == 16


def test_search_track_merges_when_upstream_data_null_list_missing():
    """上游 code=0 但 data 为 null 时仍应合成 list 并合并在线结果。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": [
                    {
                        "song_id": "1",
                        "song_name": "不再犹豫",
                        "artist": "Beyond",
                        "duration": 240,
                        "quality": "SQ",
                    }
                ],
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?q=不再犹豫")
        items = resp.json()["data"]["list"]
        assert len(items) == 1
        assert items[0]["guid"] == "online:netease:1"
        assert items[0]["format"] == "flac"


def test_metadata_uses_source_ext_not_forced_mp3():
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "id": "kuwo:flac1",
                "title": "不再犹豫",
                "artist": "Beyond",
                "duration_s": 240,
                "ext": "flac",
                "file_size": 28000000,
                "cover_url": "http://img.test/c.jpg",
                "source": "kuwo",
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/metadata?guid=online:kuwo:flac1")
        spec = resp.json()["data"]["audioSpec"]
        assert spec["format"] == "flac"
        assert spec["codec"] == "flac"
        assert spec["path"].endswith(".flac")
        _assert_playback_metadata_shape(resp.json()["data"], guid="online:kuwo:flac1")


def test_search_track_upstream_unauthorized():
    """用例 b: 上游 99999 (未登录/INVALID TOKEN) → 原样透传不合并，且快速路径绝不调用 musicdl。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {"code": 99999, "msg": "INVALID TOKEN", "data": None}
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        # 并行预取可能被触发，但 401 路径必须立刻返回、不依赖该响应
        return httpx.Response(200, json={"ok": True, "items": [{"id": "should-not-merge"}]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=test")
        assert resp.status_code == 200
        res_json = resp.json()
        assert res_json["code"] == 99999
        assert res_json["msg"] == "INVALID TOKEN"
        assert res_json["data"] is None


def test_search_track_upstream_http_401_fast_path():
    """上游 HTTP 401 非 200 响应 → 快速路径原样透传，不调用 musicdl。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 99999, "msg": "UNAUTHORIZED"})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=test")
        assert resp.status_code == 401
        assert resp.json()["code"] == 99999


def test_search_suggest_upstream_unauthorized_fast_path(monkeypatch):
    """开启 suggest 合并时，若上游返回 99999，快速路径直接返回，不调用 musicdl。"""
    monkeypatch.setitem(CONF, "merge_suggest", True)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": [{"title": "should-not-merge"}]})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=test")
        assert resp.status_code == 200
        assert resp.json()["code"] == 99999


def test_search_track_deduplication():
    """用例 c: title+artist 与上游重复时去重。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "code": 0,
            "msg": "OK",
            "data": {
                "list": [
                    {
                        "guid": "local:101",
                        "title": "晴天",
                        "artist": "周杰伦",
                        "album": "叶惠美",
                    }
                ],
                "total": 1,
            },
        }
        return httpx.Response(200, json=data)

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        data = {
            "ok": True,
            "data": [
                {
                    "song_id": "228908",
                    "song_name": "晴天",
                    "artist": "周杰伦",
                    "album_name": "叶惠美",
                    "duration": 269,
                    "quality": "LD",
                },
                {
                    "song_id": "228909",
                    "song_name": "晴天 (Live)",
                    "artist": "周杰伦",
                    "album_name": "演唱会",
                    "duration": 300,
                    "quality": "LD",
                },
            ],
        }
        return httpx.Response(200, json=data)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "items": []})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/track?keyword=晴天")
        assert resp.status_code == 200
        items = resp.json()["data"]["list"]
        assert len(items) == 2
        assert items[0]["guid"] == "local:101"
        assert items[1]["guid"] == "online:netease:228909"
        assert items[1]["title"] == "晴天 (Live)"


def test_stream_online_guid_range_and_tee_cache():
    """用例 d: stream online guid Range 转发与落盘 (mock musicdl 返回带 Content-Length 的 200 流，断言 cache 文件生成且内容一致)。"""
    audio_content = b"RIFF....WAVEfmt....FAKE_MP3_STREAM_CONTENT" * 50
    content_len = str(len(audio_content))

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not be called")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "kuwo:228908",
                    "lyric": "[00:00.00]晴天 - 周杰伦\n[00:10.00]故事的小黄花",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "album": "叶惠美",
                },
            )
        assert request.url.path == "/stream"
        assert request.url.params.get("id") == "kuwo:228908"
        assert request.url.params.get("proxy") == "true"
        return httpx.Response(
            200,
            content=audio_content,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Length": content_len,
                "Accept-Ranges": "bytes",
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:kuwo:228908")
        assert resp.status_code == 200
        assert resp.content == audio_content
        assert resp.headers.get("content-length") == content_len

        # 检查落盘到飞牛曲库目录：歌名 - id.ext + 同名 .lrc
        cache_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.mp3")
        assert os.path.exists(cache_file)
        assert "228908" not in os.path.basename(cache_file)
        with open(cache_file, "rb") as f:
            saved = f.read()
        assert saved == audio_content

        lyric_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.lrc")
        assert os.path.exists(lyric_file)
        with open(lyric_file, encoding="utf-8") as f:
            assert "晴天" in f.read()


def test_stream_online_guid_range_0_1_safari_probe_no_cache():
    """Safari Range bytes=0-1 探测不产生任何缓存文件（tmp cache dir 断言为空）。"""
    probe_content = b"\x00\x01"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") == "bytes=0-1"
        return httpx.Response(
            206,
            content=probe_content,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Range": "bytes 0-1/5000",
                "Content-Length": "2",
                "Accept-Ranges": "bytes",
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=online:kuwo:228908",
            headers={"Range": "bytes=0-1"},
        )
        assert resp.status_code == 206
        assert resp.content == probe_content

        # 断言临时曲库/缓存目录没有音频落盘
        for d in (CONF["cache_dir"], CONF["library_dir"]):
            if os.path.exists(d):
                assert not any(
                    f.endswith((".mp3", ".flac", ".lrc")) for f in os.listdir(d)
                )


def test_stream_online_guid_existing_cache_no_part():
    """已存在完整缓存文件时，再次在线播放不再生成 .part。"""
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    cache_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.mp3")
    existing_content = b"EXISTING_CACHED_AUDIO_CONTENT" * 50
    with open(cache_file, "wb") as f:
        f.write(existing_content)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("musicdl should not be called when local cache exists")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:kuwo:228908")
        assert resp.status_code == 200
        assert resp.content == existing_content

        resp2 = client.get(
            "/music/api/v1/track/stream?guid=online:kuwo:228908",
            headers={"Range": "bytes=0-9"},
        )
        assert resp2.status_code == 206
        assert resp2.content == existing_content[:10]

        files = os.listdir(CONF["cache_dir"])
        assert not any(f.endswith(".part") for f in files)
        assert "online_kuwo_228908.mp3" in files
        with open(cache_file, "rb") as f:
            assert f.read() == existing_content


def test_stream_online_guid_nonzero_range_no_cache():
    """Range 从非 0 开始时不落盘，只转发。"""
    partial_content = b"PARTIAL_STREAM_DATA"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") == "bytes=100-119"
        return httpx.Response(
            206,
            content=partial_content,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Range": "bytes 100-119/1000",
                "Content-Length": str(len(partial_content)),
                "Accept-Ranges": "bytes",
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=online:kuwo:228908",
            headers={"Range": "bytes=100-119"},
        )
        assert resp.status_code == 206
        assert resp.content == partial_content
        assert resp.headers.get("content-range") == "bytes 100-119/1000"

        # 断言没有落盘
        cache_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.mp3")
        assert not os.path.exists(cache_file)
        assert not os.path.exists(os.path.join(CONF["library_dir"], "unknown.mp3"))
        assert not os.path.exists(os.path.join(CONF["library_dir"], "unknown - 228908.mp3"))


def test_stream_online_guid_unavailable_404():
    """musicdl 404/502 时返回飞牛格式 404 JSON。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "Source error"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/stream?guid=online:kuwo:notfound")
        assert resp.status_code == 404
        assert resp.json() == {
            "code": 404,
            "msg": "online source unavailable",
            "data": None,
        }


def test_stream_non_online_guid_passthrough():
    """用例 e: 非 online: 前缀的 guid → 透传到 unix socket。"""
    local_audio = b"LOCAL_UNIX_SOCKET_AUDIO_BYTES"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/api/v1/track/stream"
        assert request.url.params.get("guid") == "local:9999"
        assert request.headers.get("range") == "bytes=0-100"
        return httpx.Response(
            206,
            content=local_audio,
            headers={
                "Content-Type": "audio/flac",
                "Content-Range": "bytes 0-100/5000",
                "Accept-Ranges": "bytes",
            },
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/track/stream?guid=local:9999",
            headers={"Range": "bytes=0-100"},
        )
        assert resp.status_code == 206
        assert resp.content == local_audio
        assert resp.headers.get("content-range") == "bytes 0-100/5000"


def test_online_lyrics_and_metadata():
    """在线歌词与元数据合成。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            assert request.url.params.get("id") == "kuwo:228908"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "kuwo:228908",
                    "source": "kuwo",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "album": "叶惠美",
                    "duration_s": 269,
                    "ext": "mp3",
                    "lyric": "[00:00.00]晴天 - 周杰伦\n[00:10.00]故事的小黄花",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # Lyrics (legacy path)
        resp = client.get("/music/api/v1/track/lyrics?guid=online:kuwo:228908")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        assert rj["data"]["guid"] == "online:kuwo:228908"
        assert "[00:00.00]晴天" in rj["data"]["lyric"]

        # 飞牛播放器实际走 GET /lyric/list?trackGUID=
        resp_list = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert resp_list.status_code == 200
        lj = resp_list.json()
        assert lj["code"] == 0
        assert lj["data"]["preferred"] == "online:kuwo:228908:lyric"
        assert len(lj["data"]["list"]) == 1
        item = lj["data"]["list"][0]
        assert item["guid"] == "online:kuwo:228908:lyric"
        assert item["source"] == 2
        assert item["isLRC"] is True
        assert "[00:00.00]晴天" in item["content"]

        # Metadata
        resp2 = client.get("/music/api/v1/track/metadata?guid=online:kuwo:228908")
        assert resp2.status_code == 200
        rj2 = resp2.json()
        assert rj2["code"] == 0
        assert rj2["data"]["title"] == "晴天"
        assert rj2["data"]["artist"] == "周杰伦"
        assert rj2["data"]["duration_ms"] == 269000
        assert rj2["data"]["audioSpec"]["format"] == "mp3"
        assert rj2["data"]["audioSpec"]["codec"] == "mp3"
        assert rj2["data"]["audioSpec"]["channel"] == 2
        _assert_playback_metadata_shape(rj2["data"], guid="online:kuwo:228908")
        assert rj2["data"]["track"]["hasLyric"] is True


def test_online_lyrics_and_metadata_musicdl_error():
    """在线歌词/元数据获取失败时，安全返回 code 0 和空 data，绝不 500。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "failed"})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/lyrics?guid=online:kuwo:err")
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "ok", "data": {}}

        resp_list = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:err")
        assert resp_list.status_code == 200
        assert resp_list.json()["code"] == 0
        assert resp_list.json()["data"]["list"] == []
        assert resp_list.json()["data"]["preferred"] == ""

        resp2 = client.get("/music/api/v1/track/metadata?guid=online:kuwo:err")
        assert resp2.status_code == 200
        # /info 失败也必须给出 _h() 可解构的 stub，否则播放器抛错后直接跳过、永不请求 stream
        _assert_playback_metadata_shape(resp2.json()["data"], guid="online:kuwo:err")


def test_lyric_cache_hit_skips_musicdl():
    """第一次拉歌词落盘后，再次播放只读 cache/*.lrc，不再请求 musicdl。"""
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    lyric_file = os.path.join(CONF["cache_dir"], "online_kuwo_228908.lrc")
    with open(lyric_file, "w", encoding="utf-8") as f:
        f.write("[00:00.00]本地缓存的晴天\n[00:10.00]不再请求源站\n")

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("musicdl should not be called when local lyric cache exists")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert resp.status_code == 200
        item = resp.json()["data"]["list"][0]
        assert "本地缓存的晴天" in item["content"]

        resp2 = client.get("/music/api/v1/track/lyrics?guid=online:kuwo:228908")
        assert "本地缓存的晴天" in resp2.json()["data"]["lyric"]


def test_lyric_list_persists_sidecar():
    """首次 /lyric/list 从 musicdl 取回后写入 .lrc。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    info_calls = {"n": 0}

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            info_calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "kuwo:228908",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "lyric": "[00:00.00]晴天 - 周杰伦\n",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert resp.status_code == 200
        assert info_calls["n"] == 1
        lyric_file = os.path.join(CONF["library_dir"], "周杰伦 - 晴天.lrc")
        assert os.path.exists(lyric_file)
        with open(lyric_file, encoding="utf-8") as f:
            assert "晴天" in f.read()

        resp2 = client.get("/music/api/v1/lyric/list?trackGUID=online:kuwo:228908")
        assert "晴天" in resp2.json()["data"]["list"][0]["content"]
        assert info_calls["n"] == 1


def test_ext_healthz():
    """自身端点 /_ext/healthz 探测 upstream 和 musicdl。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    def musicbox_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )
    app.state.musicbox_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicbox_handler), base_url="http://127.0.0.1:8770"
    )

    with TestClient(app) as client:
        resp = client.get("/_ext/healthz")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["ok"] is True
        assert rj["upstream"] == "ok"
        assert rj["musicdl"] == "ok"
        assert rj["musicbox"] == "ok"


def test_general_passthrough():
    """非拦截路径透传（如静态资源或登录接口）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/api/v1/user/profile"
        assert request.headers.get("authorization") == "Bearer mytoken123"
        return httpx.Response(200, json={"code": 0, "data": {"username": "admin"}})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/user/profile",
            headers={"Authorization": "Bearer mytoken123"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "data": {"username": "admin"}}


def test_search_suggest_merge(monkeypatch):
    """测试 search/suggest 开启与关闭配置。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": ["本地周杰伦"]})

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "items": [
                    {"title": "周杰伦 晴天"},
                    {"title": "周杰伦 七里香"},
                ],
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    # 默认关闭
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=周杰伦")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["本地周杰伦"]

    # 开启 suggest 合并
    monkeypatch.setitem(CONF, "merge_suggest", True)
    with TestClient(app) as client:
        resp = client.get("/music/api/v1/search/suggest?keyword=周杰伦")
        assert resp.status_code == 200
        assert resp.json()["data"] == ["本地周杰伦", "周杰伦 晴天", "周杰伦 七里香"]


def test_online_hls_playlist():
    """在线曲 HLS 兜底 playlist 指向 stream。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "duration_s": 269, "ext": "mp3", "title": "晴天"},
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/track/hls/online:kuwo:228908/preset.m3u8")
        assert resp.status_code == 200
        body = resp.text
        assert "#EXTM3U" in body
        assert "guid=online%3Akuwo%3A228908" in body
        assert "#EXT-X-ENDLIST" in body


def test_online_transcode_ready():
    """在线曲 transcode 直接 success，避免前端卡会话。"""

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/track/transcode",
            json={"guid": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        hb = client.post(
            "/music/api/v1/track/transcode/heartbeat",
            json={"guid": "online:kuwo:228908"},
        )
        assert hb.status_code == 200
        assert hb.json()["code"] == 0


def test_favorite_track_create_online_authorized():
    """用例 1: create online: 上游 mock 已登录(user/me 200 code:0 guid:user-a)，本地返回 code:0，fav 文件落盘。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a", "name": "admin"}})
        return httpx.Response(500, text="Unexpected upstream call")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "kuwo:228908",
                    "source": "kuwo",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "album": "叶惠美",
                    "duration_s": 269,
                    "ext": "mp3",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
            cookies={"music-token": "valid_token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}

        # 验证 fav 文件已落盘在 user-a.json
        user_fav = os.path.join(CONF["fav_dir"], "user-a.json")
        assert os.path.exists(user_fav)
        import json
        with open(user_fav, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert len(saved["items"]) == 1
        item = saved["items"][0]
        assert item["guid"] == "online:kuwo:228908"
        track = item["track"]
        assert track["title"] == "晴天"
        assert track["artists"][0]["name"] == "周杰伦"
        assert track["album"]["name"] == "叶惠美"
        assert track["isFavorite"] is True
        assert track["duration"] == 269000
        assert track["audioSpec"]["format"] == "mp3"


def test_favorite_track_create_online_unauthorized():
    """用例 2: create online 未登录: 上游 mock INVALID TOKEN → 原样透传返回 99999。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 99999, "msg": "INVALID TOKEN", "data": None}
        assert not os.path.exists(os.path.join(CONF["fav_dir"], "user-a.json"))


def test_favorite_track_create_local_passthrough():
    """用例 3: create 本地 guid: 透传上游 mock（验证不写本地）。"""
    upstream_called = {"called": False}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/create":
            upstream_called["called"] = True
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "local:1001"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}
        assert upstream_called["called"] is True
        assert len(os.listdir(CONF["fav_dir"])) == 0


def test_favorite_track_delete_online():
    """用例 4: delete online: 已登录 → code:0 且存储清空；幂等再删仍 code:0。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "晴天"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    user_fav = os.path.join(CONF["fav_dir"], "user-a.json")
    with TestClient(app) as client:
        # 先收藏
        resp1 = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp1.status_code == 200
        import json
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 1

        # 删除
        resp2 = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp2.status_code == 200
        assert resp2.json() == {"code": 0, "msg": "", "data": None}
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 0

        # 再次幂等删除
        resp3 = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp3.status_code == 200
        assert resp3.json() == {"code": 0, "msg": "", "data": None}
        with open(user_fav, "r", encoding="utf-8") as f:
            assert len(json.load(f)["items"]) == 0


def test_favorite_track_list_merge():
    """用例 5: list 合并: 官方 mock 1 条本地 + 本地存 1 条在线 → total=2，list 里有在线条目且形状完整。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            data = {
                "code": 0,
                "msg": "",
                "data": {
                    "list": [
                        {
                            "guid": "local:101",
                            "title": "夜曲",
                            "artists": [{"name": "周杰伦", "guid": "local:artist:1"}],
                            "album": {"name": "十一月的萧邦", "guid": "local:album:1"},
                            "duration": 226000,
                            "isFavorite": True,
                        }
                    ],
                    "total": 1,
                    "sort": "favoriteAt,desc",
                },
            }
            return httpx.Response(200, json=data)
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-a"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "migu:600908",
                    "source": "migu",
                    "title": "稻香",
                    "artist": "周杰伦",
                    "album": "魔杰座",
                    "duration_s": 223,
                    "ext": "flac",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # 存入一条在线收藏
        client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:migu:600908"},
        )

        resp = client.get("/music/api/v1/favorite-track/list?page=1&size=100")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        data = rj["data"]
        assert data["total"] == 2
        items = data["list"]
        assert len(items) == 2
        assert items[0]["guid"] == "local:101"
        assert items[0]["isFavorite"] is True
        
        online_item = items[1]
        assert online_item["guid"] == "online:migu:600908"
        assert online_item["title"] == "稻香"
        assert online_item["duration"] == 223000
        assert online_item["isFavorite"] is True
        assert online_item["isCue"] is False
        assert isinstance(online_item["genres"], list)
        assert isinstance(online_item["artists"], list)
        assert online_item["artists"][0]["name"] == "周杰伦"
        assert isinstance(online_item["album"], dict)
        assert online_item["album"]["name"] == "魔杰座"
        assert isinstance(online_item["audioSpec"], dict)
        assert online_item["audioSpec"]["format"] == "flac"
        assert "createdAt" in online_item
        assert "updatedAt" in online_item


def test_favorite_track_create_unwritable_fav_dir_safe():
    """用例 6: create 时 fav 文件不可写（如路径指向不可写或非法路径）→ 不抛异常，返回仍 code:0（绝不能 500）。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"guid": "user-a"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "晴天"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    # 将 fav_dir 设置为文件路径而非目录，使在其中创建文件失败
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    bad_file = os.path.join(CONF["cache_dir"], "not_a_dir")
    with open(bad_file, "w") as f:
        f.write("xxx")
    CONF["fav_dir"] = os.path.join(bad_file, "sub")

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:228908"},
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == 0


def test_favorite_track_list_upstream_unauthorized():
    """用例 7: list 上游 INVALID TOKEN → 原样返回。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 99999, "msg": "INVALID TOKEN", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get("/music/api/v1/favorite-track/list?page=1&size=100")
        assert resp.status_code == 200
        assert resp.json() == {"code": 99999, "msg": "INVALID TOKEN", "data": None}


def test_favorite_track_delete_local_passthrough():
    """用例 8: delete 本地 guid: 透传上游 mock。"""
    upstream_called = {"called": False}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/delete":
            upstream_called["called"] = True
            return httpx.Response(200, json={"code": 0, "msg": "", "data": None})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "local:1001"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": 0, "msg": "", "data": None}
        assert upstream_called["called"] is True


def test_user_isolation_create_and_list():
    """用户隔离用例 1: 用户 A create → 用户 B list 看不到 A 的在线条目；B 自己 create 后只看到自己的。"""
    current_user = {"guid": "user-a"}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": current_user["guid"]}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            gid = request.url.params.get("id")
            title = "A的歌曲" if "aaa" in str(gid) else "B的歌曲"
            return httpx.Response(200, json={"ok": True, "title": title})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # A 收藏曲目 A
        current_user["guid"] = "user-a"
        resp_a_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:aaa"},
        )
        assert resp_a_create.status_code == 200

        # B 查看列表，看不到 A 的收藏
        current_user["guid"] = "user-b"
        resp_b_list1 = client.get("/music/api/v1/favorite-track/list")
        assert resp_b_list1.status_code == 200
        assert resp_b_list1.json()["data"]["total"] == 0
        assert len(resp_b_list1.json()["data"]["list"]) == 0

        # B 收藏曲目 B
        resp_b_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:bbb"},
        )
        assert resp_b_create.status_code == 200

        # B 再次查看列表，只有 B 的歌曲
        resp_b_list2 = client.get("/music/api/v1/favorite-track/list")
        assert resp_b_list2.status_code == 200
        assert resp_b_list2.json()["data"]["total"] == 1
        assert resp_b_list2.json()["data"]["list"][0]["guid"] == "online:kuwo:bbb"

        # A 查看列表，只有 A 的歌曲
        current_user["guid"] = "user-a"
        resp_a_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_a_list.status_code == 200
        assert resp_a_list.json()["data"]["total"] == 1
        assert resp_a_list.json()["data"]["list"][0]["guid"] == "online:kuwo:aaa"


def test_user_isolation_delete():
    """用户隔离用例 2: 用户 A create、用户 B delete 同一 guid → A 的仍在（B 幂等 code:0），互不影响。"""
    current_user = {"guid": "user-a"}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": current_user["guid"]}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "公共在线曲目"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # A 收藏曲目
        current_user["guid"] = "user-a"
        client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:same_song"},
        )

        # B 尝试删除同一曲目（B 本身未收藏）
        current_user["guid"] = "user-b"
        resp_b_del = client.post(
            "/music/api/v1/favorite-track/delete",
            json={"trackGUID": "online:kuwo:same_song"},
        )
        assert resp_b_del.status_code == 200
        assert resp_b_del.json()["code"] == 0

        # A 检查列表，收藏依然在
        current_user["guid"] = "user-a"
        resp_a_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_a_list.status_code == 200
        assert resp_a_list.json()["data"]["total"] == 1
        assert resp_a_list.json()["data"]["list"][0]["guid"] == "online:kuwo:same_song"


def test_user_me_missing_guid_fallback_shared():
    """用户隔离用例 3: user/me 响应缺 guid → 落 'shared' 桶不报错。"""
    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/user/me":
            # 返回没有 guid 字段或者 data 非 dict
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"name": "someone"}})
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"ok": True, "title": "兜底歌曲"})
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # 创建收藏
        resp_create = client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:kuwo:fallback_track"},
        )
        assert resp_create.status_code == 200
        assert resp_create.json()["code"] == 0

        # 检查是否落入 shared.json
        shared_file = os.path.join(CONF["fav_dir"], "shared.json")
        assert os.path.exists(shared_file)
        import json
        with open(shared_file, "r", encoding="utf-8") as f:
            items = json.load(f)["items"]
        assert len(items) == 1
        assert items[0]["guid"] == "online:kuwo:fallback_track"

        # 查询 list
        resp_list = client.get("/music/api/v1/favorite-track/list")
        assert resp_list.status_code == 200
        assert resp_list.json()["data"]["total"] == 1
        assert resp_list.json()["data"]["list"][0]["guid"] == "online:kuwo:fallback_track"


def test_user_guid_sanitization():
    """测试 user_guid 特殊字符文件名过滤安全逻辑。"""
    from proxy.app import sanitize_user_guid
    assert sanitize_user_guid("user-123_ABC") == "user-123_ABC"
    assert sanitize_user_guid("../../etc/passwd") == "______etc_passwd"
    assert sanitize_user_guid("user:name*?<>|") == "user_name_____"
    assert sanitize_user_guid("   ") == "shared"
    assert sanitize_user_guid("") == "shared"
    assert sanitize_user_guid(None) == "shared"


def test_static_cover_online_coverid_redirect():
    """测试 1: mock musicdl /info 返回 cover_url，GET /static/cover?coverId=online:migu:123&size=120 → 302 且 Location == cover_url。"""
    cover_target = "http://img.music.migu.cn/cover123.jpg"

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Should not reach upstream")

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/info"
        assert request.url.params.get("id") == "migu:123"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "id": "migu:123",
                "title": "测试歌曲",
                "artist": "歌手",
                "cover_url": cover_target,
            },
        )

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/static/cover?coverId=online:migu:123&size=120",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers.get("location") == cover_target


def test_static_cover_local_coverid_passthrough():
    """测试 2: coverId 为非 online: 本地 guid → 透传上游（mock 上游 200 二进制），响应原样。"""
    fake_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR..."

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/music/api/v1/static/cover"
        assert request.url.params.get("coverId") == "local:track:999"
        assert request.url.params.get("size") == "120"
        return httpx.Response(
            200,
            content=fake_image_bytes,
            headers={"content-type": "image/png"},
        )

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Should not reach musicdl")

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        resp = client.get(
            "/music/api/v1/static/cover?coverId=local:track:999&size=120",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.content == fake_image_bytes
        assert resp.headers.get("content-type") == "image/png"


def test_favorite_track_list_official_items_populate_is_favorite_and_empty_handling():
    """测试 official_list 缺失 isFavorite 时补齐 True，且在官方列表为空或有条目时正确合并和计算 total。"""
    official_data_holder = {"list": []}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/music/api/v1/favorite-track/list":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "",
                    "data": {
                        "list": official_data_holder["list"],
                        "total": len(official_data_holder["list"]),
                    },
                },
            )
        if request.url.path == "/music/api/v1/user/me":
            return httpx.Response(200, json={"code": 0, "data": {"guid": "user-fav-test"}})
        return httpx.Response(500)

    def musicdl_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "netease:12345",
                    "source": "netease",
                    "title": "测试网易曲目",
                    "artist": "歌手A",
                    "album": "专辑A",
                    "duration_s": 180,
                    "ext": "mp3",
                },
            )
        return httpx.Response(404)

    app.state.upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler), base_url="http://unix"
    )
    app.state.musicdl_client = httpx.AsyncClient(
        transport=httpx.MockTransport(musicdl_handler), base_url="http://127.0.0.1:8768"
    )

    with TestClient(app) as client:
        # Case 1: 官方列表为空，无在线收藏 -> total=0, list=[]
        resp = client.get("/music/api/v1/favorite-track/list")
        assert resp.status_code == 200
        rj = resp.json()
        assert rj["code"] == 0
        assert rj["data"]["total"] == 0
        assert rj["data"]["list"] == []

        # 添加一条在线收藏
        client.post(
            "/music/api/v1/favorite-track/create",
            json={"trackGUID": "online:netease:12345"},
        )

        # Case 2: 官方列表为空，存在 1 条在线收藏 -> total=1
        resp2 = client.get("/music/api/v1/favorite-track/list")
        assert resp2.status_code == 200
        rj2 = resp2.json()
        assert rj2["data"]["total"] == 1
        assert len(rj2["data"]["list"]) == 1
        assert rj2["data"]["list"][0]["guid"] == "online:netease:12345"
        assert rj2["data"]["list"][0]["isFavorite"] is True

        # Case 3: 官方列表中包含未带 isFavorite 字段（或 isFavorite 为 False）的条目
        official_data_holder["list"] = [
            {"guid": "local:201", "title": "本地曲目1"},
            {"guid": "local:202", "title": "本地曲目2", "isFavorite": False},
        ]
        resp3 = client.get("/music/api/v1/favorite-track/list")
        assert resp3.status_code == 200
        rj3 = resp3.json()
        # 官方 2 条 + 在线 1 条 = 3 条
        assert rj3["data"]["total"] == 3
        items = rj3["data"]["list"]
        assert len(items) == 3
        assert items[0]["guid"] == "local:201"
        assert items[0]["isFavorite"] is True
        assert items[1]["guid"] == "local:202"
        assert items[1]["isFavorite"] is True
        assert items[2]["guid"] == "online:netease:12345"
        assert items[2]["isFavorite"] is True



