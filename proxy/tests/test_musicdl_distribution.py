"""Issue #69 distribution contracts; all runtime data and children are isolated.

No Docker build, NAS lifecycle commands, real music provider, or fnpack download.
The FPK payload test executes only build.sh's real rsync stanza: --stage-only
also invokes network-capable lxserver preparation and is not inherently offline.
"""
from __future__ import annotations

import ast
import asyncio
import configparser
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import textwrap
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "musicdl-service"
MODULES = ("app.py", "hardening.py", "search_core.py", "worker.py")


def load_hardening():
    spec = importlib.util.spec_from_file_location("distribution_hardening", SERVICE / "hardening.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_container_explicit_copy_contains_complete_worker_runtime(tmp_path):
    """Materialize the actual COPY inputs, not just a substring assertion."""
    copied = set()
    dockerfile = (ROOT / "container/Dockerfile").read_text()
    for line in dockerfile.replace("\\\n", " ").splitlines():
        tokens = shlex.split(line, comments=True)
        if not tokens or tokens[0].upper() != "COPY":
            continue
        args = [arg for arg in tokens[1:] if not arg.startswith("--")]
        if args[-1] == "/srv/musicdl-service/":
            for source in args[:-1]:
                assert source.startswith("musicdl-service/")
                shutil.copyfile(ROOT / source, tmp_path / Path(source).name)
                copied.add(Path(source).name)
    assert set(MODULES) <= copied
    for name in MODULES:
        compile((tmp_path / name).read_text(), name, "exec")
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "!musicdl-service/" in ignore
    assert not any(line in ignore for line in ("**/worker.py", "**/search_core.py"))
    # The shared file logger must also be available in the image.
    assert any("proxy/fnlog.py" in line and line.startswith("COPY ")
               for line in dockerfile.splitlines())


@pytest.mark.parametrize("config", ["supervisord.conf", "supervisord-native.conf.in"])
def test_native_and_container_supervise_same_app_and_worker_group(config):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "container" / config)
    program = parser["program:musicdl"]
    assert program["directory"].endswith("/musicdl-service")
    assert shlex.split(program["command"])[1] == "app:app"
    assert program.getboolean("stopasgroup")
    assert program.getboolean("killasgroup")
    # A multiprocessing fork/preexec path must not reappear in native launch.
    tree = ast.parse((SERVICE / "hardening.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            assert not (isinstance(func, ast.Attribute) and func.attr in {"fork", "forkserver"})
            if isinstance(func, ast.Attribute) and func.attr == "get_context" and node.args:
                assert not (isinstance(node.args[0], ast.Constant) and node.args[0].value == "fork")


def test_worker_spawn_uses_current_interpreter_and_sibling_script(monkeypatch):
    module = load_hardening()
    proc = type("FakeProcess", (), {"stderr": None})()
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("PYTHONPATH", "/fixture/existing")
    worker = asyncio.run(module.spawn_subprocess_worker("KuwoMusicClient"))
    assert worker.proc is proc
    args, kwargs = spawn.call_args
    assert args == (sys.executable, "-u", str(SERVICE / "worker.py"))
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep) == [str(SERVICE), "/fixture/existing"]
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert "preexec_fn" not in kwargs
    assert "start_new_session" not in kwargs  # Remains in supervisor's process group.


def test_worker_protocol_runs_from_distributed_files_without_network(tmp_path):
    """Run the real worker with a fake provider core, including noisy fd output."""
    shutil.copyfile(SERVICE / "worker.py", tmp_path / "worker.py")
    (tmp_path / "search_core.py").write_text(textwrap.dedent('''\
        import os
        print("fake library import chatter", flush=True)
        def describe_task_error(exc, stage=None):
            # 与真实 search_core 协议一致：只回传异常类型与阶段，不回传正文。
            return f"{type(exc).__name__} (stage={stage or 'unspecified'})"
        def run_search_task(task, emit, on_stage=None):
            os.write(1, b"fake native library chatter\\n")
            if on_stage is not None:
                on_stage("search")
            if task.get("fail"):
                raise ValueError("synthetic-private-detail")
            emit([{"id": "kuwo:fixture"}, {}, ""])
            return {"count": 1, "partial": False}
    '''))
    result = subprocess.run(
        [sys.executable, "-u", str(tmp_path / "worker.py")],
        input='{"fail": true}\n{}\n', text=True, capture_output=True,
        cwd=tmp_path, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    import json
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert [message["type"] for message in messages] == ["ready", "error", "entry", "result"]
    assert messages[-1] == {"type": "result", "count": 1, "partial": False}
    assert "fake library import chatter" in result.stderr
    assert "fake native library chatter" in result.stderr
    assert "synthetic-private-detail" not in result.stdout


def test_fpk_real_payload_copy_packages_all_musicdl_modules(tmp_path):
    if not shutil.which("rsync"):
        pytest.skip("rsync is required for the real offline payload copy")
    source = tmp_path / "source"
    service = source / "musicdl-service"
    service.mkdir(parents=True)
    for module in MODULES:
        shutil.copyfile(SERVICE / module, service / module)
    (source / ".env").write_text("fixture-only-secret")
    (service / "musicdl.log").write_text("fixture log")
    (service / "__pycache__").mkdir()
    (service / "__pycache__/worker.pyc").write_bytes(b"fixture")
    stage = tmp_path / "stage"
    target = stage / "app/repo"
    target.mkdir(parents=True)
    script = (ROOT / "packaging/fpk/build.sh").read_text()
    start = script.index("rsync -a --delete")
    tail = '"${REPO_ROOT}/" "${STAGE}/app/repo/"'
    end = script.index(tail, start) + len(tail)
    result = subprocess.run(["bash", "-c", script[start:end]],
                            env={**os.environ, "REPO_ROOT": str(source), "STAGE": str(stage)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for module in MODULES:
        assert (target / "musicdl-service" / module).read_bytes() == (service / module).read_bytes()
    assert not (target / ".env").exists()
    assert not list(target.rglob("*.log"))
    assert not list(target.rglob("__pycache__"))


def test_musicdl_unified_logger_rotates_and_webui_archive_includes_it(tmp_path):
    """Import-time logger setup is checked in a clean process, never real data."""
    code = textwrap.dedent('''\
        import importlib.util, logging, os, sys, time, types, zipfile
        from pathlib import Path
        root, isolated = map(Path, sys.argv[1:])
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / "musicdl-service"))
        fake = types.ModuleType("musicdl")
        fake.musicdl = types.SimpleNamespace(SUPPORTED_MUSIC_SOURCES=["KuwoMusicClient", "MiguMusicClient"])
        sys.modules["musicdl"] = fake
        def load(name, file):
            spec = importlib.util.spec_from_file_location(name, file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        app = load("distribution_musicdl_app", root / "musicdl-service/app.py")
        from proxy import fnlog
        handlers = [h for h in app.logger.handlers if isinstance(h, fnlog._BoundedTimedRotatingFileHandler)]
        assert len(handlers) == 1
        handler = handlers[0]
        assert Path(handler.baseFilename) == isolated / "logs/musicdl.log"
        assert handler.backupCount == fnlog.RETENTION_DAYS - 1
        # Synthetic diagnostic uses the shared redactor; arbitrary third-party output
        # is intentionally not a supported diagnostic/logging API.
        app.logger.info("[diag] %s", fnlog.redact("source=kuwo token=fixture-private-token"))
        handler.flush()
        handler.rolloverAt = time.time() - 1
        app.logger.info("[diag] source=kuwo result_count=1")
        handler.flush()
        logs = isolated / "logs"
        files = list(logs.glob("musicdl.log*"))
        assert len(files) == 2
        combined = "".join(p.read_text() for p in files)
        assert "fixture-private-token" not in combined
        assert "source=kuwo" in combined and "result_count=1" in combined
        webui = load("distribution_webui_app", root / "webui-service/app.py")
        # Avoid even environment probes; test only the actual export/file helpers.
        webui.env_snapshot_lines = lambda context: ["[env] context=fixture"]
        stale = logs / "musicdl.log.expired"
        stale.write_text("expired")
        old = time.time() - (fnlog.RETENTION_DAYS + 1) * 86400
        os.utime(stale, (old, old))
        (logs / "musicdl.log.symlink").symlink_to(isolated / "never-read")
        names = {entry["name"] for entry in webui._log_files_info()}
        assert {p.name for p in files} <= names
        assert stale.name not in names
        archive = isolated / "support.logzip"
        webui._write_log_archive(logs, archive)
        with zipfile.ZipFile(archive) as zf:
            assert {p.name for p in files} <= set(zf.namelist())
            assert stale.name not in zf.namelist()
            assert "musicdl.log.symlink" not in zf.namelist()
            assert "export_env_snapshot.txt" in zf.namelist()
            for file in files:
                assert zf.read(file.name) == file.read_bytes()
        handler.close()
    ''')
    env = {**os.environ, "FNMUSIC_LOG_DIR": str(tmp_path / "logs"),
           "FNMUSIC_ENV_FILE": str(tmp_path / "absent.env"),
           "WEBUI_REPO_DIR": str(tmp_path), "FNMUSIC_HOME": str(tmp_path),
           "MUSICDL_WORK_DIR": str(tmp_path / "outputs"),
           "MUSICDL_SOURCES": "KuwoMusicClient,MiguMusicClient"}
    result = subprocess.run([sys.executable, "-c", code, str(ROOT), str(tmp_path)],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
