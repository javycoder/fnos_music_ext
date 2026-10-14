"""nginx reverse-proxy behavior tests for issue #49.

The extension sits behind the fnOS nginx gateway. Even when the extension sends
X-Accel-Buffering, nginx consumes that control header and does not pass it to a
second, user-owned proxy. These tests lock that behavior and verify the required
outer proxy_buffering setting with a controlled streaming origin.
"""
import http.client
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _nginx_binary():
    return shutil.which("nginx")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    records = []
    client_read_first = threading.Event()
    client_saw_first = False

    def do_GET(self):
        self.__class__.records.append({
            "path": self.path,
            "range": self.headers.get("Range"),
            "authorization": self.headers.get("Authorization"),
            "cookie": self.headers.get("Cookie"),
            "accel": self.headers.get("X-Accel-Buffering"),
        })
        body = b"stream"
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(body[:3])
        self.wfile.flush()
        if self.__class__.client_read_first.wait(0.75):
            self.__class__.client_saw_first = True
        self.wfile.write(body[3:])
        self.wfile.flush()

    def log_message(self, fmt, *args):
        return


@pytest.mark.skipif(_nginx_binary() is None, reason="nginx is unavailable")
def test_two_layer_proxy_requires_outer_buffering_off(tmp_path):
    nginx = _nginx_binary()
    origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    inner_port, buffered_port, streaming_port = _free_port(), _free_port(), _free_port()

    config = f"""
worker_processes 1;
error_log {tmp_path / 'error.log'} warn;
pid {tmp_path / 'nginx.pid'};
events {{ worker_connections 64; }}
http {{
    access_log off;
    proxy_temp_path {tmp_path / 'proxy'};
    server {{
        listen 127.0.0.1:{inner_port};
        location / {{
            proxy_pass http://127.0.0.1:{origin.server_port};
            proxy_max_temp_file_size 0;
        }}
    }}
    server {{
        listen 127.0.0.1:{buffered_port};
        location / {{
            proxy_pass http://127.0.0.1:{inner_port};
        }}
    }}
    server {{
        listen 127.0.0.1:{streaming_port};
        location / {{
            proxy_pass http://127.0.0.1:{inner_port};
            proxy_buffering off;
            proxy_set_header Range $http_range;
            proxy_set_header Authorization $http_authorization;
            proxy_set_header Cookie $http_cookie;
        }}
    }}
}}
"""
    (tmp_path / "nginx.conf").write_text(config, encoding="utf-8")
    (tmp_path / "proxy").mkdir()
    process = subprocess.Popen(
        [nginx, "-p", str(tmp_path) + os.sep, "-c", str(tmp_path / "nginx.conf"), "-g", "daemon off;"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.stderr.read())
            try:
                with socket.create_connection(("127.0.0.1", streaming_port), timeout=0.05):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            error_log = ""
            if (tmp_path / "error.log").exists():
                error_log = (tmp_path / "error.log").read_text(encoding="utf-8", errors="replace")
            error_log += process.stderr.read() if process.poll() is not None else ""
            raise AssertionError(f"nginx did not start: {error_log[-2000:]}")

        request_headers = {
            "Range": "bytes=0-",
            "Authorization": "Bearer test",
            "Cookie": "session=test",
        }
        conn = http.client.HTTPConnection("127.0.0.1", buffered_port, timeout=3)
        conn.request("GET", "/music/api/v1/track/stream?guid=online:test:1", headers=request_headers)
        response = conn.getresponse()
        response.read()
        assert response.status == 200
        assert response.getheader("X-Accel-Buffering") is None
        conn.close()

        OriginHandler.client_read_first.clear()
        conn = http.client.HTTPConnection("127.0.0.1", streaming_port, timeout=3)
        conn.request("GET", "/music/api/v1/track/stream?guid=online:test:1", headers=request_headers)
        response = conn.getresponse()
        assert response.read(3) == b"str"
        OriginHandler.client_read_first.set()
        assert response.read() == b"eam"
        assert response.status == 200
        assert response.getheader("X-Accel-Buffering") is None
        conn.close()

        assert OriginHandler.client_saw_first is True
        assert OriginHandler.records[-1]["path"] == "/music/api/v1/track/stream?guid=online:test:1"
        assert OriginHandler.records[-1]["range"] == "bytes=0-"
        assert OriginHandler.records[-1]["authorization"] == "Bearer test"
        assert OriginHandler.records[-1]["cookie"] == "session=test"
        assert OriginHandler.records[-1]["accel"] is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        origin.shutdown()
        origin.server_close()
        origin_thread.join(timeout=2)
