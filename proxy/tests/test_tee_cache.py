"""边听边存开关 / 保存路径回退 / 滚动缓存与 restore 清理行为测试。"""
import asyncio
import importlib
import logging
import os

import httpx
import pytest

p = importlib.import_module("proxy.app")
gc = importlib.import_module("proxy.cache_gc")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("cache_dir", "library_dir", "fav_dir"):
        monkeypatch.setitem(p.CONF, key, str(tmp_path / key))
    monkeypatch.setitem(p.CONF, "music_db", str(tmp_path / "missing.db"))
    monkeypatch.setitem(p.CONF, "tee_save_enabled", True)
    monkeypatch.setitem(p.CONF, "tee_save_dir", "")
    monkeypatch.setitem(p.CONF, "tee_cache_max", 2)
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", False)
    monkeypatch.setattr(p, "_TEE_SAVE_DIR_WARNED", False)
    p._full_fetch_tasks.clear()
    p._full_fetch_failed.clear()
    p._scan_last_attempt = 0.0
    p._scan_task = None
    monkeypatch.setitem(p.CONF, "library_scan_path", "")
    os.makedirs(p.CONF["library_dir"], exist_ok=True)
    yield
    p._full_fetch_tasks.clear()
    p._full_fetch_failed.clear()


class Audio(httpx.AsyncByteStream):
    def __init__(self, payload=b"A" * 4096):
        self.payload = payload
        self.closed = False

    async def __aiter__(self):
        yield self.payload

    async def aclose(self):
        self.closed = True


async def listen(guid, title="晴天", artist="周杰伦", ext="flac", range_header=None,
                 info=None, post_finalize=None, response=None):
    """完整试听一首歌并消费完整个响应体，触发 stream_tee_response 落盘转正。"""
    if info is None:
        info = {"title": title, "artist": artist, "album": "叶惠美", "lyric": "[00:00.00]测试歌词"}
    if response is None:
        response = httpx.Response(200, stream=Audio())
    tee_response = p.stream_tee_response(
        response, guid, range_header,
        pre_info=info, resolved_ext=ext, post_finalize=post_finalize,
    )
    body = b"".join([chunk async for chunk in tee_response.body_iterator])
    assert body == b"A" * 4096


def audio_files(directory):
    if not os.path.isdir(directory):
        return []
    return sorted(
        f for f in os.listdir(directory)
        if os.path.splitext(f)[1].lstrip(".").lower() in gc.AUDIO_EXTS
    )


@pytest.mark.anyio
async def test_tee_off_only_caches_into_cache_dir(monkeypatch):
    """开关关：文件以 online_源_id 命名落 cache 目录，不进曲库，回放可命中；
    滚动缓存不属于"自动下载音乐"，无论歌词开关如何都不写歌词。"""
    monkeypatch.setitem(p.CONF, "tee_save_enabled", False)
    await listen("online:netease:186016")
    cache_dir, library_dir = p.CONF["cache_dir"], p.CONF["library_dir"]
    cached = os.path.join(cache_dir, "online_netease_186016.flac")
    assert os.path.isfile(cached)
    with open(cached, "rb") as f:
        assert f.read() == b"A" * 4096
    assert not os.path.exists(os.path.join(cache_dir, "online_netease_186016.lrc"))
    assert audio_files(library_dir) == []
    assert p.find_cache_file("online:netease:186016") == cached


@pytest.mark.anyio
async def test_tee_off_keeps_latest_n_rolling(monkeypatch):
    """开关关：滚动保留最新 tee_cache_max 首，最旧的音频/ref 一并淘汰。"""
    monkeypatch.setitem(p.CONF, "tee_save_enabled", False)
    for i, guid in enumerate(["online:kuwo:1", "online:kuwo:2", "online:kuwo:3"]):
        await listen(guid, title=f"歌{i}")
        safe = p.cache_safe_guid(guid)
        os.utime(os.path.join(p.CONF["cache_dir"], f"{safe}.flac"), (1000 + i, 1000 + i))
    cache_dir = p.CONF["cache_dir"]
    assert sorted(f for f in os.listdir(cache_dir) if f.endswith(".flac")) == [
        "online_kuwo_2.flac", "online_kuwo_3.flac",
    ]
    assert not os.path.exists(os.path.join(cache_dir, "online_kuwo_1.lrc"))
    assert not os.path.exists(os.path.join(cache_dir, "online_kuwo_1.ref"))
    assert audio_files(p.CONF["library_dir"]) == []


@pytest.mark.anyio
async def test_tee_on_ignores_cache_max(monkeypatch):
    """开关开：tee_cache_max 不生效，全部永久保存且不滚动清理。"""
    monkeypatch.setitem(p.CONF, "tee_cache_max", 2)
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", True)
    await listen("online:kuwo:1", title="晴天")
    await listen("online:kuwo:2", title="七里香")
    await listen("online:kuwo:3", title="稻香")
    assert audio_files(p.CONF["library_dir"]) == [
        "周杰伦 - 七里香.flac", "周杰伦 - 晴天.flac", "周杰伦 - 稻香.flac",
    ]
    assert [f for f in os.listdir(p.CONF["cache_dir"]) if f.endswith((".flac", ".mp3"))] == []
    # 自动下载歌词开启：每首歌旁都有同名 .lrc
    library_dir = p.CONF["library_dir"]
    assert sorted(f for f in os.listdir(library_dir) if f.endswith(".lrc")) == [
        "周杰伦 - 七里香.lrc", "周杰伦 - 晴天.lrc", "周杰伦 - 稻香.lrc",
    ]


@pytest.mark.anyio
async def test_tee_save_dir_custom_path_used(monkeypatch, tmp_path):
    """配置可用保存路径：文件落配置目录并写标签/歌词，ref 指向该文件。"""
    custom = str(tmp_path / "my-music")
    monkeypatch.setitem(p.CONF, "tee_save_dir", custom)
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", True)
    await listen("online:netease:186016")
    dest = os.path.join(custom, "周杰伦 - 晴天.flac")
    assert os.path.isfile(dest)
    assert os.path.isfile(os.path.join(custom, "周杰伦 - 晴天.lrc"))
    assert audio_files(p.CONF["library_dir"]) == []
    assert p.find_cache_file("online:netease:186016") == dest


@pytest.mark.anyio
async def test_tee_save_dir_unusable_falls_back(monkeypatch, tmp_path, caplog):
    """配置路径不可用（被同名文件占位）：回退默认曲库目录并告警。"""
    blocked = str(tmp_path / "blocked")
    with open(blocked, "w") as f:
        f.write("not a directory")
    monkeypatch.setitem(p.CONF, "tee_save_dir", blocked)
    with caplog.at_level(logging.WARNING, logger="fnmusic_proxy"):
        await listen("online:netease:186016")
    assert os.path.isfile(os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.flac"))
    assert any("FNMUSIC_TEE_SAVE_DIR" in r.getMessage() and "不可用" in r.getMessage()
               for r in caplog.records)


@pytest.mark.anyio
async def test_rolling_ref_does_not_trap_library_promotion(monkeypatch):
    """开关关→开的切换：滚动缓存留下的 ref 不得把后续保存困在 cache 目录。"""
    monkeypatch.setitem(p.CONF, "tee_save_enabled", False)
    await listen("online:kuwo:1")
    assert os.path.isfile(os.path.join(p.CONF["cache_dir"], "online_kuwo_1.flac"))
    monkeypatch.setitem(p.CONF, "tee_save_enabled", True)
    await listen("online:kuwo:1")
    library_dir = p.CONF["library_dir"]
    assert os.path.isfile(os.path.join(library_dir, "周杰伦 - 晴天.flac"))
    with open(p.media_ref_path("online:kuwo:1"), encoding="utf-8") as f:
        assert os.path.dirname(f.read().strip()) == library_dir


# === 自动下载歌词（FNMUSIC_LYRIC_AUTO_DL，默认关） ===


@pytest.mark.anyio
async def test_lyric_auto_dl_off_no_lrc(monkeypatch):
    """开关关（默认）：即使源返回了歌词也不落 .lrc，下载即完成、无歌词文件。"""
    assert p.CONF["lyric_auto_dl"] is False
    await listen("online:netease:186016")
    assert os.path.isfile(os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.flac"))
    assert not os.path.exists(os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.lrc"))
    assert p.find_lyric_file("online:netease:186016") is None


@pytest.mark.anyio
async def test_lyric_auto_dl_on_writes_sidecar(monkeypatch):
    """开关开：音乐完整落库成功后，info 里的歌词写到音频旁同名 .lrc。"""
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", True)
    await listen("online:netease:186016")
    library_dir = p.CONF["library_dir"]
    assert os.path.isfile(os.path.join(library_dir, "周杰伦 - 晴天.flac"))
    sidecar = os.path.join(library_dir, "周杰伦 - 晴天.lrc")
    assert os.path.isfile(sidecar)
    with open(sidecar, encoding="utf-8") as f:
        assert "测试歌词" in f.read()


class Broken(httpx.AsyncByteStream):
    """上游中途断流的音频流。"""

    async def __aiter__(self):
        yield b"A" * 2048
        raise RuntimeError("upstream reset")

    async def aclose(self):
        pass


@pytest.mark.anyio
async def test_lyric_auto_dl_on_download_failure_no_pollution(monkeypatch):
    """开关开但音频下载失败：音频与歌词都不落盘，目录无污染。"""
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", True)
    guid = "online:netease:186016"
    response = p.stream_tee_response(
        httpx.Response(200, stream=Broken()), guid, None,
        pre_info={"title": "晴天", "artist": "周杰伦", "album": "叶惠美", "lyric": "[00:00.00]测试歌词"},
        resolved_ext="flac",
    )
    with pytest.raises(RuntimeError):
        _ = b"".join([chunk async for chunk in response.body_iterator])
    library_dir = p.CONF["library_dir"]
    assert audio_files(library_dir) == []
    assert [f for f in os.listdir(library_dir) if f.endswith(".lrc")] == []
    if os.path.isdir(p.CONF["cache_dir"]):
        assert [f for f in os.listdir(p.CONF["cache_dir"])
                if f.endswith((".flac", ".lrc", ".part"))] == []


@pytest.mark.anyio
async def test_auto_lyric_after_finalize_fetches_when_missing(monkeypatch):
    """开关开 + info 未带歌词：finalize 后补拉源站歌词并落音频旁。"""
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", True)
    calls = []

    async def fake_resolve(request, guid):
        calls.append(guid)
        p.write_lyric_cache(guid, "[00:00.00]补拉歌词", title="晴天", artist="周杰伦")
        return "[00:00.00]补拉歌词"

    monkeypatch.setattr(p, "resolve_online_lyric", fake_resolve)
    guid = "online:netease:186016"

    async def post(meta):
        await p._auto_lyric_after_finalize(_fake_request(), guid, meta)

    await listen(guid, info={"title": "晴天", "artist": "周杰伦", "album": "叶惠美"},
                 post_finalize=post)
    assert calls == [guid]
    sidecar = os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.lrc")
    assert os.path.isfile(sidecar)
    with open(sidecar, encoding="utf-8") as f:
        assert "补拉歌词" in f.read()


@pytest.mark.anyio
async def test_auto_lyric_after_finalize_guards(monkeypatch):
    """开关关 / 滚动缓存产物 / 已有歌词：三种情况都不发补拉请求。"""
    calls = []

    async def fake_resolve(request, guid):
        calls.append(guid)
        return ""

    monkeypatch.setattr(p, "resolve_online_lyric", fake_resolve)
    req = _fake_request()

    # 开关关（默认）：直接返回
    await p._auto_lyric_after_finalize(req, "online:netease:186016", {"dest": "/x/周杰伦 - 晴天.flac"})
    assert calls == []

    # 边听边存关：落盘产物是滚动缓存命名，不补歌词
    monkeypatch.setitem(p.CONF, "lyric_auto_dl", True)
    rolling_dest = os.path.join(
        p.CONF["cache_dir"], f"{p.cache_safe_guid('online:netease:186016')}.flac")
    await p._auto_lyric_after_finalize(req, "online:netease:186016", {"dest": rolling_dest})
    assert calls == []

    # 已有歌词（音频旁 sidecar 已就位）：零请求
    audio = os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(audio, "wb") as f:
        f.write(b"audio")
    p.remember_media_path("online:netease:186016", audio)
    with open(os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.lrc"), "w", encoding="utf-8") as f:
        f.write("已有歌词")
    await p._auto_lyric_after_finalize(req, "online:netease:186016", {"dest": audio})
    assert calls == []


def test_cache_gc_keeps_latest_and_library_refs(tmp_path):
    """purge_rolling：淘汰最旧滚动音频+歌词；指向曲库存活文件的 ref 保留，陈旧 ref 清理。"""
    cache_dir = tmp_path / "cache"
    library_dir = tmp_path / "library"
    cache_dir.mkdir()
    library_dir.mkdir()
    library_audio = library_dir / "周杰伦 - 晴天.flac"
    library_audio.write_bytes(b"lib")

    def touch(path, mtime, content=b""):
        path.write_bytes(content)
        os.utime(path, (mtime, mtime))

    touch(cache_dir / "online_kuwo_1.mp3", 100)
    touch(cache_dir / "online_kuwo_1.lrc", 100, b"[00:00.00]lrc")
    touch(cache_dir / "online_kuwo_2.mp3", 200)
    touch(cache_dir / "online_kuwo_2.lrc", 200, b"[00:00.00]lrc")
    touch(cache_dir / "online_kuwo_3.mp3", 300)
    (cache_dir / "online_kuwo_1.ref").write_text(str(cache_dir / "online_kuwo_1"), encoding="utf-8")
    (cache_dir / "online_kuwo_2.ref").write_text(str(cache_dir / "online_kuwo_2"), encoding="utf-8")
    # 指向曲库存活文件：必须保留；指向已消失目标：清理；无关文件：不动。
    (cache_dir / "online_kuwo_9.ref").write_text(str(library_audio), encoding="utf-8")
    (cache_dir / "online_kuwo_8.ref").write_text(str(library_dir / "已删除"), encoding="utf-8")
    (cache_dir / "notes.txt").write_text("keep me", encoding="utf-8")

    removed = gc.purge_rolling(str(cache_dir), keep=2)

    assert sorted(f for f in os.listdir(cache_dir) if f.endswith(".mp3")) == [
        "online_kuwo_2.mp3", "online_kuwo_3.mp3",
    ]
    assert not (cache_dir / "online_kuwo_1.lrc").exists()
    assert not (cache_dir / "online_kuwo_1.ref").exists()
    assert (cache_dir / "online_kuwo_2.ref").exists()
    assert (cache_dir / "online_kuwo_9.ref").exists()
    assert not (cache_dir / "online_kuwo_8.ref").exists()
    assert (cache_dir / "notes.txt").exists()
    assert str(cache_dir / "online_kuwo_1.mp3") in removed


def test_cache_gc_cli_reads_env_cache_dir(tmp_path, capsys):
    """restore.sh 依赖的 CLI：--base 从 .env 解析 FNMUSIC_CACHE_DIR，keep=0 全清。"""
    base = tmp_path / "proj"
    cache_dir = tmp_path / "custom-cache"
    base.mkdir()
    cache_dir.mkdir()
    (base / ".env").write_text(
        "FNMUSIC_HOME='" + str(base) + "'\nFNMUSIC_CACHE_DIR='" + str(cache_dir) + "'\n",
        encoding="utf-8",
    )
    (cache_dir / "online_migu_600929.mp3").write_bytes(b"x" * 10)
    (cache_dir / "online_migu_600929.lrc").write_text("lrc", encoding="utf-8")
    (cache_dir / "other.json").write_text("{}", encoding="utf-8")

    assert gc.main(["--base", str(base)]) == 0
    out = capsys.readouterr().out
    assert str(cache_dir) in out and "removed=2" in out
    assert os.listdir(cache_dir) == ["other.json"]


def test_cache_gc_missing_dir_is_noop(tmp_path):
    assert gc.purge_rolling(str(tmp_path / "nope"), keep=0) == []


class Whole(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = payload

    async def __aiter__(self):
        yield self.payload

    async def aclose(self):
        pass


class Done(httpx.AsyncByteStream):
    """已耗尽的迭代器：载荷已作为首块交付（对齐真实 _open_online_stream 语义）。"""

    async def __aiter__(self):
        if False:
            yield b""

    async def aclose(self):
        pass


def _fake_request():
    return p.Request({"type": "http", "headers": [(b"cookie", b"sid=test")], "app": p.app})


@pytest.mark.anyio
async def test_full_fetch_saves_library_for_windowed_client(monkeypatch):
    """定长窗口客户端：本响应不落盘，后台整轨下载把完整文件写进曲库。"""
    guid = "online:netease:186016"
    full = b"F" * 8192
    info = {"title": "晴天", "artist": "周杰伦", "album": "叶惠美"}
    opens = []

    async def fake_open(request, g, range_header, force_mp3=False, fresh_url=False):
        opens.append(range_header)
        if range_header is None:
            resp = httpx.Response(200, stream=Whole(full), headers={"content-length": str(len(full))})
            return (resp, None, "flac", info, Done(), full)
        resp = httpx.Response(
            206, stream=Whole(full[:1024]),
            headers={"content-length": "1024", "content-range": f"bytes 0-1023/{len(full)}"},
        )
        return (resp, None, "flac", info, Done(), full[:1024])

    monkeypatch.setattr(p, "_open_online_stream", fake_open)
    p._register_full_fetch(_fake_request(), guid)
    await asyncio.wait_for(p._full_fetch_tasks[guid], timeout=5.0)
    # 整轨下载走的是无 Range 请求
    assert opens == [None]
    dest = os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.flac")
    assert os.path.isfile(dest)
    with open(dest, "rb") as f:
        assert f.read() == full
    assert guid not in p._full_fetch_tasks


@pytest.mark.anyio
async def test_full_fetch_dedup_and_failure_cooldown(monkeypatch):
    """同 guid 去重；失败进入冷却期，冷却期内不再注册新任务。"""
    guid = "online:migu:7"
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_open(request, g, range_header, force_mp3=False, fresh_url=False):
        calls.append(g)
        started.set()
        await release.wait()
        return None  # 视为打开失败

    monkeypatch.setattr(p, "_open_online_stream", fake_open)
    p._register_full_fetch(_fake_request(), guid)
    p._register_full_fetch(_fake_request(), guid)  # 在途任务：去重
    await asyncio.wait_for(started.wait(), timeout=5.0)
    release.set()
    await asyncio.wait_for(asyncio.gather(*p._full_fetch_tasks.values()), timeout=5.0)
    assert calls == [guid]
    # 失败后冷却：不注册新任务
    p._register_full_fetch(_fake_request(), guid)
    assert guid not in p._full_fetch_tasks


@pytest.mark.anyio
async def test_full_fetch_skips_when_cached_or_tee_off(monkeypatch):
    """缓存已命中或边听边存关闭时不注册下载任务。"""
    called = []

    async def fake_open(request, g, range_header, force_mp3=False, fresh_url=False):
        called.append(g)
        return None

    monkeypatch.setattr(p, "_open_online_stream", fake_open)
    monkeypatch.setitem(p.CONF, "tee_save_enabled", False)
    p._register_full_fetch(_fake_request(), "online:kuwo:1")
    assert p._full_fetch_tasks == {}
    # 缓存命中：同样不注册
    monkeypatch.setitem(p.CONF, "tee_save_enabled", True)
    os.makedirs(p.CONF["cache_dir"], exist_ok=True)
    cached = os.path.join(p.CONF["cache_dir"], "online_kuwo_2.flac")
    with open(cached, "wb") as f:
        f.write(b"x" * 2048)
    p._register_full_fetch(_fake_request(), "online:kuwo:2")
    assert p._full_fetch_tasks == {}
    assert called == []


@pytest.mark.anyio
async def test_library_scan_disabled_by_default():
    """路径未配置（默认禁用）：落盘后完全不发起扫描。"""
    p._schedule_library_scan({"cookie": "sid=x"})
    assert p._scan_task is None


@pytest.mark.anyio
async def test_library_scan_debounce_and_ttl(monkeypatch):
    """合并窗口内多次落盘只发一次；90 秒 TTL 内（含失败）不再重发。"""
    monkeypatch.setitem(p.CONF, "library_scan_path", "/music/api/v1/shared-library/scan")
    posts = []

    class ScanTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            posts.append((request.method, request.url.path))
            return httpx.Response(200, json={"code": 0})

    client = httpx.AsyncClient(transport=ScanTransport(), base_url="http://unix")
    monkeypatch.setattr(p, "get_upstream_client", lambda a: client)
    try:
        p._schedule_library_scan({"cookie": "sid=x"})
        p._schedule_library_scan({"cookie": "sid=y"})  # 合并窗口内：并入同一任务
        await asyncio.wait_for(p._scan_task, timeout=5.0)
        assert posts == [("POST", "/music/api/v1/shared-library/scan")]
        # TTL 内：不重发
        p._schedule_library_scan({"cookie": "sid=z"})
        await asyncio.sleep(0.1)
        assert posts == [("POST", "/music/api/v1/shared-library/scan")]
    finally:
        await client.aclose()
