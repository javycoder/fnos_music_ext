"""歌词三档归属 / 影子提升 / 孤儿清扫安全边界 / 取流探针测试。"""
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
    monkeypatch.setattr(p, "_TEE_SAVE_DIR_WARNED", False)
    os.makedirs(p.CONF["library_dir"], exist_ok=True)
    os.makedirs(p.CONF["cache_dir"], exist_ok=True)
    yield


class Audio(httpx.AsyncByteStream):
    def __init__(self, payload=b"A" * 4096):
        self.payload = payload

    async def __aiter__(self):
        yield self.payload

    async def aclose(self):
        pass


async def listen(guid, title="晴天", artist="周杰伦", ext="flac", range_header=None):
    """完整试听一首歌并消费完整个响应体，触发 stream_tee_response 落盘转正。"""
    info = {"title": title, "artist": artist, "album": "叶惠美", "lyric": "[00:00.00]新词"}
    response = p.stream_tee_response(
        httpx.Response(200, stream=Audio()), guid, range_header,
        pre_info=info, resolved_ext=ext,
    )
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == b"A" * 4096


# === 三档归属 ===

def test_no_audio_lyric_stays_in_cache():
    """无音频时歌词只落 cache（guid 命名），绝不写曲库目录（孤儿歌词根因）。"""
    p.write_lyric_cache("online:netease:186016", "[00:00.00]旧词", title="晴天", artist="周杰伦")
    cache_lrc = os.path.join(p.CONF["cache_dir"], "online_netease_186016.lrc")
    assert os.path.isfile(cache_lrc)
    assert [f for f in os.listdir(p.CONF["library_dir"]) if f.endswith(".lrc")] == []
    assert p.read_lyric_cache("online:netease:186016") == "[00:00.00]旧词"


def test_lyric_follows_library_audio():
    """音频已在曲库：歌词落音频旁同名 sidecar。"""
    audio = os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(audio, "wb") as f:
        f.write(b"audio")
    p.remember_media_path("online:netease:186016", audio)
    p.write_lyric_cache("online:netease:186016", "[00:00.00]词", title="晴天", artist="周杰伦")
    assert os.path.isfile(os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.lrc"))
    assert [f for f in os.listdir(p.CONF["cache_dir"]) if f.endswith(".lrc")] == []


def test_lyric_follows_cache_audio():
    """音频只在 cache（滚动缓存形态）：歌词落 cache 同词干（无需 .ref 也能命中）。"""
    with open(os.path.join(p.CONF["cache_dir"], "online_netease_186016.flac"), "wb") as f:
        f.write(b"audio")
    p.write_lyric_cache("online:netease:186016", "[00:00.00]词", title="晴天", artist="周杰伦")
    assert os.path.isfile(os.path.join(p.CONF["cache_dir"], "online_netease_186016.lrc"))
    assert [f for f in os.listdir(p.CONF["library_dir"]) if f.endswith(".lrc")] == []


def test_find_lyric_file_prefers_recorded_library_sidecar():
    """词曲成对后，ref 记录的曲库 sidecar 优先于 cache 内的 guid 命名副本。"""
    audio = os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(audio, "wb") as f:
        f.write(b"audio")
    p.remember_media_path("online:netease:186016", audio)
    with open(os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.lrc"), "w", encoding="utf-8") as f:
        f.write("curated")
    with open(os.path.join(p.CONF["cache_dir"], "online_netease_186016.lrc"), "w", encoding="utf-8") as f:
        f.write("shadow")
    assert p.find_lyric_file("online:netease:186016").endswith("周杰伦 - 晴天.lrc")


# === 影子提升 ===

@pytest.mark.anyio
async def test_tee_promotes_shadow_lyric():
    """先听出 cache 影子歌词，整轨落曲库后词曲贴身：曲库 sidecar 就位、影子清除。"""
    guid = "online:netease:186016"
    p.write_lyric_cache(guid, "[00:00.00]旧词", title="晴天", artist="周杰伦")  # 无音频 → 影子
    shadow = os.path.join(p.CONF["cache_dir"], "online_netease_186016.lrc")
    assert os.path.isfile(shadow)

    await listen(guid)

    library_dir = p.CONF["library_dir"]
    assert os.path.isfile(os.path.join(library_dir, "周杰伦 - 晴天.flac"))
    sidecar = os.path.join(library_dir, "周杰伦 - 晴天.lrc")
    assert os.path.isfile(sidecar)
    assert not os.path.exists(shadow)
    with open(sidecar, encoding="utf-8") as f:
        assert "新词" in f.read()  # 新词覆盖旧词，且落在贴身位置
    assert p.find_lyric_file(guid) == sidecar


@pytest.mark.anyio
async def test_tee_promote_keeps_existing_sidecar():
    """曲库已有 sidecar 时提升不覆盖它；sidecar 缺失时影子才被提升过去。"""
    guid = "online:netease:186016"
    p.write_lyric_cache(guid, "[00:00.00]影子词", title="晴天", artist="周杰伦")
    shadow = os.path.join(p.CONF["cache_dir"], "online_netease_186016.lrc")
    audio = os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.flac")
    with open(audio, "wb") as f:
        f.write(b"audio")
    sidecar = os.path.join(p.CONF["library_dir"], "周杰伦 - 晴天.lrc")

    # 曲库已有（例如用户整理过的）sidecar：不得被影子覆盖
    with open(sidecar, "w", encoding="utf-8") as f:
        f.write("curated")
    p.promote_shadow_lyric(guid, audio)
    with open(sidecar, encoding="utf-8") as f:
        assert f.read() == "curated"

    # sidecar 缺失时：影子提升为 sidecar，cache 副本清除
    os.remove(sidecar)
    p.promote_shadow_lyric(guid, audio)
    with open(sidecar, encoding="utf-8") as f:
        assert "影子词" in f.read()
    assert not os.path.exists(shadow)


# === 孤儿清扫安全边界 ===

def _reflay(cache_dir, name, stem):
    (cache_dir / f"{name}.ref").write_text(str(stem), encoding="utf-8")


def test_sweep_removes_only_recorded_stale_orphans(tmp_path):
    """只删"ref 记录 + 曲库目录内 + 无同名词干音频 + 超过保护期"四条全命中的 .lrc。"""
    cache_dir = tmp_path / "cache"
    library_dir = tmp_path / "library"
    cache_dir.mkdir()
    library_dir.mkdir()

    def touch(path, mtime):
        path.write_text("lrc", encoding="utf-8")
        os.utime(path, (mtime, mtime))

    # 1) 标准孤儿：ref 记录、无音频、mtime 很旧 → 删（连同死 ref）
    orphan = library_dir / "歌手A - 歌1.lrc"
    touch(orphan, 1000)
    _reflay(cache_dir, "a", library_dir / "歌手A - 歌1")
    # 2) 用户自有：无 ref 记录 → 不动
    (library_dir / "用户自己的.lrc").write_text("mine", encoding="utf-8")
    # 3) 词曲成对：ref + 音频 + 歌词 → 不动
    (library_dir / "歌手B - 歌2.flac").write_bytes(b"audio")
    touch(library_dir / "歌手B - 歌2.lrc", 1000)
    _reflay(cache_dir, "b", library_dir / "歌手B - 歌2")
    # 4) 太新：ref 记录、无音频、mtime=now → 不动
    fresh = library_dir / "歌手C - 歌3.lrc"
    fresh.write_text("lrc", encoding="utf-8")
    _reflay(cache_dir, "c", library_dir / "歌手C - 歌3")
    # 5) cache 内部：ref 指向 cache 词干 → 不属 media_dirs，归 purge_rolling 管
    (cache_dir / "online_kuwo_9.lrc").write_text("lrc", encoding="utf-8")
    _reflay(cache_dir, "e", cache_dir / "online_kuwo_9")

    removed = gc.sweep_orphan_lyrics(str(cache_dir), [str(library_dir)])

    assert sorted(removed) == sorted([str(orphan), str(cache_dir / "a.ref")])
    assert not orphan.exists()
    assert not (cache_dir / "a.ref").exists()
    assert (library_dir / "用户自己的.lrc").exists()
    assert (library_dir / "歌手B - 歌2.lrc").exists()
    assert (cache_dir / "b.ref").exists()
    assert fresh.exists()
    assert (cache_dir / "c.ref").exists()
    assert (cache_dir / "online_kuwo_9.lrc").exists()
    assert (cache_dir / "e.ref").exists()


def test_sweep_refuses_dirs_outside_media_roots(tmp_path):
    """ref 指向的词干不在 media_dirs 内（别的目录）→ 即使是孤儿也绝不动。"""
    cache_dir = tmp_path / "cache"
    library_dir = tmp_path / "library"
    elsewhere = tmp_path / "elsewhere"
    for d in (cache_dir, library_dir, elsewhere):
        d.mkdir()
    orphan = elsewhere / "歌手A - 歌1.lrc"
    orphan.write_text("lrc", encoding="utf-8")
    os.utime(orphan, (1000, 1000))
    _reflay(cache_dir, "a", elsewhere / "歌手A - 歌1")

    assert gc.sweep_orphan_lyrics(str(cache_dir), [str(library_dir)]) == []
    assert orphan.exists()
    assert (cache_dir / "a.ref").exists()


def test_sweep_missing_cache_dir_is_noop(tmp_path):
    assert gc.sweep_orphan_lyrics(str(tmp_path / "nope"), [str(tmp_path)]) == []


# === 取流探针与后台门控 ===

def test_stream_probe_logs_range_shape(monkeypatch, caplog):
    """探针记录 Range 原文与 tee 资格：定长窗口 False、开区间 0- True。"""
    monkeypatch.setitem(p.CONF, "stream_probe", True)
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        p.log_stream_probe("GET", "online:netease:1", "bytes=0-1048575", False)
        p.log_stream_probe("GET", "online:netease:1", "bytes=0-", False)
        p.log_stream_probe("HEAD", "online:netease:1", None, True)
    messages = [r.getMessage() for r in caplog.records]
    assert any("bytes=0-1048575" in m and "tee_eligible=False" in m for m in messages)
    assert any("bytes=0-" in m and "tee_eligible=True" in m for m in messages)
    assert any("HEAD" in m and "cached=True" in m for m in messages)


def test_stream_probe_switch_off(monkeypatch, caplog):
    monkeypatch.setitem(p.CONF, "stream_probe", False)
    with caplog.at_level(logging.INFO, logger="fnmusic_proxy"):
        p.log_stream_probe("GET", "online:netease:1", "bytes=0-", False)
    assert caplog.records == []


def test_background_jobs_disabled_by_default(monkeypatch):
    """后台清扫仅在服务环境（takeover 注入）启用，测试/手动导入默认关闭。"""
    monkeypatch.delenv("FNMUSIC_BACKGROUND_JOBS", raising=False)
    assert p._background_jobs_enabled() is False
    monkeypatch.setenv("FNMUSIC_BACKGROUND_JOBS", "1")
    assert p._background_jobs_enabled() is True
