#!/usr/bin/env python3
"""Explicit NAS/container smoke check; never run on a production filesystem.

Run as uid 1000 in a fresh, unprivileged container with private /run and /data
tmpfs, no host mounts and no published ports. Copy the checkout to /tmp/repo.
The official and musicdl endpoints below are controlled HTTP fixtures. This
checks real sockets, the proxy and cross-directory log export, not live music.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import zipfile


async def main():
    if not Path("/.dockerenv").exists() or os.geteuid() == 0:
        raise RuntimeError("Requires a fresh, nonroot container")
    root = Path(__file__).resolve().parents[2]
    if root != Path("/tmp/repo"):
        raise RuntimeError("Checkout must be copied to /tmp/repo in the disposable container")
    work = Path("/tmp/issue126-runtime")
    work.mkdir()  # Refuse reused state.
    host_logs = root / "logs"
    host_logs.mkdir(exist_ok=True)
    os.environ.update({
        "FNMUSIC_HOME": str(root), "FNMUSIC_LOG_DIR": str(host_logs),
        "FNMUSIC_CACHE_DIR": str(work / "cache"),
        "FNMUSIC_LIBRARY_DIR": str(work / "library"),
        "FNMUSIC_FAV_DIR": str(work / "favorites"),
        "FNMUSIC_PLT_DIR": str(work / "playlists"),
        "FNMUSIC_UPSTREAM_SOCK": str(work / "official.sock"),
        "FNMUSIC_MUSICDL_URL": "http://127.0.0.1:18968",
        "FNMUSIC_MUSICDL_ENABLED": "true", "FNMUSIC_NETEASE_ENABLED": "false",
        "FNMUSIC_LX_ENABLED": "false", "FNMUSIC_RECOMMEND_HOT": "false",
        "FNMUSIC_RECOMMEND_DAILY": "false", "FNMUSIC_SEARCH_DEBOUNCE_S": "0",
        "FNMUSIC_BACKGROUND_JOBS": "0", "WEBUI_REPO_DIR": str(root),
        "WEBUI_MUSICDL_URL": "http://127.0.0.1:18968",
    })
    sys.path.insert(0, str(root))
    import httpx
    import uvicorn
    from fastapi import FastAPI
    from proxy import app as proxy
    from proxy import maintenance_socket

    fixture = FastAPI()

    @fixture.get("/healthz")
    async def health():
        return {"ok": True}

    @fixture.get("/search")
    async def search():
        return {"ok": True, "items": [{
            "id": "kuwo:126", "source": "kuwo", "title": "fixture",
            "artist": "fixture", "duration_s": 60, "ext": "mp3",
        }], "errors": {"fixture": "timeout token=fixture-never-export"}}

    async def official(reader, writer):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            data = json.dumps({"code": 0, "data": {"list": [], "total": 0}}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    official_server = await asyncio.start_unix_server(official, path=str(work / "official.sock"))
    servers, tasks = [], []
    private_server = None

    async def start(app, **kwargs):
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, **kwargs))
        servers.append(server)
        task = asyncio.create_task(server.serve())
        tasks.append(task)
        for _ in range(200):
            if task.done():
                task.result()
                raise RuntimeError("Server exited before readiness")
            if server.started:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("Server readiness")

    try:
        await start(fixture, host="127.0.0.1", port=18968)
        await start(proxy.app, uds="/var/run/trim_music.socket")
        private_server = await maintenance_socket.start(
            asyncio.get_running_loop(), proxy._maintenance_request)
        os.environ["FNMUSIC_LOG_DIR"] = "/data/logs"
        spec = importlib.util.spec_from_file_location("issue126_webui", root / "webui-service/app.py")
        webui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(webui)
        await start(webui.app, host="127.0.0.1", port=18974)
        transport = httpx.AsyncHTTPTransport(uds="/var/run/trim_music.socket")
        async with httpx.AsyncClient(transport=transport, base_url="http://unix", timeout=10) as client:
            response = await client.get("/music/api/v1/search/track",
                                        params={"keyword": "private-fixture-query"})
            assert response.status_code == 200
            assert len(response.json()["data"]["list"]) == 1
        async with httpx.AsyncClient(base_url="http://127.0.0.1:18974", timeout=10) as client:
            assert (await client.get("/api/logs/export")).status_code == 403
            response = await client.get("/api/logs/export", headers={"X-Trim-Isadmin": "true"})
            assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            manifest = json.loads(archive.read("log_collection.json"))
            host = next(row for row in manifest["sources"] if row["scope"] == "host")
            assert host["status"] == "ok" and "proxy.log" in host["files"]
            report = json.loads(archive.read("proxy_diagnostics.json"))
            assert report["status"] == "reachable"
            assert report["takeover"]["status"] == report["upstream"]["status"] == "ok"
            logs = archive.read("host/proxy.log").decode()
            assert "search_received" in logs and "search_source" in logs and "search_finished" in logs
            assert "online_page=1" in logs
            for name in archive.namelist():
                content = archive.read(name)
                assert b"fixture-never-export" not in content
                assert b"private-fixture-query" not in content
            root_log = archive.read("host/install.log")
            assert b"root-owned-fixture" in root_log
            assert (host_logs / "install.log").stat().st_uid == 0
        result = {
            "status": "ok", "uid": os.geteuid(), "real_unix_sockets": True,
            "takeover": "ok", "upstream": "ok", "online_page": 1,
            "root_owned_host_log_read": True, "admin_auth": "ok", "redaction": "ok",
            "validation_scope": "isolated_container_with_controlled_http_fixtures",
            "live_music_playback_verified": False,
        }
        print(json.dumps(result))
    finally:
        if private_server is not None:
            await maintenance_socket.stop(private_server)
        for server in reversed(servers):
            server.should_exit = True
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        official_server.close()
        await official_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
