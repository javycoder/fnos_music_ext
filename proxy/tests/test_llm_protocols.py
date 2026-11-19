"""issue #83：LLM chat/responses 双协议、失败可见性与每日 token 限额。

覆盖：两种协议的请求形状与响应解析、#83 场景（HTTP 200 但正文为空）、
失败原因码进 tierFailures、每日 token 记账与限额拦截、附加请求参数合并。
HTTP 一律走 httpx.MockTransport，不出网。
"""
import asyncio
import json
import os
import stat
import time

import httpx
import pytest

from proxy import llm_usage
from proxy import recommend as dailyrec


@pytest.fixture
def anyio_backend():
    return "asyncio"


_CANDIDATES = [
    {"title": f"双协议歌{i}", "artist": f"协议歌手{i}", "dimension": "genre", "reason": "t"}
    for i in range(30)
]


def _chat_body(content: str, finish: str = "stop", usage: dict | None = None) -> dict:
    body = {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
    return body


def _responses_body(text: str, status: str = "completed", usage: dict | None = None) -> dict:
    return {
        "status": status,
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": usage if usage is not None else {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    }


def _configure_llm(monkeypatch, base="http://llm.invalid/v1"):
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", base)
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_LLM_MODEL", "test-model")


def _capture_transport(captured: list, responder):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responder(request)
    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_chat_protocol_request_shape_and_parse(monkeypatch):
    _configure_llm(monkeypatch)
    captured: list = []
    content = json.dumps(_CANDIDATES, ensure_ascii=False)
    transport = _capture_transport(
        captured, lambda r: httpx.Response(200, json=_chat_body(content)))
    async with httpx.AsyncClient(transport=transport) as client:
        recs = await dailyrec.call_llm(client, "推荐一些歌")
    assert len(recs) == len(_CANDIDATES)
    req = captured[0]
    assert req.url.path.endswith("/chat/completions")
    assert req.headers["Authorization"] == "Bearer test-key"
    payload = json.loads(req.content)
    assert payload["model"] == "test-model"
    assert payload["temperature"] == 0.8
    assert payload["max_tokens"] == 8192  # issue #83：不再是硬编码 4096
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    # usage 记入当日账本
    state = llm_usage.used_today()
    assert state["prompt"] == 100 and state["completion"] == 50 and state["requests"] == 1


@pytest.mark.anyio
async def test_responses_protocol_request_shape_and_parse(monkeypatch):
    _configure_llm(monkeypatch)
    monkeypatch.setenv("FNMUSIC_LLM_PROTOCOL", "responses")
    captured: list = []
    content = json.dumps(_CANDIDATES, ensure_ascii=False)
    transport = _capture_transport(
        captured, lambda r: httpx.Response(200, json=_responses_body(content)))
    async with httpx.AsyncClient(transport=transport) as client:
        recs = await dailyrec.call_llm(client, "推荐一些歌")
    assert len(recs) == len(_CANDIDATES)
    req = captured[0]
    assert req.url.path.endswith("/responses")
    payload = json.loads(req.content)
    assert payload["max_output_tokens"] == 8192
    assert [m["role"] for m in payload["input"]] == ["system", "user"]
    # 推理系模型兼容：responses 协议默认不发 temperature/max_tokens/messages
    assert "temperature" not in payload
    assert "max_tokens" not in payload
    assert "messages" not in payload
    # input/output token 字段名归一进同一账本
    state = llm_usage.used_today()
    assert state["prompt"] == 100 and state["completion"] == 50 and state["total"] == 150


@pytest.mark.anyio
@pytest.mark.parametrize("protocol,suffix", [("chat", "/chat/completions"), ("responses", "/responses")])
async def test_base_url_with_suffix_not_duplicated(monkeypatch, protocol, suffix):
    _configure_llm(monkeypatch, base=f"http://llm.invalid/v1{suffix}")
    monkeypatch.setenv("FNMUSIC_LLM_PROTOCOL", protocol)
    captured: list = []
    content = json.dumps(_CANDIDATES, ensure_ascii=False)
    transport = _capture_transport(
        captured,
        (lambda r: httpx.Response(200, json=_chat_body(content))) if protocol == "chat"
        else (lambda r: httpx.Response(200, json=_responses_body(content))))
    async with httpx.AsyncClient(transport=transport) as client:
        await dailyrec.call_llm(client, "p")
    assert str(captured[0].url).count(suffix) == 1


@pytest.mark.anyio
async def test_extra_body_merged_and_invalid_ignored(monkeypatch):
    _configure_llm(monkeypatch)
    monkeypatch.setenv("FNMUSIC_LLM_EXTRA_BODY", '{"thinking":{"type":"disabled"},"user":"u1"}')
    captured: list = []
    content = json.dumps(_CANDIDATES, ensure_ascii=False)
    transport = _capture_transport(
        captured, lambda r: httpx.Response(200, json=_chat_body(content)))
    async with httpx.AsyncClient(transport=transport) as client:
        await dailyrec.call_llm(client, "p")
    payload = json.loads(captured[0].content)
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["user"] == "u1"
    # 非法 JSON：忽略并继续（不抛异常）
    monkeypatch.setenv("FNMUSIC_LLM_EXTRA_BODY", "not-json{")
    captured.clear()
    async with httpx.AsyncClient(transport=transport) as client:
        await dailyrec.call_llm(client, "p")
    payload = json.loads(captured[0].content)
    assert "thinking" not in payload


@pytest.mark.anyio
async def test_issue83_empty_content_raises_with_reason(monkeypatch):
    """#83 核心：HTTP 200、正文为空、reasoning 吃满预算必须显式失败。"""
    _configure_llm(monkeypatch)
    usage = {"prompt_tokens": 100, "completion_tokens": 4096, "total_tokens": 4196,
             "completion_tokens_details": {"reasoning_tokens": 4096}}
    transport = _capture_transport(
        [], lambda r: httpx.Response(200, json=_chat_body("", finish="length", usage=usage)))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")
    assert excinfo.value.reason == "empty-content"
    assert "finish_reason=length" in excinfo.value.detail
    assert "reasoning_tokens=4096" in excinfo.value.detail
    # 失败也要记账与留痕
    state = llm_usage.used_today()
    assert state["total"] == 4196
    assert state["lastFailure"]["reason"] == "empty-content"


@pytest.mark.anyio
async def test_responses_incomplete_empty_text_raises(monkeypatch):
    _configure_llm(monkeypatch)
    monkeypatch.setenv("FNMUSIC_LLM_PROTOCOL", "responses")
    usage = {"input_tokens": 10, "output_tokens": 4096, "total_tokens": 4106,
             "output_tokens_details": {"reasoning_tokens": 4096}}
    transport = _capture_transport(
        [], lambda r: httpx.Response(200, json=_responses_body("", status="incomplete", usage=usage)))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")
    assert excinfo.value.reason == "empty-content"
    assert "status=incomplete" in excinfo.value.detail


@pytest.mark.anyio
async def test_not_configured_raises(monkeypatch):
    monkeypatch.delenv("FNMUSIC_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("FNMUSIC_LLM_API_KEY", raising=False)
    async with httpx.AsyncClient() as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")
    assert excinfo.value.reason == "not-configured"


@pytest.mark.anyio
async def test_http_error_timeout_and_bad_json_reasons(monkeypatch):
    _configure_llm(monkeypatch)
    # HTTP 401
    transport = _capture_transport([], lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")
    assert excinfo.value.reason == "http-401"
    assert llm_usage.used_today()["lastFailure"]["reason"] == "http-401"
    # 响应不是合法 JSON
    transport = _capture_transport([], lambda r: httpx.Response(200, text="<html>gateway</html>"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")
    assert excinfo.value.reason == "bad-response"
    # 超时：MockTransport 不经过 httpcore 超时层，直接抛 TimeoutException 模拟
    transport = _capture_transport(
        [], lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=r)))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")
    assert excinfo.value.reason == "timeout"


@pytest.mark.anyio
async def test_unparseable_content_raises_parse(monkeypatch):
    _configure_llm(monkeypatch)
    transport = _capture_transport(
        [], lambda r: httpx.Response(200, json=_chat_body("抱歉，我无法以 JSON 输出。")))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")
    assert excinfo.value.reason == "parse"


@pytest.mark.anyio
async def test_call_llm_json_both_protocols(monkeypatch):
    _configure_llm(monkeypatch)
    # chat + ```json 围栏
    captured: list = []
    transport = _capture_transport(
        captured, lambda r: httpx.Response(
            200, json=_chat_body('```json\n{"preference": ["a"], "new": [], "explore": []}\n```')))
    async with httpx.AsyncClient(transport=transport) as client:
        data = await dailyrec.call_llm_json(client, "p")
    assert data == {"preference": ["a"], "new": [], "explore": []}
    payload = json.loads(captured[0].content)
    assert payload["temperature"] == 0.2
    # responses + output_text 直出键（部分网关的平铺形态）
    monkeypatch.setenv("FNMUSIC_LLM_PROTOCOL", "responses")
    body = {"status": "completed", "output_text": '["x","y"]',
            "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}}
    transport = _capture_transport(captured, lambda r: httpx.Response(200, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        data = await dailyrec.call_llm_json(client, "p")
    assert data == ["x", "y"]
    # 非法 JSON → parse 失败（不再静默 None）
    transport = _capture_transport(
        captured, lambda r: httpx.Response(200, json=_responses_body("not json")))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm_json(client, "p")
    assert excinfo.value.reason == "parse"


# ---------------------------------------------------------------- token 限额 --

def test_usage_state_path_webui_repo_dir_fallback(tmp_path, monkeypatch):
    """issue #85：容器内 webui 没有 FNMUSIC_HOME，靠 WEBUI_REPO_DIR 对齐
    宿主 proxy 写在仓库根的账本；FNMUSIC_HOME 仍优先。"""
    monkeypatch.delenv("FNMUSIC_LLM_USAGE_FILE", raising=False)
    monkeypatch.delenv("FNMUSIC_HOME", raising=False)
    monkeypatch.setenv("WEBUI_REPO_DIR", str(tmp_path))
    assert llm_usage._state_path() == str(tmp_path / "llm_usage.json")
    monkeypatch.setenv("FNMUSIC_HOME", "/custom/home")
    assert os.path.normpath(llm_usage._state_path()) == os.path.normpath("/custom/home/llm_usage.json")


def test_usage_file_world_readable_under_strict_umask(tmp_path, monkeypatch):
    """issue #85：宿主 proxy 在 UMask=0077 下落盘，账本必须显式 0644，
    容器 webui（uid=1000）才读得到；内容无密钥，放宽是安全的。"""
    usage_file = tmp_path / "llm_usage.json"
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))
    monkeypatch.setattr(llm_usage, "_STATE", None)
    monkeypatch.setattr(llm_usage, "_STATE_DAY", "")
    old_umask = os.umask(0o077)
    try:
        llm_usage.record({"prompt": 1, "completion": 2, "total": 3})
    finally:
        os.umask(old_umask)
    if os.name != "nt":
        assert stat.S_IMODE(usage_file.stat().st_mode) == 0o644
    data = json.loads(usage_file.read_text(encoding="utf-8"))
    assert data["days"][llm_usage._today()]["total"] == 3


def test_getters_bounds(monkeypatch):
    monkeypatch.setenv("FNMUSIC_LLM_PROTOCOL", "RESPONSES")
    assert dailyrec.llm_protocol() == "responses"
    monkeypatch.setenv("FNMUSIC_LLM_PROTOCOL", "bogus")
    assert dailyrec.llm_protocol() == "chat"
    monkeypatch.setenv("FNMUSIC_LLM_MAX_TOKENS", "1")
    assert dailyrec.llm_max_tokens() == 256
    monkeypatch.setenv("FNMUSIC_LLM_MAX_TOKENS", "99999999")
    assert dailyrec.llm_max_tokens() == 131072
    monkeypatch.setenv("FNMUSIC_LLM_MAX_TOKENS", "junk")
    assert dailyrec.llm_max_tokens() == 8192
    monkeypatch.delenv("FNMUSIC_LLM_MAX_TOKENS", raising=False)
    assert dailyrec.llm_max_tokens() == 8192
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "-5")
    assert dailyrec.llm_daily_token_limit() == 0
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "junk")
    assert dailyrec.llm_daily_token_limit() == 1_000_000
    monkeypatch.delenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", raising=False)
    assert dailyrec.llm_daily_token_limit() == 1_000_000


def test_usage_record_cross_day_reset_and_limit(tmp_path, monkeypatch):
    usage_file = tmp_path / "llm_usage.json"
    yesterday = time.strftime("%Y%m%d", time.localtime(time.time() - 86400))
    usage_file.write_text(json.dumps({
        "date": yesterday, "prompt": 900000, "completion": 100000,
        "total": 1000000, "requests": 9, "lastFailure": {"reason": "http-429"},
    }), encoding="utf-8")
    monkeypatch.setenv("FNMUSIC_LLM_USAGE_FILE", str(usage_file))
    monkeypatch.setattr(llm_usage, "_STATE", None)
    monkeypatch.setattr(llm_usage, "_STATE_DAY", "")
    # 跨日自动清零：昨天的 100 万不拦今天
    assert llm_usage.within_limit(1_000_000) is True
    llm_usage.record({"prompt": 600000, "completion": 400000, "total": 1000000})
    assert llm_usage.within_limit(1_000_000) is False
    assert llm_usage.within_limit(0) is True  # 0=不限制
    summary = llm_usage.summary()
    assert summary["total"] == 1_000_000 and summary["requests"] == 1
    assert summary["dailyLimit"] == 1_000_000 and summary["withinLimit"] is False


@pytest.mark.anyio
async def test_daily_limit_blocks_request_before_http(monkeypatch):
    _configure_llm(monkeypatch)
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "100")
    llm_usage.record({"prompt": 60, "completion": 40, "total": 100})
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(200, json=_chat_body(json.dumps(_CANDIDATES)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")
    assert excinfo.value.reason == "token-limit"
    assert hits["n"] == 0  # 请求前即拦截，不打 API
    assert llm_usage.used_today()["lastFailure"]["reason"] == "token-limit"


@pytest.mark.anyio
async def test_daily_build_records_llm_failure_and_keeps_local(monkeypatch):
    """#83 场景端到端：LLM 正文为空 → tierFailures 记 llm:empty-content，
    每日推荐不再静默（本地兜底照常兜底）。"""
    from proxy.app import build_online_track

    monkeypatch.setenv("FNMUSIC_MUSIC_DB", "/nonexistent.db")
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", "true")
    usage = {"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106,
             "completion_tokens_details": {"reasoning_tokens": 4096}}
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=_chat_body("", finish="length", usage=usage)))
    async with httpx.AsyncClient(transport=transport) as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                              base_url="http://127.0.0.1:8768") as mdl, \
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                              base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-issue83",
            musicdl_client=mdl,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=False,
        )
    assert "llm:empty-content" in payload["tierFailures"]
    assert "llm" not in payload["tiers"]


@pytest.mark.anyio
async def test_daily_build_records_token_limit_failure(monkeypatch):
    from proxy.app import build_online_track

    monkeypatch.setenv("FNMUSIC_MUSIC_DB", "/nonexistent.db")
    monkeypatch.setenv("FNMUSIC_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("FNMUSIC_LLM_API_KEY", "test-key")
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_LLM_DAILY_TOKEN_LIMIT", "50")
    llm_usage.record({"prompt": 20, "completion": 30, "total": 50})
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(200, json=_chat_body(json.dumps(_CANDIDATES)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as llm_client, \
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                              base_url="http://127.0.0.1:8768") as mdl, \
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                              base_url="http://127.0.0.1:8770") as mb:
        payload = await dailyrec.get_or_build_daily(
            user_guid="u-token-limit",
            musicdl_client=mdl,
            musicbox_client=mb,
            llm_http=llm_client,
            build_track=build_online_track,
            netease_enabled=False,
        )
    assert hits["n"] == 0
    assert "llm:token-limit" in payload["tierFailures"]


# ------------------------------------------- 未知用量归因与 no_provider 冷却 --

@pytest.mark.anyio
async def test_http_error_with_usage_is_recorded_not_unknown(monkeypatch):
    """错误响应也可能带 usage：有就如实记入，只有拿不到才计入未知。"""
    _configure_llm(monkeypatch)
    body = {"error": {"message": "rate limited", "code": "rate_limited"},
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}
    transport = httpx.MockTransport(lambda r: httpx.Response(429, json=body))

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            await dailyrec.call_llm(client, "p")

    assert excinfo.value.reason == "http-429"
    today = llm_usage.used_today()
    assert today["requests"] == 1
    assert today["unknownUsageRequests"] == 0
    assert (today["prompt"], today["completion"], today["total"]) == (7, 3, 10)
    assert today["lastFailure"]["code"] == "rate_limited"


@pytest.mark.anyio
async def test_no_provider_503_records_cause_and_cools_down(monkeypatch):
    """503 + no_provider：记账一次未知后短时抑制同配置重试，配置变化立即放行。"""
    _configure_llm(monkeypatch)
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(503, json={"error": {
            "message": "模型 test-model 的提供者均不可用（上游来源模型清单不含该模型）",
            "code": "no_provider"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(dailyrec.LLMFailure) as first:
            await dailyrec.call_llm(client, "p")
        assert first.value.reason == "http-503"
        assert first.value.code == "no_provider"
        today = llm_usage.used_today()
        assert today["requests"] == 1
        assert today["unknownUsageRequests"] == 1
        assert today["unknownUsageByCause"]["httpError"] == 1
        assert today["lastFailure"]["code"] == "no_provider"

        # 冷却期内：不发请求、不增加请求计数，也不覆盖最近失败
        with pytest.raises(dailyrec.LLMFailure) as second:
            await dailyrec.call_llm(client, "p")
        assert second.value.reason == "no-provider"
        assert hits["n"] == 1
        assert llm_usage.used_today()["requests"] == 1
        assert llm_usage.used_today()["lastFailure"]["code"] == "no_provider"

        # 换成另一个模型 → 新配置键 → 立即允许再次尝试
        monkeypatch.setenv("FNMUSIC_LLM_MODEL", "another-model")
        with pytest.raises(dailyrec.LLMFailure) as third:
            await dailyrec.call_llm(client, "p")
        assert third.value.reason == "http-503"
        assert hits["n"] == 2


@pytest.mark.anyio
async def test_other_503_codes_do_not_cool_down(monkeypatch):
    """只有 no_provider 触发抑制；其他 503 仍按原逻辑逐次尝试。"""
    _configure_llm(monkeypatch)
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(503, json={"error": {"code": "overloaded"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(dailyrec.LLMFailure):
                await dailyrec.call_llm(client, "p")

    assert hits["n"] == 2
    assert llm_usage.used_today()["unknownUsageByCause"]["httpError"] == 2


def test_no_provider_cooldown_expiry_and_key(monkeypatch):
    _configure_llm(monkeypatch)
    key = dailyrec._no_provider_key()
    assert dailyrec.no_provider_cooldown_remaining(key) == 0.0

    dailyrec._mark_no_provider(key)
    assert dailyrec.no_provider_cooldown_remaining(key) > 0

    monkeypatch.setenv("FNMUSIC_LLM_MODEL", "other-model")
    assert dailyrec._no_provider_key() != key, "模型变化必须产生新配置键"

    dailyrec._NO_PROVIDER_UNTIL[key] = time.monotonic() - 1
    assert dailyrec.no_provider_cooldown_remaining(key) == 0.0
    assert key not in dailyrec._NO_PROVIDER_UNTIL


@pytest.mark.anyio
@pytest.mark.parametrize("usage", [
    {"prompt_tokens": "abc", "completion_tokens": 3},
    {"prompt_tokens": "²", "completion_tokens": 3},
    {"prompt_tokens": "9" * 5000, "completion_tokens": 3},
    {"prompt_tokens": -5, "completion_tokens": 3, "total_tokens": "x"},
    {"prompt_tokens": True, "completion_tokens": 3},
    {"total_tokens": 20},
    {"foo": 1},
])
async def test_invalid_usage_fields_never_drop_the_request(monkeypatch, usage):
    """字段异常只让用量变成“不完整”，绝不抛异常、也绝不漏记这次请求。"""
    _configure_llm(monkeypatch)
    content = json.dumps(_CANDIDATES, ensure_ascii=False)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage}))

    async with httpx.AsyncClient(transport=transport) as client:
        recs = await dailyrec.call_llm(client, "p")

    assert len(recs) == len(_CANDIDATES)
    today = llm_usage.used_today()
    assert today["requests"] == 1
    assert today["unknownUsageRequests"] == 1
    assert today["unknownUsageByCause"]["missingUsage"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("choices", [{"unexpected": "mapping"}, [{"message": ["not a dict"]}]])
async def test_malformed_chat_structure_still_records_usage(monkeypatch, choices):
    """异常正文字段不能使整次请求或可证实的用量从账本消失。"""
    _configure_llm(monkeypatch)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": choices,
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }))

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure):
            await dailyrec.call_llm(client, "p")

    today = llm_usage.used_today()
    assert today["requests"] == 1
    assert today["total"] == 10
    assert today["unknownUsageRequests"] == 0


@pytest.mark.anyio
async def test_malformed_chat_without_usage_is_classified_invalid(monkeypatch):
    _configure_llm(monkeypatch)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": ["not a dict"]}],
    }))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure):
            await dailyrec.call_llm(client, "p")
    today = llm_usage.used_today()
    assert today["requests"] == today["unknownUsageRequests"] == 1
    assert today["unknownUsageByCause"]["invalidResponse"] == 1


@pytest.mark.anyio
async def test_explicit_zero_usage_is_not_unknown(monkeypatch):
    """合法的显式 0 是已知用量，不能被当成“未返回用量”。"""
    _configure_llm(monkeypatch)
    content = json.dumps(_CANDIDATES, ensure_ascii=False)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}))

    async with httpx.AsyncClient(transport=transport) as client:
        await dailyrec.call_llm(client, "p")

    today = llm_usage.used_today()
    assert today["requests"] == 1
    assert today["unknownUsageRequests"] == 0


@pytest.mark.anyio
async def test_cancelled_request_is_recorded_then_reraised(monkeypatch):
    """请求途中被取消：记一次实际尝试后原样抛出，取消绝不被吞掉。"""
    _configure_llm(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await dailyrec.call_llm(client, "p")

    today = llm_usage.used_today()
    assert today["requests"] == 1
    assert today["unknownUsageByCause"]["transportError"] == 1
    assert today["lastFailure"] is None, "取消不是模型失败，不应覆盖最近失败"


@pytest.mark.anyio
@pytest.mark.parametrize("parse_json", [False, True])
async def test_parse_failure_updates_reason_without_double_counting(monkeypatch, parse_json):
    """正文已记账后解析失败：只补失败原因，不重复计请求与 token。"""
    _configure_llm(monkeypatch)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=_chat_body(
        "not json at all",
        usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16})))

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dailyrec.LLMFailure) as excinfo:
            if parse_json:
                await dailyrec.call_llm_json(client, "p")
            else:
                await dailyrec.call_llm(client, "p")

    assert excinfo.value.reason == "parse"
    today = llm_usage.used_today()
    assert today["requests"] == 1
    assert today["total"] == 16
    assert today["unknownUsageRequests"] == 0
    assert today["lastFailure"]["reason"] == "parse"
