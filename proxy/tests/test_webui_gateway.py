"""桌面网关 socket 把 HTTP 原样转到 WebUI 端口。"""
from __future__ import annotations

import importlib.util
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


def test_socket_path_for_fpk_layout(tmp_path: Path):
    app = tmp_path / "fnmusic-ext"
    (app / "ui").mkdir(parents=True)
    repo = app / "repo"
    repo.mkdir()
    assert socket_path_for(repo) == app / "fnmusic-ext.sock"


def test_socket_forwards_desktop_path(tmp_path: Path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
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
    client.sendall(b"GET /app/fnmusic-ext/ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    data = b""
    while True:
        chunk = client.recv(4096)
        if not chunk:
            break
        data += chunk
    client.close()
    upstream.shutdown()
    assert b"/app/fnmusic-ext/" in data
