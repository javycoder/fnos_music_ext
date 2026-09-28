"""自动下载封面：封面字节嗅探 / 内嵌各格式 / _tee_finalize 接线时序测试。"""
import importlib
import os
import shutil
import subprocess

import httpx
import pytest

p = importlib.import_module("proxy.app")

JPEG = b"\xFF\xD8\xFF" + b"fake-jpeg-body" + b"\xFF\xD9"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 fake-webp-body"


def make_audio(tmp_path, name, *codec_args):
    """ffmpeg 生成 0.2s 静音样本（无 ffmpeg 环境 skip，与 test_merge 同约定）。"""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = str(tmp_path / name)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.2", *codec_args, path],
        check=True, capture_output=True,
    )
    return path


def read_apic(path):
    from mutagen import File as MutagenFile

    mf = MutagenFile(path)
    pics = mf.tags.getall("APIC") if hasattr(mf.tags, "getall") else []
    return pics[0] if pics else None


# ---------------------------------------------------------------- 封面下载嗅探 --

def test_download_cover_bytes_sniffs_magic_over_content_type(monkeypatch):
    """Content-Type 说谎不影响：按魔数识别 jpeg/png，返回字节与 mime。"""
    def handler(request: httpx.Request) -> httpx.Response:
        body = JPEG if "jpg" in request.url.path else PNG
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=body)

    monkeypatch.setattr(p, "_COVER_FETCH_TRANSPORT", httpx.MockTransport(handler))
    got = p.download_cover_bytes("https://img.example.com/a.jpg")
    assert got == (JPEG, "image/jpeg")
    got = p.download_cover_bytes("https://img.example.com/a.png")
    assert got == (PNG, "image/png")


def test_download_cover_bytes_rejects_non_image(monkeypatch):
    """HTML/纯文本/空响应/非 200 一律 None，绝不返回伪封面字节。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "html" in request.url.path:
            return httpx.Response(200, content=b"<html>not an image</html>")
        if "empty" in request.url.path:
            return httpx.Response(200, content=b"")
        return httpx.Response(404)

    monkeypatch.setattr(p, "_COVER_FETCH_TRANSPORT", httpx.MockTransport(handler))
    assert p.download_cover_bytes("https://img.example.com/html") is None
    assert p.download_cover_bytes("https://img.example.com/empty") is None
    assert p.download_cover_bytes("https://img.example.com/missing") is None


def test_download_cover_bytes_rejects_kw_text_host_and_bad_scheme():
    """酷我 artistpicserver 文本假图与非 http(s) 输入直接拒绝（不发请求）。"""
    assert p.download_cover_bytes("https://artistpicserver.kuwo.cn/pic?rid=1") is None
    assert p.download_cover_bytes("file:///etc/passwd") is None
    assert p.download_cover_bytes("") is None


def test_download_cover_bytes_rejects_oversize(monkeypatch):
    """超过 10MB 的图片拒收，防异常大图内嵌拖垮落盘。"""
    big = b"\xFF\xD8\xFF" + b"x" * (p._COVER_FETCH_MAX_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big)

    monkeypatch.setattr(p, "_COVER_FETCH_TRANSPORT", httpx.MockTransport(handler))
    assert p.download_cover_bytes("https://img.example.com/huge.jpg") is None


# ---------------------------------------------------------------- 内嵌各格式 --

def test_embed_audio_cover_mp3(tmp_path):
    path = make_audio(tmp_path, "sample.mp3", "-q:a", "9")
    assert p.embed_audio_cover(path, JPEG, "image/jpeg") is True
    pic = read_apic(path)
    assert pic is not None and pic.data == JPEG and pic.mime == "image/jpeg"


def test_embed_audio_cover_flac(tmp_path):
    from mutagen import File as MutagenFile

    path = make_audio(tmp_path, "sample.flac", "-c:a", "flac")
    assert p.embed_audio_cover(path, PNG, "image/png") is True
    pics = MutagenFile(path).pictures
    assert len(pics) == 1 and pics[0].data == PNG


def test_embed_audio_cover_mp4_rejects_webp(tmp_path):
    """MP4 covr 只认 jpeg/png：webp 拒绝且不写入。"""
    from mutagen import File as MutagenFile

    path = make_audio(tmp_path, "sample.m4a", "-c:a", "aac", "-b:a", "8k")
    assert p.embed_audio_cover(path, WEBP, "image/webp") is False
    assert not MutagenFile(path).tags.get("covr")
    assert p.embed_audio_cover(path, JPEG, "image/jpeg") is True


def test_embed_audio_cover_skips_existing(tmp_path):
    """源站自带内嵌封面时不重复嵌入，保持幂等。"""
    path = make_audio(tmp_path, "sample.mp3", "-q:a", "9")
    assert p.embed_audio_cover(path, JPEG, "image/jpeg") is True
    assert p.embed_audio_cover(path, PNG, "image/png") is False
    assert read_apic(path).data == JPEG


# ---------------------------------------------------------------- finalize 接线 --

@pytest.fixture
def finalized_env(tmp_path, monkeypatch):
    for key in ("cache_dir", "library_dir", "fav_dir"):
        monkeypatch.setitem(p.CONF, key, str(tmp_path / key))
    monkeypatch.setitem(p.CONF, "music_db", str(tmp_path / "missing.db"))
    monkeypatch.setitem(p.CONF, "tee_save_dir", "")
    monkeypatch.setitem(p.CONF, "auto_cover", True)
    monkeypatch.setattr(p, "_TEE_SAVE_DIR_WARNED", False)
    os.makedirs(p.CONF["library_dir"], exist_ok=True)
    os.makedirs(p.CONF["cache_dir"], exist_ok=True)
    yield monkeypatch
    p._full_fetch_tasks.clear()


def finalize_mp3(tmp_path, info, tee_enabled=True):
    """造一个完整 part 并直接跑 _tee_finalize（同步），返回落盘路径。"""
    part = make_audio(tmp_path, "part.mp3", "-q:a", "9")
    meta = p._tee_finalize(part, "online:kuwo:123", "mp3", info, tee_enabled)
    assert meta and meta["dest"]
    return meta["dest"]


def test_finalize_embeds_cover_after_tags(finalized_env, tmp_path):
    """开关开 + cover_url 非空：落库、写标签、内嵌封面一气呵成。"""
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return JPEG, "image/jpeg"

    finalized_env.setattr(p, "download_cover_bytes", fake_fetch)
    dest = finalize_mp3(tmp_path, {"title": "晴天", "artist": "周杰伦", "cover_url": "https://img.example.com/a.jpg"})
    assert calls == ["https://img.example.com/a.jpg"]
    from mutagen import File as MutagenFile

    tagged = MutagenFile(dest, easy=True)
    assert tagged["title"] == ["晴天"]  # 标签仍在
    assert read_apic(dest).data == JPEG  # 封面已嵌


def test_finalize_no_cover_url_no_fetch(finalized_env, tmp_path):
    """cover_url 为空（如 lx kw 榜单）：不发请求、正常落库。"""
    calls = []
    finalized_env.setattr(p, "download_cover_bytes", lambda url: calls.append(url))
    dest = finalize_mp3(tmp_path, {"title": "晴天", "artist": "周杰伦", "cover_url": ""})
    assert calls == [] and read_apic(dest) is None


def test_finalize_auto_cover_off_no_fetch(finalized_env, tmp_path):
    """「自动下载封面」开关关：不请求、不内嵌。"""
    calls = []
    finalized_env.setitem(p.CONF, "auto_cover", False)
    finalized_env.setattr(p, "download_cover_bytes", lambda url: calls.append(url))
    dest = finalize_mp3(tmp_path, {"title": "晴天", "artist": "周杰伦", "cover_url": "https://img.example.com/a.jpg"})
    assert calls == [] and read_apic(dest) is None


def test_finalize_tee_off_rolling_cache_no_fetch(finalized_env, tmp_path):
    """边听边存关（滚动缓存）：封面端点有在线兜底链，无需内嵌。"""
    calls = []
    finalized_env.setattr(p, "download_cover_bytes", lambda url: calls.append(url))
    finalize_mp3(tmp_path, {"title": "晴天", "artist": "周杰伦", "cover_url": "https://img.example.com/a.jpg"}, tee_enabled=False)
    assert calls == []


def test_finalize_cover_fetch_failure_never_breaks(finalized_env, tmp_path):
    """封面下载失败/嗅探不通过：落库与标签不受影响（仅无封面）。"""
    finalized_env.setattr(p, "download_cover_bytes", lambda url: None)
    dest = finalize_mp3(tmp_path, {"title": "晴天", "artist": "周杰伦", "cover_url": "https://img.example.com/a.jpg"})
    from mutagen import File as MutagenFile

    assert MutagenFile(dest, easy=True)["title"] == ["晴天"]
    assert read_apic(dest) is None


def test_finalize_real_fetch_via_transport(finalized_env, tmp_path):
    """走真实下载路径（MockTransport 注入）：httpx 拉字节→魔数嗅探→内嵌。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG)

    finalized_env.setattr(p, "_COVER_FETCH_TRANSPORT", httpx.MockTransport(handler))
    dest = finalize_mp3(tmp_path, {"title": "晴天", "artist": "周杰伦", "cover_url": "https://img.example.com/a.jpg"})
    assert read_apic(dest).data == JPEG
