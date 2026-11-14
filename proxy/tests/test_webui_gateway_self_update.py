from __future__ import annotations

import http.client
import json
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest

from proxy.webui_gateway import _self_update_target, handle_self_update
from proxy.self_update import TargetSnapshot, UpdateJob


def test_self_update_target_parsing():
    assert _self_update_target(b"GET /api/self-update HTTP/1.1\r\n") == ("GET", "/api/self-update")
    assert _self_update_target(b"GET /app/fnmusic-ext/api/self-update HTTP/1.1\r\n") == ("GET", "/api/self-update")
    assert _self_update_target(b"GET /api/self-update/target?tag=v2.12.0 HTTP/1.1\r\n") == ("GET", "/api/self-update/target?tag=v2.12.0")
    assert _self_update_target(b"POST /api/self-update HTTP/1.1\r\n") == ("POST", "/api/self-update")
    assert _self_update_target(b"GET /api/about HTTP/1.1\r\n") is None


def _call_gateway_handler(head_raw: bytes, method: str, target: str) -> tuple[int, dict]:
    client_sock, server_sock = socket.socketpair()
    t = threading.Thread(target=handle_self_update, args=(server_sock, head_raw, method, target), daemon=True)
    t.start()

    resp = http.client.HTTPResponse(client_sock)
    resp.begin()
    status = resp.status
    data = resp.read()
    t.join(timeout=2.0)
    client_sock.close()
    return status, json.loads(data)


def test_handle_self_update_requires_admin():
    head_raw = b"GET /api/self-update HTTP/1.1\r\nHost: localhost\r\n\r\n"
    status, body = _call_gateway_handler(head_raw, "GET", "/api/self-update")
    assert status == 403
    assert "仅管理员可执行更新" in body["error"]


def test_handle_self_update_get_status():
    head_raw = b"GET /api/self-update HTTP/1.1\r\nHost: localhost\r\nX-Trim-Isadmin: true\r\n\r\n"
    with patch("proxy.webui_gateway.get_active_job", return_value=None), \
         patch("proxy.webui_gateway.get_most_recent_job", return_value=None):
        status, body = _call_gateway_handler(head_raw, "GET", "/api/self-update")
        assert status == 200
        assert body["ok"] is True
        assert "capability" in body
        assert body["job"] is None


def test_handle_self_update_get_target_valid_and_invalid():
    head_invalid = b"GET /api/self-update/target?tag=bad tag HTTP/1.1\r\nHost: localhost\r\nX-Trim-Isadmin: true\r\n\r\n"
    status, body = _call_gateway_handler(head_invalid, "GET", "/api/self-update/target?tag=bad tag")
    assert status == 400

    head_valid = b"GET /api/self-update/target?tag=v2.12.0 HTTP/1.1\r\nHost: localhost\r\nX-Trim-Isadmin: true\r\n\r\n"
    mock_snap = TargetSnapshot(
        target_id="0123456789abcdef0123456789abcdef",
        tag="v2.12.0",
        formal_base="2.12.0",
        install_version="2.12.0-0",
        architecture="amd64",
        asset_name="fnmusic-ext-2.12.0-0.x86_64.fpk",
        download_url="https://example.com/fpk",
        sha256="abc",
        sha256_url="https://example.com/sha",
        source="gitee",
        size=1024,
        candidates=[],
        created_at=100.0,
        expires_at=700.0,
    )
    with patch("proxy.webui_gateway.resolve_target", return_value=mock_snap):
        status, body = _call_gateway_handler(head_valid, "GET", "/api/self-update/target?tag=v2.12.0")
        assert status == 200
        assert body["ok"] is True
        assert body["target"]["target_id"] == "0123456789abcdef0123456789abcdef"
    with patch("proxy.webui_gateway.resolve_target", return_value=None):
        status, body = _call_gateway_handler(head_valid, "GET", "/api/self-update/target?tag=v2.12.0")
        assert status == 404
        assert body["ok"] is False


def test_handle_self_update_post_validation_and_dispatch():
    payload_bad = json.dumps({"target_id": "short"}).encode()
    head_bad = (
        b"POST /api/self-update HTTP/1.1\r\nHost: localhost\r\nX-Trim-Isadmin: true\r\n"
        b"Content-Length: " + str(len(payload_bad)).encode() + b"\r\n\r\n" + payload_bad
    )
    status, body = _call_gateway_handler(head_bad, "POST", "/api/self-update")
    assert status == 400

    payload_good = json.dumps({"target_id": "0123456789abcdef0123456789abcdef", "confirm": True}).encode()
    head_good = (
        b"POST /api/self-update HTTP/1.1\r\nHost: localhost\r\nX-Trim-Isadmin: true\r\n"
        b"Content-Length: " + str(len(payload_good)).encode() + b"\r\n\r\n" + payload_good
    )
    with patch("proxy.webui_gateway.start_update_job", return_value=(202, {"ok": True, "job_id": "job123"})):
        status, body = _call_gateway_handler(head_good, "POST", "/api/self-update")
        assert status == 202
        assert body["ok"] is True
        assert body["job_id"] == "job123"


def test_handle_self_update_job_status_and_unknown_path():
    head_job = b"GET /api/self-update/jobs/0123456789abcdef0123456789abcdef HTTP/1.1\r\nHost: localhost\r\nX-Trim-Isadmin: true\r\n\r\n"
    with patch("proxy.webui_gateway.get_job", return_value=None):
        status, body = _call_gateway_handler(head_job, "GET", "/api/self-update/jobs/0123456789abcdef0123456789abcdef")
        assert status == 404
        assert body["error"] == "任务不存在"

    head_unknown = b"GET /api/self-update/not-exist HTTP/1.1\r\nHost: localhost\r\nX-Trim-Isadmin: true\r\n\r\n"
    status, body = _call_gateway_handler(head_unknown, "GET", "/api/self-update/not-exist")
    assert status == 404
    assert body["error"] == "未知自更新接口"
