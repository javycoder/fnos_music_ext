"""Private maintenance transport security and async callback delegation."""
import asyncio
import errno
import importlib.util
import json
import os
import socket
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "maintenance_socket", Path(__file__).resolve().parents[1] / "maintenance_socket.py")
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)


def request(method="POST", target=m.CLEANUP_PATH, body=b'{"confirm_beta":true}', extra=b""):
    return (f"{method} {target} HTTP/1.1\r\nContent-Length: {len(body)}\r\n".encode()
            + extra + b"\r\n" + body)


async def exchange(path, raw):
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(raw)
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    return int(data.split()[1]), json.loads(data.split(b"\r\n\r\n")[1])


def test_private_permissions_delegation_and_stop(tmp_path):
    async def run():
        calls = []
        loop = asyncio.get_running_loop()

        async def callback(method, path, body):
            assert asyncio.get_running_loop() is loop
            calls.append((method, path, body))
            return 202, {"job_id": "a" * 32, "state": "real proxy state"}

        directory = tmp_path / "private"
        path = directory / "maintenance.sock"
        server = await m.start(loop, callback, path)
        try:
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            status, data = await exchange(path, request())
            assert status == 202 and data["state"] == "real proxy state"
            assert calls == [("POST", m.CLEANUP_PATH, {"confirm_beta": True})]
            status, _ = await exchange(path, request("GET", m.CLEANUP_PATH + "/" + "a" * 32, b""))
            assert status == 202
            assert calls[-1][2] == {}
            with pytest.raises(FileExistsError):
                await m.start(loop, callback, path)
            assert (await exchange(path, request()))[0] == 202
        finally:
            await m.stop(server)
        assert not path.exists()
        await m.stop(server)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["file", "symlink", "directory", "insecure", "foreign"])
def test_reject_unsafe_paths(tmp_path, kind):
    async def run():
        directory = tmp_path / "private"
        directory.mkdir(mode=0o700)
        path = directory / "maintenance.sock"
        if kind == "file":
            path.write_text("do not remove")
        elif kind == "symlink":
            directory.rmdir()
            directory.symlink_to(tmp_path, target_is_directory=True)
        elif kind == "directory":
            path.mkdir()
        elif kind == "insecure":
            directory.chmod(0o777)
        elif kind == "foreign":
            original = os.getuid()
            with patch.object(m.os, "getuid", return_value=original + 1):
                with pytest.raises(PermissionError):
                    await m.start(asyncio.get_running_loop(), None, path)
            return
        with pytest.raises((PermissionError, FileExistsError)):
            await m.start(asyncio.get_running_loop(), None, path)
        if kind == "file":
            assert path.read_text() == "do not remove"
    asyncio.run(run())


def test_stale_socket_recovered(tmp_path):
    async def run():
        path = tmp_path / "maintenance.sock"
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(path))
            path.chmod(0o600)

        async def callback(*args):
            return 200, {"recovered": True}

        server = await m.start(asyncio.get_running_loop(), callback, path)
        try:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert await exchange(path, request()) == (200, {"recovered": True})
        finally:
            await m.stop(server)
    asyncio.run(run())


def test_live_socket_rejected_without_unlink(tmp_path):
    async def run():
        path = tmp_path / "maintenance.sock"
        with socket.socket(socket.AF_UNIX) as live:
            live.bind(str(path))
            live.listen(4)
            identity = path.stat().st_ino
            with pytest.raises(FileExistsError, match="listening"):
                await m.start(asyncio.get_running_loop(), None, path)
            assert path.stat().st_ino == identity
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(path))
    asyncio.run(run())


@pytest.mark.parametrize("replacement", ["socket", "file", "owner"])
def test_probe_identity_replacement_never_unlinked(tmp_path, monkeypatch, replacement):
    async def run():
        path = tmp_path / "maintenance.sock"
        replacement_path = tmp_path / "replacement.sock"
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(path))
        # Keep both inodes allocated so the replacement cannot reuse the old one.
        with socket.socket(socket.AF_UNIX) as other:
            other.bind(str(replacement_path))
        original_connect = socket.socket.connect
        original_lstat = Path.lstat
        replaced = False

        def connect(probe, address):
            nonlocal replaced
            if str(address) == str(path):
                if replacement == "socket":
                    replacement_path.replace(path)
                elif replacement == "file":
                    replacement_path.unlink()
                    replacement_path.write_text("replacement")
                    replacement_path.replace(path)
                replaced = True
                raise ConnectionRefusedError(errno.ECONNREFUSED, "stale probe")
            return original_connect(probe, address)

        def lstat(target, *args, **kwargs):
            result = original_lstat(target, *args, **kwargs)
            if target == path and replaced and replacement == "owner":
                values = list(result)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(Path, "lstat", lstat)
        with pytest.raises(FileExistsError, match="identity changed"):
            await m.start(asyncio.get_running_loop(), None, path)
        assert path.exists()
        if replacement == "file":
            assert path.read_text() == "replacement"
    asyncio.run(run())


@pytest.mark.parametrize("error", [errno.EACCES, errno.EAGAIN, errno.ETIMEDOUT, errno.EIO])
def test_unknown_probe_errors_preserve_socket(tmp_path, monkeypatch, error):
    async def run():
        path = tmp_path / "maintenance.sock"
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(path))
        identity = path.stat().st_ino

        def connect(*args):
            raise OSError(error, "probe failed")

        monkeypatch.setattr(socket.socket, "connect", connect)
        with pytest.raises(OSError) as caught:
            await m.start(asyncio.get_running_loop(), None, path)
        assert caught.value.errno == error
        assert path.stat().st_ino == identity
    asyncio.run(run())


def test_disappearing_socket_probe_retries_safely(tmp_path, monkeypatch):
    async def run():
        path = tmp_path / "maintenance.sock"
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(path))
        original_connect = socket.socket.connect
        probes = []

        def connect(probe, address):
            if not probes and str(address) == str(path):
                probes.append(address)
                path.unlink()
                raise FileNotFoundError(errno.ENOENT, "disappeared")
            return original_connect(probe, address)

        monkeypatch.setattr(socket.socket, "connect", connect)
        server = await m.start(asyncio.get_running_loop(), None, path)
        try:
            assert len(probes) == 1
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            await m.stop(server)
    asyncio.run(run())


def test_enoent_probe_rechecks_replacement_listener(tmp_path, monkeypatch):
    async def run():
        path = tmp_path / "maintenance.sock"
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(path))
        original_connect = socket.socket.connect
        calls = []
        with socket.socket(socket.AF_UNIX) as live:
            def connect(probe, address):
                if str(address) == str(path):
                    calls.append(address)
                    if len(calls) == 1:
                        path.unlink()
                        live.bind(str(path))
                        live.listen(4)
                        raise FileNotFoundError(errno.ENOENT, "replaced during probe")
                return original_connect(probe, address)

            monkeypatch.setattr(socket.socket, "connect", connect)
            with pytest.raises(FileExistsError, match="listening"):
                await m.start(asyncio.get_running_loop(), None, path)
            assert len(calls) == 2
            assert stat.S_ISSOCK(path.lstat().st_mode)
    asyncio.run(run())


def test_foreign_owned_socket_rejected(tmp_path, monkeypatch):
    async def run():
        path = tmp_path / "maintenance.sock"
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        try:
            original = Path.lstat

            def lstat(target, *args, **kwargs):
                result = original(target, *args, **kwargs)
                if target == path:
                    values = list(result)
                    values[4] = os.getuid() + 1
                    return os.stat_result(values)
                return result

            monkeypatch.setattr(Path, "lstat", lstat)
            with pytest.raises(PermissionError):
                await m.start(asyncio.get_running_loop(), None, path)
            assert path.exists()
        finally:
            sock.close()
    asyncio.run(run())


def test_existing_gateway_directory_permissions_unchanged(tmp_path):
    async def run():
        tmp_path.chmod(0o755)
        server = await m.start(asyncio.get_running_loop(), None, tmp_path / "maintenance.sock")
        await m.stop(server)
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    asyncio.run(run())


@pytest.mark.parametrize("raw", [
    request(body=b'{}'), request(body=b'{"confirm_beta":false}'),
    request(body=b'{"confirm_beta":1}'), request(body=b'{"confirm_beta":true,"path":"/tmp"}'),
    request(body=b'[]'), request(extra=b"Transfer-Encoding: chunked\r\n"),
    request(extra=b"Content-Length: 1\r\n"),
    b"POST /api/maintenance/cleanup HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
    b"POST /api/maintenance/cleanup HTTP/1.1\r\nContent-Length: 999999999999999\r\n\r\n",
    request("DELETE"), request("GET", m.CLEANUP_PATH + "/../anything", b""),
    request("GET", m.CLEANUP_PATH + "/" + "g" * 32, b""),
    request(extra=b"Long: " + b"x" * m.MAX_HEADER + b"\r\n"),
])
def test_invalid_requests_never_delegate(tmp_path, raw):
    async def run():
        calls = []

        async def callback(*args):
            calls.append(args)
            return 200, {}

        path = tmp_path / "maintenance.sock"
        server = await m.start(asyncio.get_running_loop(), callback, path)
        try:
            assert (await exchange(path, raw))[0] == 400
            assert calls == []
        finally:
            await m.stop(server)
    asyncio.run(run())


def test_request_and_callback_timeouts(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "REQUEST_TIMEOUT", 0.03)
    monkeypatch.setattr(m, "CALLBACK_TIMEOUT", 0.03)

    async def run():
        async def callback(*args):
            await asyncio.sleep(1)
        path = tmp_path / "maintenance.sock"
        server = await m.start(asyncio.get_running_loop(), callback, path)
        try:
            assert (await exchange(path, b"POST "))[0] == 408
            assert (await exchange(path, request()))[0] == 408
        finally:
            await m.stop(server)
    asyncio.run(run())


def test_stop_preserves_replacement_and_closes_clients(tmp_path):
    async def run():
        path = tmp_path / "maintenance.sock"
        server = await m.start(asyncio.get_running_loop(), None, path)
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"POST ")
        await writer.drain()
        await asyncio.sleep(0)
        path.unlink()
        path.write_text("replacement")
        await m.stop(server)
        assert path.read_text() == "replacement"
        try:
            assert await asyncio.wait_for(reader.read(), 1) == b""
        except ConnectionResetError:
            pass  # Closing a socket with an unread request may reset the peer.
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass
    asyncio.run(run())
