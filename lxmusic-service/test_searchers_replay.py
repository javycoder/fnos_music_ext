"""内置平台搜索器回放契约测试：合成负载（精确断言）+ 真实录制回放（形状断言）。

两层防护：
1. 合成负载：按各搜索器期望的字段形状手工构造，逐字段精确断言
   （VIP 转探活、试听剔除、无 id 剔除、相关度排序、字段映射）。
2. 真实回放：lxmusic-service/tests/fixtures/ 下是
   tests/integration/capture_search_fixtures.py 录制的上游真实响应——
   上游接口改版（字段改名/结构变化）时回放立即失败，提示刷新 fixture
   并修正解析器，而不是等到用户搜索全空。

fixtures 缺失（如录制网络不通的平台）对应用例自动跳过并给出补录指引。
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from conftest import FakeRuntime, lxapp, mock_client

FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
PLATFORMS = ("kg", "wy", "mg", "tx", "kw")

# 探活成功的最小 FLAC 前缀（probe_url 只做前缀魔数校验）
_FLAC = b"fLaC" + b"\x00" * 8192


def media_handler(body_for_search: dict | str) -> httpx.MockTransport:
    """搜索 URL 回放 fixture；media.test 直链回最小 FLAC。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://media.test/"):
            return httpx.Response(
                206, content=_FLAC,
                headers={"content-range": f"bytes 0-{len(_FLAC)-1}/{len(_FLAC)+123456}"},
            )
        if isinstance(body_for_search, str):
            return httpx.Response(200, text=body_for_search)
        return httpx.Response(200, json=body_for_search)

    return handler


@pytest.fixture
def with_source(isolated):
    """启用全平台用户源替身：直链统一返回 media.test FLAC。"""
    isolated._runtime = FakeRuntime(
        platforms={p: ["128k", "320k", "flac"] for p in PLATFORMS}
    )
    return isolated


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ 合成负载：kg ---

def _kg_payload() -> dict:
    return {"data": {"info": [
        {"hash": "h1", "songname": "晴天", "singername": "周杰伦", "album_name": "叶惠美",
         "album_id": 966846,
         "duration": 269000, "pay_type": 0, "sqhash": "sq1", "hqhash": "hq1",
         "sq_size": 27000000, "filesize": 4300000,
         "origin_cover": "http://img/x{size}.jpg", "mixsongid": 77},
        {"hash": "h2", "songname": "付费曲", "singername": "歌手乙", "album_name": "B",
         "duration": 200000, "pay_type": 1, "filesize": 3000000},
        {"hash": "h3", "songname": "试听片段版", "singername": "x", "is_free_part": 1},
        {"songname": "没有hash", "singername": "x"},
        {"hash": "h5", "songname": "VIP拦截曲", "singername": "x", "fail_process": 4},
    ]}}


def test_kg_search_contract(with_source):
    http = mock_client(media_handler(_kg_payload()))
    try:
        items = run(lxapp.kg_search(http, "晴天", 10))
    finally:
        run(http.aclose())
    by_id = {it["id"]: it for it in items}
    # 免费曲目直接返回，字段逐项映射
    first = by_id["lx:kg:h1"]
    assert first["title"] == "晴天" and first["artist"] == "周杰伦"
    assert first["album"] == "叶惠美"
    assert first["duration_s"] == 269.0
    assert first["ext"] == "flac"          # sqhash 存在 → 无损
    assert first["cover_url"] == "http://img/x480.jpg"  # {size} → 480
    assert first["file_size"] == 27000000  # 优先 sq_size
    assert first["lx_source"] == "kg"
    assert first["album_id"] == "966846"
    # 付费曲不丢弃：转探活队列，探活通过后以 verified 补进结果
    vip = by_id["lx:kg:h2"]
    assert vip["validation_status"] == "media_verified"
    assert vip["verified"] is True
    # 试听标记 / 缺 hash / VIP 拦截(fail_process=4) 一律剔除
    assert "lx:kg:h3" not in by_id and "lx:kg:h5" not in by_id
    assert len(items) == 2


def test_kg_resolve_lyric_contract():
    lrc_text = "[00:01.00]晴天 - 周杰伦\n[00:05.00]故事的小黄花\n"
    state = {"step": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "krcs.kugou.com/search" in url:
            return httpx.Response(200, json={"candidates": [{"id": 9, "accesskey": "k"}]})
        return httpx.Response(200, json={"content": base64.b64encode(lrc_text.encode()).decode()})

    http = mock_client(handler)
    try:
        out = run(lxapp.kg_resolve_lyric(http, {
            "title": "晴天", "artist": "周杰伦", "duration_s": 269.0, "hash": "h1"}))
    finally:
        run(http.aclose())
    assert out == lrc_text  # base64 解码为 UTF-8 lrc 原文


# ------------------------------------------------------------ 合成负载：wy ---

def _wy_payload() -> dict:
    return {"result": {"songs": [
        {"id": 100, "name": "晴天", "fee": 0,
         "artists": [{"name": "周杰伦"}],
         "album": {"name": "叶惠美", "picUrl": "http://p/100.jpg"},
         "duration": 269000},
        {"id": 200, "name": "付费曲", "fee": 1,
         "artists": [{"name": "甲"}, {"name": "乙"}],
         "album": {"name": "B", "picUrl": "http://p/200.jpg"},
         "duration": 200000},
        {"id": 300, "name": "无版权", "fee": 0, "artists": [], "album": {},
         "duration": 1000, "noCopyrightRcmd": {"type": 1}},
    ]}}


def test_wy_search_contract(with_source):
    http = mock_client(media_handler(_wy_payload()))
    try:
        items = run(lxapp.wy_search(http, "晴天", 10))
    finally:
        run(http.aclose())
    by_id = {it["id"]: it for it in items}
    free = by_id["lx:wy:100"]
    assert free["title"] == "晴天" and free["artist"] == "周杰伦"
    assert free["album"] == "叶惠美" and free["cover_url"] == "http://p/100.jpg"
    assert free["duration_s"] == 269.0 and free["ext"] == "mp3"
    # fee∉(0,8) → VIP 候选，探活通过补进；多歌手 " / " 连接
    vip = by_id["lx:wy:200"]
    assert vip["artist"] == "甲 / 乙" and vip["verified"] is True
    # 无版权曲目剔除
    assert "lx:wy:300" not in by_id
    assert len(items) == 2


def test_wy_resolve_lyric_contract():
    http = mock_client(lambda r: httpx.Response(
        200, json={"lrc": {"lyric": "[00:01.00]故事的小黄花\n"}}))
    try:
        out = run(lxapp.wy_resolve_lyric(http, "100"))
    finally:
        run(http.aclose())
    assert out == "[00:01.00]故事的小黄花\n"


# ------------------------------------------------------------ 合成负载：mg ---

def test_mg_search_contract(with_source):
    payload = {"songResultData": {"result": [
        {"copyrightId": "6001", "id": "9001", "songName": "晴天",
         "singers": [{"name": "周杰伦"}], "albums": [{"albumName": "叶惠美"}],
         "albumMaterialList": [{"coverUrl": "http://m/c.jpg"}],
         "toneFlags": [{"toneType": "SQ"}], "length": 240000,
         "lrcUrl": "http://m/lrc.lrc"},
        {"songName": "无id曲目"},  # copyrightId 与 id 全缺 → 剔除
        {"id": "9003", "songName": "仅id无copyrightId", "singers": [], "length": 100},
    ]}}
    http = mock_client(media_handler(payload))
    try:
        items = run(lxapp.mg_search(http, "晴天", 10))
    finally:
        run(http.aclose())
    # mg 全量走探活：通过者 verified；copyrightId 缺失时回退 id 字段
    ids = {it["id"] for it in items}
    assert ids == {"lx:mg:6001", "lx:mg:9003"}
    first = next(it for it in items if it["id"] == "lx:mg:6001")
    assert first["title"] == "晴天" and first["artist"] == "周杰伦"
    assert first["album"] == "叶惠美" and first["cover_url"] == "http://m/c.jpg"
    assert first["lrc_url"] == "http://m/lrc.lrc"
    assert first["duration_s"] == 240.0
    assert first["validation_status"] == "media_verified"


# ------------------------------------------------------------ 合成负载：tx ---

def test_cold_tx_resolve_fills_media_mid(with_source):
    """播放缓存过期后只剩 songmid，解析前要补回 media mid 和专辑 id。"""
    detail = {"data": [{
        "mid": "m9", "name": "晴天",
        "file": {"media_mid": "media9"},
        "album": {"id": 8220, "name": "叶惠美"},
    }]}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "fcg_play_single_song" in url:
            return httpx.Response(200, json=detail)
        if url.startswith("https://media.test/"):
            return httpx.Response(
                206, content=_FLAC,
                headers={"content-range": f"bytes 0-{len(_FLAC)-1}/{len(_FLAC)+123456}"},
            )
        return httpx.Response(404)

    http = mock_client(handler)
    item = {"id": "lx:tx:m9", "lx_source": "tx", "title": "晴天", "artist": "周杰伦"}
    try:
        result = run(lxapp.resolve_and_probe(http, "tx", item, "standard"))
    finally:
        run(http.aclose())
    assert result and str(result.get("url") or "").startswith("http")
    info = with_source._runtime.calls[0]["info"]
    assert info["songmid"] == "m9"
    assert info["strMediaMid"] == "media9"
    assert info["albumId"] == "8220"


def test_cold_kg_resolve_fills_album_id(with_source):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "getSongInfo.php" in url:
            return httpx.Response(200, json={"albumid": 966846, "songName": "晴天", "hash": "h9"})
        if url.startswith("https://media.test/"):
            return httpx.Response(
                206, content=_FLAC,
                headers={"content-range": f"bytes 0-{len(_FLAC)-1}/{len(_FLAC)+123456}"},
            )
        return httpx.Response(404)

    http = mock_client(handler)
    item = {"id": "lx:kg:h9", "lx_source": "kg", "title": "晴天", "artist": "周杰伦", "hash": "h9"}
    try:
        result = run(lxapp.resolve_and_probe(http, "kg", item, "standard"))
    finally:
        run(http.aclose())
    assert result and str(result.get("url") or "").startswith("http")
    info = with_source._runtime.calls[0]["info"]
    assert info["hash"] == "h9"
    assert info["albumId"] == "966846"


def test_tx_search_contract(with_source):
    songs = {"list": [
        {"mid": "m1", "title": "晴天", "singer": [{"name": "周杰伦"}],
         "album": {"name": "叶惠美", "mid": "am1", "id": 8220}, "interval": 269,
         "file": {"media_mid": "media1"},
         "pay": {"pay_play": 0}},
        {"mid": "", "title": "无mid", "singer": [], "album": {}, "interval": 1},
        {"mid": "m3", "title": "试听版", "singer": [], "album": {}, "interval": 1,
         "trial": 1},
    ]}
    payload = {"req_1": {"data": {"body": {"song": songs}}}}
    http = mock_client(media_handler(payload))
    try:
        items = run(lxapp.tx_search(http, "晴天", 10))
    finally:
        run(http.aclose())
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "lx:tx:m1" and item["title"] == "晴天"
    assert item["artist"] == "周杰伦" and item["album"] == "叶惠美"
    assert item["duration_s"] == 269
    assert item["cover_url"].startswith("https://y.gtimg.cn/music/photo_new/T002R300x300M000am1")
    assert item["str_media_mid"] == "media1"
    assert item["album_id"] == "8220"
    assert item["validation_status"] == "media_verified"


# ------------------------------------------------------------ 合成负载：kw ---

def _kw_body() -> str:
    # r.s 老接口返回 Python 字面量（单引号）；乱序放置验证标题相关度重排
    # （注意：带括号后缀会被相关度归一剥掉，故用无括号后缀制造区分度）
    return (
        "{'abslist':["
        "{'MUSICRID':'MUSIC_9002','SONGNAME':'晴天翻唱合集','ARTIST':'路人','ALBUM':'X',"
        "'DURATION':130,'PAY':0,'payInfo':{'cannotOnlinePlay':'0'}},"
        "{'MUSICRID':'MUSIC_9001','SONGNAME':'晴天','ARTIST':'周杰伦','ALBUM':'叶惠美',"
        "'DURATION':269,'PAY':0,'web_albumpic_short':'a/b.jpg',"
        "'payInfo':{'cannotOnlinePlay':'0'}},"
        "{'MUSICRID':'MUSIC_9003','SONGNAME':'无版权','ARTIST':'x','ALBUM':'x',"
        "'DURATION':100,'PAY':0,'payInfo':{'cannotOnlinePlay':'1'}},"
        "{'MUSICRID':'BAD','SONGNAME':'坏id','ARTIST':'x','ALBUM':'x',"
        "'DURATION':100,'PAY':0,'payInfo':{'cannotOnlinePlay':'0'}}"
        "]}"
    )


def test_kw_search_contract(with_source):
    http = mock_client(media_handler(_kw_body()))
    try:
        items = run(lxapp.kw_search(http, "晴天", 10))
    finally:
        run(http.aclose())
    # 无版权(cannotOnlinePlay=1)与非法 MUSICRID 剔除；原版按相关度排最前
    ids = [it["id"] for it in items]
    assert ids[0] == "lx:kw:9001"
    assert set(ids) == {"lx:kw:9001", "lx:kw:9002"}
    first = items[0]
    assert first["title"] == "晴天" and first["artist"] == "周杰伦"
    assert first["album"] == "叶惠美" and first["duration_s"] == 269
    assert first["cover_url"] == "https://img1.kuwo.cn/star/albumcover/a/b.jpg"
    assert first["validation_status"] == "media_verified"


# ------------------------------------------------------- 真实录制回放（形状断言） ---

def _fixture(name: str) -> bytes | None:
    path = FIXTURES / name
    return path.read_bytes() if path.exists() else None


def _replay(name: str, searcher_name: str, src: str, isolated):
    body = _fixture(name)
    if body is None:
        pytest.skip(f"缺少 {name}：运行 tests/integration/capture_search_fixtures.py 录制")
    isolated._runtime = FakeRuntime(platforms={p: ["128k", "320k", "flac"] for p in PLATFORMS})
    if name.endswith(".txt"):
        content = body.decode("utf-8", "replace")

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith("https://media.test/"):
                return httpx.Response(206, content=_FLAC, headers={
                    "content-range": f"bytes 0-{len(_FLAC)-1}/{len(_FLAC)+999}"})
            return httpx.Response(200, text=content)
    else:
        payload = json.loads(body)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith("https://media.test/"):
                return httpx.Response(206, content=_FLAC, headers={
                    "content-range": f"bytes 0-{len(_FLAC)-1}/{len(_FLAC)+999}"})
            return httpx.Response(200, json=payload)
    http = mock_client(handler)
    try:
        searcher = getattr(lxapp, searcher_name)
        return run(searcher(http, "晴天", 10))
    finally:
        run(http.aclose())


@pytest.mark.parametrize("name,fn,src", [
    ("kg_search.json", "kg_search", "kg"),
    ("wy_search.json", "wy_search", "wy"),
    ("tx_search.json", "tx_search", "tx"),
    ("kw_search.txt", "kw_search", "kw"),
    ("mg_search.json", "mg_search", "mg"),
])
def test_real_capture_replay(name, fn, src, isolated):
    """真实上游响应回放：解析不炸、返回形状合规、至少一条结果。"""
    items = _replay(name, fn, src, isolated)
    assert items, f"{name} 回放解析结果为空：上游接口可能已改版，请刷新 fixture"
    for it in items:
        assert it["id"].startswith(f"lx:{src}:"), it["id"]
        assert isinstance(it["title"], str) and it["title"]
        assert isinstance(it["artist"], str)
        assert it["duration_s"] >= 0
        assert it["lx_source"] == src
    assert len(items) <= 10 + 10 // 2  # 上限契约：limit + limit//2


def test_real_capture_kg_lyric_replay(isolated):
    search_body = _fixture("kg_lyric_search.json")
    download_body = _fixture("kg_lyric_download.json")
    if search_body is None or download_body is None:
        pytest.skip("缺少 kg 歌词 fixture：运行 capture_search_fixtures.py 录制")
    search_json = json.loads(search_body)
    download_json = json.loads(download_body)

    def handler(request: httpx.Request) -> httpx.Response:
        if "krcs.kugou.com/search" in str(request.url):
            return httpx.Response(200, json=search_json)
        return httpx.Response(200, json=download_json)

    http = mock_client(handler)
    try:
        item = {"title": "晴天", "artist": "周杰伦", "duration_s": 269.0, "hash": "x"}
        out = run(lxapp.kg_resolve_lyric(http, item))
    finally:
        run(http.aclose())
    # 真实 base64 内容可解码出 lrc 时间轴
    assert isinstance(out, str)
    if out:
        assert "[" in out and "]" in out


def test_real_capture_wy_lyric_replay(isolated):
    body = _fixture("wy_lyric.json")
    if body is None:
        pytest.skip("缺少 wy 歌词 fixture：运行 capture_search_fixtures.py 录制")
    payload = json.loads(body)
    http = mock_client(lambda r: httpx.Response(200, json=payload))
    try:
        out = run(lxapp.wy_resolve_lyric(http, "1"))
    finally:
        run(http.aclose())
    assert isinstance(out, str)
    if out:
        assert "[" in out


def test_fixture_meta_records_capture():
    """meta.json 必须存在且记录录制时间（fixture 是有日期的事实记录，不是永久真理）。"""
    meta_path = FIXTURES / "meta.json"
    if not meta_path.exists():
        pytest.skip("尚未录制 fixture")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["captured_at"]
    assert meta["keyword"]
