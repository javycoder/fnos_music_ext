"""Issue #126: distinguish host takeover, upstream failure and search cancellation offline."""
import asyncio
import json
import os

import httpx
import pytest
from starlette.requests import Request

from proxy import app as proxy_app
from proxy import maintenance_socket


@pytest.mark.parametrize("target_pid,upstream_status,expected", [
    (None, 401, "ok"), (1, 401, "mismatch"), (None, 503, "ok"),
])
def test_host_diagnostics_checks_actual_entry_and_official_upstream(
        monkeypatch, target_pid, upstream_status, expected):
    calls = []

    def target(request):
        calls.append(("target", request.url.path))
        return httpx.Response(200, json={
            "service": "fnmusic-ext", "pid": os.getpid() if target_pid is None else target_pid})

    def upstream(request):
        calls.append(("upstream", request.url.path))
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(upstream_status)

    monkeypatch.setattr(proxy_app.httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(target))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
                                     base_url="http://unix") as client:
            monkeypatch.setattr(proxy_app, "get_upstream_client", lambda app: client)
            status, report = await proxy_app._maintenance_request("GET", maintenance_socket.DIAGNOSTICS_PATH, {})
            assert status == 200 and report["ok"] is True
            assert report["takeover"]["status"] == expected
            assert report["upstream"]["status"] == ("fail" if upstream_status >= 500 else "ok")
            assert report["pid"] == os.getpid()
            assert len(calls) == 2

    asyncio.run(exercise())


def test_host_diagnostics_unreachable_does_not_expose_exception_detail(monkeypatch):
    def fail(request):
        raise httpx.ConnectError("token=private-diagnostic-value", request=request)

    monkeypatch.setattr(proxy_app.httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(fail))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail),
                                     base_url="http://unix") as client:
            monkeypatch.setattr(proxy_app, "get_upstream_client", lambda app: client)
            report = await proxy_app._proxy_diagnostic_status()
            assert report["takeover"] == {"status": "unreachable", "error": "ConnectError"}
            assert report["upstream"] == {"status": "unreachable", "error": "ConnectError"}
            assert "private-diagnostic-value" not in json.dumps(report)

    asyncio.run(exercise())


def test_diagnostics_private_transport_accepts_only_exact_read_route():
    path = maintenance_socket.DIAGNOSTICS_PATH
    assert maintenance_socket._route("GET", path)
    assert not maintenance_socket._route("POST", path)
    assert not maintenance_socket._route("GET", path + "?path=/etc/passwd")
    assert not maintenance_socket._route("GET", path + "/extra")


def test_cancelled_search_logs_final_outcome_and_resets_context(caplog):
    request = Request({"type": "http", "method": "GET", "path": "/music/api/v1/search/track",
                       "query_string": b"keyword=private-query&token=private-token", "headers": []})

    async def cancelled(request):
        raise asyncio.CancelledError

    async def exercise():
        assert proxy_app._SEARCH_DIAG_ID.get() == "-"
        with pytest.raises(asyncio.CancelledError):
            await proxy_app.log_client_requests(request, cancelled)
        assert proxy_app._SEARCH_DIAG_ID.get() == "-"

    caplog.set_level("INFO", logger="fnmusic_proxy")
    asyncio.run(exercise())
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "search_received" in messages and "outcome=cancelled" in messages
    assert "private-query" not in messages and "private-token" not in messages
