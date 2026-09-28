"""桌面网关 socket 把 HTTP 原样转到 WebUI 端口；/api/host-file 由网关本地处理。"""
from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "webui_gateway", Path(__file__).resolve().parents[1] / "webui_gateway.py")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
serve = _MOD.serve
socket_path_for = _MOD.socket_path_for


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _start_gateway(tmp_path: Path, port: int) -> Path:
    sock_path = tmp_path / "fnmusic-ext.sock"
    errors = []

    def _run():
        try:
            serve(sock_path, ("127.0.0.1", port))
        except Exception as exc:  # noqa: BLE001 — 断言里带出线程失败原因
            errors.append(exc)

    threading.Thread(target=_run, daemon=True).start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    last = None
    for _ in range(50):
        try:
            client.connect(str(sock_path))
            break
        except OSError as exc:
            last = exc
            time.sleep(0.02)
    else:
        raise AssertionError(f"socket 未就绪: {last}; thread={errors}")
    client.close()
    return sock_path


def _request(sock_path: Path, raw: bytes, timeout: float = 5) -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(sock_path))
    client.sendall(raw)
    data = b""
    client.settimeout(timeout)
    try:
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    finally:
        client.close()
    return data


def _post_host_file(path: str, is_admin: str = "true") -> bytes:
    body = json.dumps({"path": path}).encode()
    return (
        "POST /app/fnmusic-ext/api/host-file HTTP/1.1\r\n"
        "Host: localhost\r\n"
        f"X-Trim-Isadmin: {is_admin}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode() + body


def test_socket_path_for_fpk_layout(tmp_path: Path):
    app = tmp_path / "fnmusic-ext"
    (app / "ui").mkdir(parents=True)
    repo = app / "repo"
    repo.mkdir()
    assert socket_path_for(repo) == app / "fnmusic-ext.sock"


class _SlowHandler(BaseHTTPRequestHandler):
    """响应故意晚于旧的 5 秒套接字超时，用来守住校验请求不被中途拆掉。"""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        time.sleep(6)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def test_slow_upstream_is_not_cut_at_connect_timeout(tmp_path: Path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    sock_path = _start_gateway(tmp_path, port)
    raw = (
        b"POST /app/fnmusic-ext/api/lx/verify HTTP/1.1\r\n"
        b"Host: localhost\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
    )
    data = _request(sock_path, raw, timeout=12)
    upstream.shutdown()
    assert b"200" in data.split(b"\r\n", 1)[0]
    assert b'{"ok":true}' in data


def test_socket_forwards_desktop_path(tmp_path: Path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    sock_path = _start_gateway(tmp_path, port)
    data = _request(sock_path, b"GET /app/fnmusic-ext/ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    upstream.shutdown()
    assert b"/app/fnmusic-ext/" in data


def test_host_file_requires_admin(tmp_path: Path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    sock_path = _start_gateway(tmp_path, port)
    data = _request(sock_path, _post_host_file("/tmp/x.js", is_admin="false"))
    upstream.shutdown()
    assert b"403" in data.split(b"\r\n", 1)[0]
    assert "仅管理员".encode() in data


def test_host_file_validates_path_and_reads_content(tmp_path: Path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    sock_path = _start_gateway(tmp_path, port)

    script = "/*\n * @name 测试\n */\nvar a = 1;\n"
    js = tmp_path / "source.js"
    js.write_text(script, encoding="utf-8")

    # 非法：相对路径 / .. / 非 .js / 不存在
    for bad in ("relative/x.js", "/tmp/../etc/passwd", str(tmp_path / "a.txt"), str(tmp_path / "nope.js")):
        data = _request(sock_path, _post_host_file(bad))
        assert b"400" in data.split(b"\r\n", 1)[0] or b"404" in data.split(b"\r\n", 1)[0], bad

    # 正常读取
    data = _request(sock_path, _post_host_file(str(js)))
    head, _, body = data.partition(b"\r\n\r\n")
    assert b"200" in head.split(b"\r\n", 1)[0]
    payload = json.loads(body.decode())
    assert payload["ok"] is True
    assert payload["script"] == script
    upstream.shutdown()


def test_host_file_only_post_intercepted(tmp_path: Path):
    """GET /api/host-file 不是代读端点，走正常转发。"""
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    sock_path = _start_gateway(tmp_path, port)
    data = _request(sock_path, b"GET /app/fnmusic-ext/api/host-file HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    upstream.shutdown()
    # 上游原样回显请求路径（未拦截）
    assert b"/app/fnmusic-ext/api/host-file" in data
