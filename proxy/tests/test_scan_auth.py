"""Administrator scan login stays in memory and rejects non-admin accounts."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from proxy.scan_auth import ScanAdminSession, ScanAuthError, authx_header


def test_authx_header_names_only():
    header = authx_header("/music/api/v1/shared-library/scan", "{}")
    assert header.startswith("nonce=")
    assert "timestamp=" in header and "sign=" in header


def _client(role: str, calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/password-login"):
            assert b"secret-value" in request.content
            return httpx.Response(200, json={"code": 0, "data": {"userToken": "memory-token"}})
        if request.url.path.endswith("/user/me"):
            return httpx.Response(200, json={"code": 0, "data": {"user": {"role": role}}})
        return httpx.Response(404, json={"code": 404})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:5666")


def test_admin_session_caches_token_and_hides_it_from_errors():
    calls: list[str] = []
    session = ScanAdminSession(_client("admin", calls))
    conf = {"scan_admin_username": "admin", "scan_admin_password": "secret-value", "scan_admin_device_id": ""}

    async def run():
        first = await session.headers(conf)
        second = await session.headers(conf)
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert first["cookie"] == "music-token=memory-token"
    assert calls.count("/music/api/v1/user/password-login") == 1
    assert "secret-value" not in str(first)


def test_non_admin_cannot_scan():
    session = ScanAdminSession(_client("user", []))
    conf = {"scan_admin_username": "plain", "scan_admin_password": "secret-value", "scan_admin_device_id": "device"}
    with pytest.raises(ScanAuthError) as caught:
        asyncio.run(session.verify(conf))
    assert caught.value.code == "not_admin"
    assert "secret-value" not in str(caught.value)
