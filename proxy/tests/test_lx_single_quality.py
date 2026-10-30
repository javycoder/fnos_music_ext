"""测试 LX 代理只传一次目标档，由下游统一降档而非双重循环。"""
import pytest
import httpx
from proxy.app import CONF, resolve_lx_url
from proxy import quality_policy as qp


@pytest.mark.anyio
async def test_resolve_lx_url_single_target_request_lossless(monkeypatch):
    """当配置了新七档 play_quality=lossless 时，LX 代理转换为 flac 并只请求一次，不发起双重循环。"""
    seen_qualities = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_qualities.append(request.url.params.get("quality"))
        # 下游服务统一降档后若依然找不到，返回 404
        return httpx.Response(404, json={"ok": False, "data": None})

    monkeypatch.setitem(CONF, "play_quality", "lossless")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772") as client:
        res = await resolve_lx_url(client, "lx:kg:HASH_ONCE")
        assert res is None

    # 关键断言：只请求了 1 次，参数为 flac，绝不进行多次双重循环
    assert seen_qualities == ["flac"]


@pytest.mark.anyio
@pytest.mark.parametrize("play_q,expected_lx", [
    ("standard", "128k"),
    ("higher", "192k"),
    ("exhigh", "320k"),
    ("lossless", "flac"),
    ("hires", "flac24bit"),
    ("jymaster", "master"),
    ("sky", "atmos"),
])
async def test_resolve_lx_url_maps_all_seven_levels(monkeypatch, play_q, expected_lx):
    """验证新七档全部正确映射为 LX 请求参数，且均为单次调用。"""
    seen_qualities = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_qualities.append(request.url.params.get("quality"))
        return httpx.Response(200, json={
            "ok": True,
            "data": {"id": "x", "url": f"http://audio.test/{expected_lx}.audio", "quality": expected_lx}
        })

    monkeypatch.setitem(CONF, "play_quality", play_q)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772") as client:
        res = await resolve_lx_url(client, f"lx:kw:SONG_{play_q}")
        assert res is not None
        assert res["url"] == f"http://audio.test/{expected_lx}.audio"

    assert seen_qualities == [expected_lx]


@pytest.mark.anyio
async def test_resolve_lx_url_respects_max_quality_with_single_call(monkeypatch):
    """当 max_quality 限制为有损时，高档位被截断到 320k，且依然只发一次请求。"""
    seen_qualities = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_qualities.append(request.url.params.get("quality"))
        return httpx.Response(200, json={
            "ok": True,
            "data": {"id": "x", "url": "http://audio.test/320k.mp3", "quality": "320k", "ext": "mp3"}
        })

    monkeypatch.setitem(CONF, "play_quality", "hires")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8772") as client:
        res = await resolve_lx_url(client, "lx:tx:SONG_MAXQ", max_quality="high")
        assert res is not None

    # hires (flac24bit) 受限于 max_quality=high 截断为 exhigh (320k)，且仅 1 次请求
    assert seen_qualities == ["320k"]
