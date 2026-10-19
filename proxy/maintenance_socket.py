"""Private HTTP transport for maintenance; business state stays in the proxy callback."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import socket
import stat
from pathlib import Path

DEFAULT_SOCKET = "/run/fnmusic-ext/maintenance.sock"
MAX_HEADER = 16 * 1024
MAX_BODY = 4096
REQUEST_TIMEOUT = 5
CALLBACK_TIMEOUT = 30
CLEANUP_PATH = "/api/maintenance/cleanup"


def _parent(path: Path) -> None:
    # Never chmod an existing shared gateway directory. Inspect each component
    # before creating the next one, rather than following symlinks via mkdir.
    for directory in reversed(path.parent.parents):
        st = directory.lstat()
        if not stat.S_ISDIR(st.st_mode):
            raise PermissionError(f"Unsafe directory: {directory}")
        if st.st_mode & 0o022 and not (st.st_uid == 0 and st.st_mode & stat.S_ISVTX):
            raise PermissionError(f"Writable ancestor: {directory}")
    try:
        path.parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = path.parent.lstat()
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or st.st_mode & 0o022):
        raise PermissionError(f"Unsafe socket directory: {path.parent}")


def _route(method: str, path: str) -> bool:
    return ((method == "POST" and path == CLEANUP_PATH)
            or (method == "GET" and re.fullmatch(CLEANUP_PATH + r"/[0-9a-fA-F]{32}", path) is not None))


def _recover_stale_socket(path: Path) -> None:
    """Remove only an owned, unchanged socket which refuses connections.

    A successful connect, timeout, permission error, or changed identity is not
    evidence of a stale listener. Never unlink in those cases. A disappearing
    path is checked again; the later bind still refuses any concurrent winner.
    """
    for _ in range(3):
        try:
            before = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
            raise PermissionError(f"Unsafe existing socket path: {path}")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(REQUEST_TIMEOUT)
            try:
                probe.connect(str(path))
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    continue
                if exc.errno != errno.ECONNREFUSED:
                    raise
            else:
                raise FileExistsError(f"Socket is listening: {path}")
        try:
            after = path.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISSOCK(after.st_mode) or after.st_uid != os.getuid()
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
            raise FileExistsError(f"Socket identity changed during probe: {path}")
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        return
    raise FileExistsError(f"Socket path changed repeatedly during probe: {path}")


async def start(loop, callback, path=DEFAULT_SOCKET):
    """Await to bind a 0600 socket on the callback's own running event loop.

    Recover only confirmed owned stale sockets; never replace a live listener.
    Call stop() to remove only the inode owned by this server.
    """
    if asyncio.get_running_loop() is not loop:
        raise ValueError("maintenance server must run on the supplied loop")
    path = Path(os.path.abspath(path))
    _parent(path)
    _recover_stale_socket(path)

    clients = {}

    def handle(reader, writer):
        if not server.is_serving():
            writer.close()
            return
        task = loop.create_task(run_handle(reader, writer))
        clients[task] = writer
        task.add_done_callback(lambda done: clients.pop(done, None))

    async def run_handle(reader, writer):
        task = asyncio.current_task()
        status, payload = 400, {"ok": False, "error": "Invalid maintenance request"}
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                head = await reader.readuntil(b"\r\n\r\n")
                if len(head) > MAX_HEADER:
                    raise ValueError("Header too large")
                lines = head[:-4].decode("ascii").split("\r\n")
                method, target, version = lines[0].split(" ")
                if version not in ("HTTP/1.0", "HTTP/1.1") or not _route(method, target):
                    raise ValueError("Invalid route")
                headers = {}
                for line in lines[1:]:
                    key, value = line.split(":", 1)
                    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                        raise ValueError("Invalid header")
                    key = key.lower()
                    if key in headers:
                        raise ValueError("Duplicate header")
                    headers[key] = value.strip()
                if "transfer-encoding" in headers:
                    raise ValueError("Transfer encoding is unsupported")
                length = headers.get("content-length", "0")
                if not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > MAX_BODY:
                    raise ValueError("Invalid content length")
                body = await reader.readexactly(int(length))
                if method == "POST":
                    body = json.loads(body)
                    if body != {"confirm_beta": True} or type(body.get("confirm_beta")) is not bool:
                        raise ValueError("Explicit confirmation required")
                elif body:
                    raise ValueError("GET body is unsupported")
                else:
                    body = {}
            status, payload = await asyncio.wait_for(callback(method, target, body), CALLBACK_TIMEOUT)
            if type(status) is not int or not 200 <= status <= 599 or not isinstance(payload, dict):
                raise RuntimeError("Invalid callback response")
        except asyncio.CancelledError:
            writer.close()
            clients.pop(task, None)
            raise
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except TimeoutError:
            status, payload = 408, {"ok": False, "error": "Maintenance request timed out"}
        except Exception:
            status, payload = 500, {"ok": False, "error": "Maintenance callback failed"}
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            writer.write((f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
            await asyncio.wait_for(writer.drain(), REQUEST_TIMEOUT)
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            clients.pop(task, None)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        # No await while the process umask is changed; publish no writable window.
        old_umask = os.umask(0o177)
        try:
            sock.bind(str(path))
        finally:
            os.umask(old_umask)
        st = path.lstat()
        identity = (st.st_dev, st.st_ino)
        os.chmod(path, 0o600)
        sock.setblocking(False)
        server = await asyncio.start_unix_server(handle, sock=sock, limit=MAX_HEADER)
    except BaseException:
        sock.close()
        if identity is not None and _same_inode(path, identity):
            path.unlink()
        raise
    server._maintenance_state = (path, identity, clients)
    return server


def _same_inode(path, identity):
    try:
        st = path.lstat()
        return (st.st_dev, st.st_ino) == identity and stat.S_ISSOCK(st.st_mode)
    except FileNotFoundError:
        return False


async def stop(server):
    """Close the listener and active clients, then unlink only our own socket."""
    server.close()
    await server.wait_closed()
    path, identity, clients = server._maintenance_state
    tasks = list(clients)
    for task in tasks:
        clients[task].close()
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if _same_inode(path, identity):
        path.unlink()
