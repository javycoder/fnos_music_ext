#!/usr/bin/env python3
"""把飞牛桌面网关的 Unix socket 原样转到本机 WebUI。

应用中心按 ui/config 的 gatewaySocket 连接
<应用目录>/fnmusic-ext.sock，把 https://<主机>:<桌面端口>/app/fnmusic-ext/
转到这个 socket。WebUI 自己监听 127.0.0.1:8774，并认识 /app/fnmusic-ext 前缀。
"""
from __future__ import annotations

import os
import signal
import socket
import sys
import threading
from pathlib import Path

UPSTREAM = ("127.0.0.1", 8774)
SOCK_NAME = "fnmusic-ext.sock"
PID_FILE = Path("/run/fnmusic-ext/webui-gateway.pid")


def socket_path_for(base: Path) -> Path:
    """fpk 布局是 <应用目录>/repo。socket 必须放在应用目录下，桌面网关才找得到。"""
    parent = base.resolve().parent
    if (parent / "ui").is_dir() and base.name == "repo":
        return parent / SOCK_NAME
    run = Path("/run/fnmusic-ext")
    run.mkdir(parents=True, exist_ok=True)
    return run / SOCK_NAME


def _relay(src: socket.socket, dst: socket.socket) -> None:
    try:
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


def _handle(client: socket.socket, upstream: tuple[str, int]) -> None:
    try:
        remote = socket.create_connection(upstream, timeout=5)
    except OSError:
        client.close()
        return
    left = threading.Thread(target=_relay, args=(client, remote), daemon=True)
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
