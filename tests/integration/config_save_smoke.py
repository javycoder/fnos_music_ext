"""Opt-in save smoke for a disposable, network-disabled sources container.

Run with FNMUSIC_ISOLATED_SAVE_TEST=1. The caller must provide an empty container,
without host data mounts or published ports. Only synthetic scripts are executed.
The slow-adapter scenario uses the production 55-second budget without shortening it.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx


def report(scenario, started, **values):
    print(json.dumps({"scenario": scenario, "elapsed_s": round(time.monotonic() - started, 3),
                      **values}), flush=True)


def call(client, method, port, path, body=None):
    response = client.request(method, f"http://127.0.0.1:{port}{path}", json=body)
    assert response.status_code == 200, (path, response.status_code, response.text[:400])
    return response.json()


def real_services(work: Path):
    repo = work / "repo"
    repo.mkdir()
    (repo / ".env").write_text(
        "FNMUSIC_LX_ENABLED=true\nFNMUSIC_NETEASE_ENABLED=false\n"
        "FNMUSIC_MUSICDL_ENABLED=false\nLX_SOURCE_LIST='[]'\nLX_SOURCE_URL=''\n"
    )
    os.environ["WEBUI_REPO_DIR"] = str(repo)
    os.environ["LX_ENV_PATH"] = str(repo / ".env")
    data = Path("/data/lxserver")
    assert not (data / "config.js").exists(), "refusing an existing application data directory"
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.js").write_text(
        'module.exports = { bindIP: "127.0.0.1", port: 8005, '
        '"frontend.password": "isolated-save-test", "player.enableAuth": false, '
        '"system.allowUnsafeVM": false, "subsonic.enable": false, "webdav.enable": false };'
    )
    os.environ["LXSERVER_ADMIN_PASSWORD"] = "isolated-save-test"
    log = (work / "services.log").open("wb")
    supervisor = subprocess.Popen(
        ["supervisord", "-c", "/etc/supervisor/supervisord.conf"],
        stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(timeout=58, trust_env=False, headers={"X-Trim-Isadmin": "true"}) as client:
            end = time.monotonic() + 10
            while not Path("/tmp/supervisor.sock").exists():
                assert time.monotonic() < end, "supervisor startup timeout"
                time.sleep(0.05)
            for name in ("lxserver", "lxmusic", "webui"):
                subprocess.run(["supervisorctl", "start", name], check=True,
                               capture_output=True, timeout=15)
            end = time.monotonic() + 15
            while True:
                try:
                    lx_ready = client.get("http://127.0.0.1:8003/healthz").json().get("ok")
                    webui_ready = client.get("http://127.0.0.1:8004/api/config").status_code == 200
                    if lx_ready and webui_ready:
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                if time.monotonic() >= end:
                    tail = (work / "services.log").read_text(errors="replace")[-5000:]
                    raise AssertionError(f"isolated services startup timeout: {tail}")
                time.sleep(0.1)
            items = []
            for name in ("SaveSmokeA", "SaveSmokeB", "SaveSmokeC"):
                script = (
                    f"/**\n * @name {name}\n * @version 1.0.0\n */\n"
                    'lx.send(lx.EVENT_NAMES.inited, {status:true, sources:{kw:{'
                    'name:"synthetic", type:"music", actions:["musicUrl"], qualitys:["128k"]}}});\n'
                    'lx.on(lx.EVENT_NAMES.request, () => Promise.reject(new Error("synthetic source")));'
                )
                uploaded = call(client, "POST", 8004, "/api/lx/upload",
                                {"filename": f"{name}.js", "script": script})
                items.append({"name": name, "url": uploaded["data"]["url"], "active": False})
            items[0]["active"] = items[1]["active"] = True
            started = time.monotonic()
            saved = call(client, "PUT", 8004, "/api/config", {"values": {"LX_SOURCE_LIST": items}})
            assert saved["complete"] is True, saved
            report("real_activate_two", started, complete=True)
            items = json.loads(saved["values"]["LX_SOURCE_LIST"])
            items[1]["active"], items[2]["active"] = False, True
            started = time.monotonic()
            saved = call(client, "PUT", 8004, "/api/config", {"values": {"LX_SOURCE_LIST": items}})
            assert saved["complete"] is True, saved
            assert [a["op"] for a in saved["actions"] if a["kind"] == "lx_activate"] == [
                "deactivate", "activate"]
            report("real_cancel_and_activate", started, complete=True)
            items = json.loads(saved["values"]["LX_SOURCE_LIST"])
            started = time.monotonic()
            saved = call(client, "PUT", 8004, "/api/config", {"values": {"LX_SOURCE_LIST": items}})
            assert saved["complete"] and not saved["actions"], saved
            report("real_unchanged", started, complete=True)
            items[2]["active"] = False
            items.append({"name": "missing", "url": "file:///tmp/no-such-save-source.js", "active": True})
            started = time.monotonic()
            saved = call(client, "PUT", 8004, "/api/config", {"values": {"LX_SOURCE_LIST": items}})
            actual = json.loads(saved["values"]["LX_SOURCE_LIST"])
            assert saved["complete"] is False and [i["active"] for i in actual] == [True, False, False, False], saved
            assert time.monotonic() - started < 5
            report("real_cancel_survives_bad_activation", started, complete=False)
            assert "/api/v1/source/verify" not in (work / "services.log").read_text(errors="replace")
    finally:
        supervisor.terminate()
        try:
            supervisor.wait(timeout=12)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            supervisor.wait()
        log.close()


async def slow_adapter(work: Path):
    spec = importlib.util.spec_from_file_location("isolated_save_webui", "/srv/webui-service/app.py")
    webui = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = webui
    spec.loader.exec_module(webui)
    webui.ENV_PATH = work / "slow.env"
    ready = {"url": "https://synthetic.test/ready.js", "id": "ready", "active": True}
    webui.ENV_PATH.write_text(
        "FNMUSIC_LX_ENABLED=true\nFNMUSIC_NETEASE_ENABLED=false\n"
        f"LX_SOURCE_LIST='{json.dumps([ready])}'\nLX_SOURCE_URL='{ready['url']}'\n")

    async def healthy(*args, **kwargs):
        return {"ok": True}

    webui._lx_ensure_ready = healthy
    mutations = []
    cancelled = []

    async def adapter(request):
        if request.method == "GET":
            return httpx.Response(200, json={"ok": True, "data": {"sources": [
                {**ready, "status": "success", "platforms": ["kw"]}]}})
        mutations.append(json.loads(request.content))
        try:
            await asyncio.sleep(90)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        raise AssertionError("stalled adapter escaped save budget")

    async with httpx.AsyncClient(transport=httpx.MockTransport(adapter)) as upstream:
        webui.app.state.http = upstream
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webui.app),
                                    base_url="http://isolated", headers={"X-Trim-Isadmin": "true"}) as client:
            items = [ready] + [{"url": f"https://synthetic.test/slow-{i}.js", "active": True}
                               for i in range(8)]
            started = time.monotonic()
            response = await client.put("/api/config", json={"values": {"LX_SOURCE_LIST": items}})
            elapsed = time.monotonic() - started
            result = response.json()
            assert response.status_code == 200 and not result["complete"], result
            assert 48 <= elapsed < 55, elapsed
            assert len(mutations) == len(cancelled) and len(mutations) < 8
            saved = webui.read_env()
            assert [i["active"] for i in json.loads(saved["LX_SOURCE_LIST"])] == [True] + [False] * 8
            assert any(a.get("category") == "not_executed" for a in result["actions"])
            followup = await client.put("/api/config", json={"values": {"FNMUSIC_RECOMMEND_DAILY": False}})
            assert followup.status_code == 200 and followup.json()["complete"]
            await asyncio.sleep(0.05)
            assert webui.read_env()["FNMUSIC_RECOMMEND_DAILY"] == "false"
            report("full_budget_stalled_adapter", started, budget_s=55, complete=False,
                   cancelled_mutations=len(cancelled), saved_active_count=1)


def main():
    if os.environ.get("FNMUSIC_ISOLATED_SAVE_TEST") != "1" or not Path("/.dockerenv").exists():
        raise SystemExit("requires an explicitly authorized disposable container")
    assert os.geteuid() != 0, "test must run as the normal unprivileged application user"
    assert not Path("/tmp/supervisor.sock").exists(), "refusing an existing service instance"
    with tempfile.TemporaryDirectory(prefix="fn-save-smoke-") as directory:
        work = Path(directory)
        real_services(work)
        asyncio.run(slow_adapter(work))


if __name__ == "__main__":
    main()
