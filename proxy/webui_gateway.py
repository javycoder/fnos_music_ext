#!/usr/bin/env python3
"""把飞牛桌面网关的 Unix socket 原样转到本机 WebUI。

应用中心按 ui/config 的 gatewaySocket 连接
<应用目录>/fnmusic-ext.sock，把 https://<主机>:<桌面端口>/app/fnmusic-ext/
转到这个 socket。WebUI 自己监听 127.0.0.1:8774，并认识 /app/fnmusic-ext 前缀。

本模块还处理两个例外端点（都只在桌面网关链路里存在，直连 8774 不经过这里，
都要求网关注入的 X-Trim-Isadmin: true；其余请求一律原样转发，不动字节）：
- POST /app/fnmusic-ext/api/host-file——浏览器把 NAS 上选中的 .js 源脚本路径
  发来，由宿主侧（本进程，root）代读文件内容。
- POST /app/fnmusic-ext/api/fs-check——浏览器把储存目录（缓存/下载目录）路径
  发来，由宿主侧按真实落盘视角（root proxy）探测目录存在性与可写性。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import uuid
from pathlib import Path

UPSTREAM = ("127.0.0.1", 8774)
SOCK_NAME = "fnmusic-ext.sock"
PID_FILE = Path("/run/fnmusic-ext/webui-gateway.pid")
# create_connection 的 timeout 会留在套接字上，之后的 recv 也受它限制。
# 洛雪源校验要几十秒才有响应；沿用 5 秒会让桌面网关中途拆连接，nginx 回 502。
CONNECT_TIMEOUT = 5
RELAY_TIMEOUT = 180

HOST_FILE_PATHS = ("/app/fnmusic-ext/api/host-file", "/api/host-file")
HOST_FILE_MAX_BODY = 1 << 20          # 请求体上限 1MB（只装一个路径字符串）
HOST_FILE_MAX_BYTES = 9_000_000       # 与 lxmusic SCRIPT_MAX_BYTES 对齐
FS_CHECK_PATHS = ("/app/fnmusic-ext/api/fs-check", "/api/fs-check")
FS_CHECK_MAX_BODY = 1 << 20           # 同 host-file：只装一个路径字符串


def socket_path_for(base: Path) -> Path:
    """fpk 布局是 <应用目录>/repo。socket 必须放在应用目录下，桌面网关才找得到。"""
    parent = base.resolve().parent
    if (parent / "ui").is_dir() and base.name == "repo":
        return parent / SOCK_NAME
    run = Path("/run/fnmusic-ext")
    run.mkdir(parents=True, exist_ok=True)
    return run / SOCK_NAME


def _relay(src: socket.socket, dst: socket.socket, prefix: bytes = b"") -> None:
    try:
        if prefix:
            dst.sendall(prefix)
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _read_request_head(client: socket.socket) -> "tuple[bytes, dict[str, str]] | None":
    """读到 HTTP 头部结束；返回 (头部原始字节, 解析出的头)。超时/格式异常返回 None。"""
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            if len(buf) > 65536:
                return None
            chunk = client.recv(8192)
            if not chunk:
                return None
            buf += chunk
    except OSError:
        return None
    head, _, rest = buf.partition(b"\r\n\r\n")
    headers: dict[str, str] = {}
    lines = head.decode("latin-1", "replace").split("\r\n")
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    if rest:
        headers["__rest__"] = rest.decode("latin-1", "replace")
    headers["__raw__"] = head.decode("latin-1", "replace")
    return head + b"\r\n\r\n" + rest, headers


def _read_full_body(client: socket.socket, headers: dict[str, str], head_raw: bytes,
                    max_body: int = HOST_FILE_MAX_BODY) -> "bytes | None":
    """按 Content-Length 收齐 POST 体（小请求：仅路径字符串）。"""
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    if length < 0 or length > max_body:
        return None
    head_end = head_raw.find(b"\r\n\r\n") + 4
    body = head_raw[head_end:]
    while len(body) < length:
        chunk = client.recv(65536)
        if not chunk:
            return None
        body += chunk
    return body[:length]


def _http_response(status: int, reason: str, payload: bytes, content_type: str) -> bytes:
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    return head + payload


def _json_response(status: int, obj: dict) -> bytes:
    return _http_response(status, "OK" if status == 200 else "Error",
                          json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")


def handle_host_file(client: socket.socket, headers: dict[str, str], head_raw: bytes) -> None:
    """POST /api/host-file：校验 admin + .js 路径，读文件内容回 JSON {script}。"""
    try:
        if headers.get("x-trim-isadmin", "").lower() != "true":
            client.sendall(_json_response(403, {"ok": False, "error": "仅管理员可读取主机文件"}))
            return
        body = _read_full_body(client, headers, head_raw)
        if body is None:
            client.sendall(_json_response(400, {"ok": False, "error": "请求体无效"}))
            return
        try:
            req = json.loads(body.decode("utf-8"))
            path = str(req.get("path") or "")
        except Exception:  # noqa: BLE001
            client.sendall(_json_response(400, {"ok": False, "error": "请求体必须是 JSON"}))
            return
        target = Path(path)
        if not path.startswith("/") or ".." in target.parts:
            client.sendall(_json_response(400, {"ok": False, "error": "路径必须是绝对路径且不含 .."}))
            return
        if not path.lower().endswith(".js"):
            client.sendall(_json_response(400, {"ok": False, "error": "只支持读取 .js 后缀文件"}))
            return
        try:
            st = target.stat()
        except OSError:
            client.sendall(_json_response(404, {"ok": False, "error": f"文件不存在: {path}"}))
            return
        if not os.path.isfile(str(target)) or st.st_size > HOST_FILE_MAX_BYTES:
            client.sendall(_json_response(400, {"ok": False, "error": "不是常规文件或超过 9MB 上限"}))
            return
        try:
            script = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            client.sendall(_json_response(400, {"ok": False, "error": f"读取失败: {exc}"}))
            return
        client.sendall(_json_response(200, {"ok": True, "script": script}))
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def _probe_dir_writable(path: str) -> bool:
    """写入试探：os.access 对只读挂载等场景可能误报，落一个临时文件再删才算数。"""
    probe = os.path.join(path, f".fnmusic-fscheck-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        fd = os.open(probe, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.close(fd)
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def handle_fs_check(client: socket.socket, headers: dict[str, str], head_raw: bytes) -> None:
    """POST /api/fs-check：校验 admin + 目录路径，按宿主 root 视角（真实落盘方是
    root 跑的 proxy）探测目录存在性与可写性。无副作用：不创建目录，临时探测
    文件即落即删。目录不存在时用最近存在祖先目录的可写性代表"能否创建"。"""
    try:
        if headers.get("x-trim-isadmin", "").lower() != "true":
            client.sendall(_json_response(403, {"ok": False, "error": "仅管理员可校验目录"}))
            return
        body = _read_full_body(client, headers, head_raw, FS_CHECK_MAX_BODY)
        if body is None:
            client.sendall(_json_response(400, {"ok": False, "error": "请求体无效"}))
            return
        try:
            req = json.loads(body.decode("utf-8"))
            path = str(req.get("path") or "")
        except Exception:  # noqa: BLE001
            client.sendall(_json_response(400, {"ok": False, "error": "请求体必须是 JSON"}))
            return
        if any(ch in path for ch in "\r\n\x00"):
            client.sendall(_json_response(400, {"ok": False, "error": "路径不能包含换行或空字符"}))
            return
        target = Path(path)
        if not path.startswith("/") or ".." in target.parts:
            client.sendall(_json_response(400, {"ok": False, "error": "路径必须是绝对路径且不含 .."}))
            return
        exists = writable = False
        ancestor = target
        while True:
            try:
                ancestor.stat()
            except FileNotFoundError:
                # 只能跳过真正缺失的路径；悬空链接本身占位，mkdir 无法替换它。
                if ancestor.is_symlink() or ancestor.parent == ancestor:
                    break
                ancestor = ancestor.parent
                continue
            except OSError:
                # ENOTDIR（文件祖先）、EACCES 等不能当作缺失后继续向上放行。
                break
            if ancestor.is_dir():
                exists = ancestor == target
                writable = _probe_dir_writable(str(ancestor))
            # 首个存在节点不是目录时同样停止，不能跳过普通文件。
            break
        client.sendall(_json_response(200, {
            "ok": True, "path": path, "exists": exists, "writable": writable,
        }))
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def serve(sock_path: Path, upstream: tuple[str, int] = UPSTREAM) -> None:
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    os.chmod(sock_path, 0o666)
    server.listen(64)
    while True:
        client, _addr = server.accept()
        threading.Thread(target=_handle, args=(client, upstream), daemon=True).start()


def _post_request_path(head_raw: bytes) -> "str | None":
    """取请求行的 POST 路径；非 POST 或解析失败返回 None。"""
    try:
        request_line = head_raw.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    except Exception:  # noqa: BLE001
        return None
    parts = request_line.split()
    if len(parts) < 2 or parts[0].upper() != "POST":
        return None
    return parts[1]


def _is_host_file_request(head_raw: bytes) -> bool:
    return _post_request_path(head_raw) in HOST_FILE_PATHS


def _is_fs_check_request(head_raw: bytes) -> bool:
    return _post_request_path(head_raw) in FS_CHECK_PATHS


def _handle(client: socket.socket, upstream: tuple[str, int]) -> None:
    head = _read_request_head(client)
    if head is None:
        client.close()
        return
    head_raw, headers = head
    if _is_host_file_request(head_raw):
        handle_host_file(client, headers, head_raw)
        return
    if _is_fs_check_request(head_raw):
        handle_fs_check(client, headers, head_raw)
        return
    try:
        remote = socket.create_connection(upstream, timeout=CONNECT_TIMEOUT)
        remote.settimeout(RELAY_TIMEOUT)
    except OSError:
        client.close()
        return
    left = threading.Thread(target=_relay, args=(client, remote, head_raw), daemon=True)
    right = threading.Thread(target=_relay, args=(remote, client), daemon=True)
    left.start()
    right.start()
    left.join()
    right.join()
    client.close()
    remote.close()


def _stop_previous() -> None:
    if not PID_FILE.is_file():
        return
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return
    if pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _daemonize() -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    base = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    path = socket_path_for(base)
    _stop_previous()
    _daemonize()
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    try:
        serve(path)
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
