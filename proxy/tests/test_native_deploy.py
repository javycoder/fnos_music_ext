"""原生（无 Docker）部署形态测试。

覆盖 v2.8.0a 引入的三块：
- lxmusic-service：LXSERVER_DATA_DIR 路径可配置化 + 跨形态 file:// 源路径归一
- webui-service：WEBUI_SUPERVISORCTL 携带参数（shlex 拆分）
- install.sh / proxy/install_common.sh / ensure_sources_native.sh 的原生形态
  shell 函数：部署形态解析、supervisord 配置渲染、洛雪源 file:// 迁移、
  lxserver 离线落位（预置包 + sha256 + sed 补丁 + 幂等）

全部桩化外部命令，不触碰宿主 docker/systemd/网络。
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
if BASH is None:
    pytest.skip("bash 不可用", allow_module_level=True)


def _load_module(rel: str, name: str):
    path = BASE / rel
    # lxmusic app 顶层 from lxserver_client import ...：加载期需 service 目录在 sys.path，
    # 加载完即移除，避免污染其他测试的导入环境
    service_dir = str(path.resolve().parent)
    added = service_dir not in sys.path
    if added:
        sys.path.insert(0, service_dir)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    finally:
        if added:
            sys.path.remove(service_dir)
    return mod


lxapp = _load_module("lxmusic-service/app.py", "lxmusic_native_test_app")
webui = _load_module("webui-service/app.py", "webui_native_test_app")


def function(text: str, name: str) -> str:
    start = text.index(name + "() {")
    return text[start:text.index("\n}", start) + 2] + "\n"


def run_bash(body: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run([BASH, "-c", body], env=full_env,
                          cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=90)


# ------------------------------------------------------------ lxmusic-service ---

def test_lx_source_fs_path_env_override(monkeypatch):
    # 默认保持容器路径约定；原生部署经 LXSERVER_DATA_DIR 重指
    monkeypatch.setattr(lxapp, "LXSERVER_DATA_DIR", "/data/lxserver")
    assert lxapp._lx_source_fs_path("foo.js") == "/data/lxserver/users/source/_open/foo.js"
    monkeypatch.setattr(lxapp, "LXSERVER_DATA_DIR", "/vol1/apps/fnmusic/sources-data/lxserver")
    assert lxapp._lx_source_fs_path("foo.js") == \
        "/vol1/apps/fnmusic/sources-data/lxserver/users/source/_open/foo.js"


def test_lx_normalize_legacy_file_url_docker_to_native(monkeypatch, tmp_path):
    # docker→native：旧 URL 指向容器 /data，本形态 uploads 下确有同名脚本时改写
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "my.js").write_text("// src", encoding="utf-8")
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path))
    rewritten = lxapp._normalize_legacy_file_url("file:///data/lxmusic/uploads/my.js")
    assert rewritten == f"file://{tmp_path}/uploads/my.js"


def test_lx_normalize_legacy_file_url_keeps_valid_and_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("LX_DATA_DIR", str(tmp_path))
    # http(s) 一律不动
    assert lxapp._normalize_legacy_file_url("https://example.com/s.js") == "https://example.com/s.js"
    # 原路径存在（当前形态本就正确）不动
    real = tmp_path / "real.js"
    real.write_text("//", encoding="utf-8")
    assert lxapp._normalize_legacy_file_url(f"file://{real}") == f"file://{real}"
    # 目标候选也不存在：保留原值（宁可失效不可错指）
    assert lxapp._normalize_legacy_file_url("file:///data/lxmusic/uploads/none.js") == \
        "file:///data/lxmusic/uploads/none.js"


def test_lx_normalize_legacy_file_url_native_to_docker(monkeypatch, tmp_path):
    # native→docker：LX_DATA_DIR=/data/lxmusic（容器内真实存在），宿主路径候选不存在时
    # 不改写——容器内路径在宿主侧不可判定，交由容器内原路径存在性兜底
    monkeypatch.setenv("LX_DATA_DIR", "/data/lxmusic")
    url = f"file://{tmp_path}/sources-data/lxmusic/uploads/a.js"
    assert lxapp._normalize_legacy_file_url(url) == url


# ------------------------------------------------------------- webui-service ---

def test_webui_supervisorctl_accepts_args_in_command(monkeypatch, tmp_path):
    # WEBUI_SUPERVISORCTL 可携带参数（原生形态：supervisorctl -c <配置路径>）
    stub = tmp_path / "supervisorctl_stub.py"
    stub.write_text(
        "import sys\nprint(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(webui.CONF, "supervisorctl", f"{sys.executable} {stub}")
    code, out = webui.supervisorctl("status", "musicdl")
    assert code == 0
    assert "status musicdl" in out


def test_webui_supervisorctl_default_single_word_unchanged(monkeypatch, tmp_path):
    # 默认裸 "supervisorctl"：拆分后行为不变（FileNotFoundError → 127 哨兵）
    missing = tmp_path / "no-such-supervisorctl"
    monkeypatch.setitem(webui.CONF, "supervisorctl", str(missing))
    code, out = webui.supervisorctl("status")
    assert code == 127
    assert "not found" in out


# ------------------------------------------------------------ 模板形状断言 ---

def test_native_supervisor_template_programs_and_ports():
    text = (BASE / "container" / "supervisord-native.conf.in").read_text(encoding="utf-8")
    for program in ("musicdl", "musicbox", "lxserver", "lxmusic", "webui"):
        assert f"[program:{program}]" in text
    # 与容器发布端口一致（proxy 契约不变）；lxserver 保持回环 8005
    for port in ("8768", "8770", "8772", "8774", "8005"):
        assert port in text
    assert "autostart=false" in text
    assert "FNMUSIC_ENV_FILE=\"@REPO@/.env\"" in text
    assert "WEBUI_SUPERVISORCTL=" in text
    for placeholder in ("@REPO@", "@VENV@", "@DATA@", "@RUN@"):
        assert placeholder in text


def test_native_unit_template_shape():
    text = (BASE / "fnmusic-sources-native.service.in").read_text(encoding="utf-8")
    assert "WorkingDirectory=@REPO@" in text
    assert "ExecStart=/bin/bash \"@REPO@/container/entrypoint.sh\"" in text
    assert 'Environment="SUPERVISOR_CONF=@RUN@/supervisord.conf"' in text
    assert 'Environment="FNMUSIC_ENV_FILE=@REPO@/.env"' in text
    assert 'Environment="DATA_PATH=@DATA@/lxserver"' in text


# ------------------------------------------------- install.sh 形态解析/迁移 ---

def test_resolve_deploy_mode_explicit_and_env_inherit(tmp_path):
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    funcs = (function(install, "run_docker") + function(install, "resolve_deploy_mode"))
    # 1) 显式 --deploy native 优先（机器上连 docker 命令都没有也成立）
    r = run_bash(
        f'{funcs}\nDEPLOY_MODE_CLI="native" NON_INTERACTIVE=1 resolve_deploy_mode; echo "$DEPLOY_MODE"',
        env={"BASE_DIR": str(tmp_path)},
    )
    assert r.returncode == 0 and r.stdout.strip().endswith("native")
    # 2) 未显式指定时沿用 .env 既有形态（升级/重装不悄悄换形态）
    (tmp_path / ".env").write_text("FNMUSIC_DEPLOY_MODE='native'\n", encoding="utf-8")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    (stub_bin / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub_bin / "docker").chmod(0o755)
    r = run_bash(
        f'{funcs}\nDEPLOY_MODE_CLI="" NON_INTERACTIVE=1 resolve_deploy_mode; echo "$DEPLOY_MODE"',
        env={"BASE_DIR": str(tmp_path), "PATH": f"{stub_bin}:{os.environ['PATH']}"},
    )
    assert r.returncode == 0 and r.stdout.strip().endswith("native")
    # 3) .env=docker + docker 可用 → docker
    (tmp_path / ".env").write_text("FNMUSIC_DEPLOY_MODE='docker'\n", encoding="utf-8")
    r = run_bash(
        f'{funcs}\nDEPLOY_MODE_CLI="" NON_INTERACTIVE=1 resolve_deploy_mode; echo "$DEPLOY_MODE"',
        env={"BASE_DIR": str(tmp_path), "PATH": f"{stub_bin}:{os.environ['PATH']}"},
    )
    assert r.returncode == 0 and r.stdout.strip().endswith("docker")


def test_resolve_deploy_mode_rejects_unknown_value(tmp_path):
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    funcs = (function(install, "run_docker") + function(install, "resolve_deploy_mode"))
    r = run_bash(
        f'log_err() {{ echo "$*" >&2; }}\n{funcs}\nDEPLOY_MODE_CLI="host" NON_INTERACTIVE=1 resolve_deploy_mode',
        env={"BASE_DIR": str(tmp_path)},
    )
    assert r.returncode != 0
    assert "native" in (r.stderr + r.stdout)


def test_migrate_lx_url_between_modes_docker_to_native(tmp_path):
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    func = function(install, "dotenv_escape") + function(install, "migrate_lx_url_between_modes")
    uploads = tmp_path / "sources-data" / "lxmusic" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "my.js").write_text("// src", encoding="utf-8")
    # 函数经 BASE_DIR/proxy/env_merge.py 改写 .env：tmp 仓库副本须带该工具
    (tmp_path / "proxy").mkdir()
    shutil.copy(BASE / "proxy" / "env_merge.py", tmp_path / "proxy" / "env_merge.py")
    env_path = tmp_path / ".env"
    env_path.write_text("LX_SOURCE_URL='file:///data/lxmusic/uploads/my.js'\n", encoding="utf-8")
    r = run_bash(
        f'{func}\nENV_PATH={tmp_path}/".env" DEPLOY_MODE=native migrate_lx_url_between_modes; '
        f'grep -F "LX_SOURCE_URL=" "{tmp_path}/.env"',
        env={"BASE_DIR": str(tmp_path)},
    )
    assert r.returncode == 0
    assert f"file://{tmp_path}/sources-data/lxmusic/uploads/my.js" in r.stdout


def test_migrate_lx_url_between_modes_keeps_missing_target_and_external(tmp_path):
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    func = function(install, "dotenv_escape") + function(install, "migrate_lx_url_between_modes")
    (tmp_path / "proxy").mkdir()
    shutil.copy(BASE / "proxy" / "env_merge.py", tmp_path / "proxy" / "env_merge.py")
    # 目标文件不存在（或外部 http URL）：一律保留原值
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LX_SOURCE_URL='file:///data/lxmusic/uploads/gone.js'\n"
        "LX_SOURCE_URL='https://example.com/s.js'\n",
        encoding="utf-8",
    )
    r = run_bash(
        f'{func}\nENV_PATH={tmp_path}/".env" DEPLOY_MODE=native migrate_lx_url_between_modes; '
        f'grep -F "LX_SOURCE_URL=" "{tmp_path}/.env"',
        env={"BASE_DIR": str(tmp_path)},
    )
    assert r.returncode == 0
    assert "https://example.com/s.js" in r.stdout
    assert "/sources-data/" not in r.stdout


# ------------------------------------------------- install_common.sh 渲染 ---

def test_render_native_supervisor_conf_placeholders(tmp_path):
    common = (BASE / "proxy" / "install_common.sh").read_text(encoding="utf-8")
    func = function(common, "native_venv_dir") + function(common, "native_run_dir") \
        + function(common, "native_sup_conf") + function(common, "render_native_placeholders") \
        + function(common, "render_native_supervisor_conf")
    (tmp_path / "container").mkdir()
    shutil.copy(BASE / "container" / "supervisord-native.conf.in",
                tmp_path / "container" / "supervisord-native.conf.in")
    r = run_bash(
        f'{func}\nrender_native_supervisor_conf && cat "$(native_sup_conf)"',
        env={"BASE_DIR": str(tmp_path)},
    )
    assert r.returncode == 0
    out = r.stdout
    assert "@REPO@" not in out and "@VENV@" not in out and "@DATA@" not in out and "@RUN@" not in out
    assert f"directory={tmp_path}/musicdl-service" in out
    assert f"{tmp_path}/.venv-sources/bin/uvicorn" in out
    assert f"directory={tmp_path}/.lxserver" in out
    assert f"WEBUI_SUPERVISORCTL=\"supervisorctl -c '{tmp_path}/sources-native/supervisord.conf'\"" in out
    # 渲染产物落在 sources-native/ 下
    assert (tmp_path / "sources-native" / "supervisord.conf").is_file()


# ------------------------------------------------- ensure_sources_native.sh ---

def _write_stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(0o755)


def test_ensure_sources_native_lxserver_provision_idempotent(tmp_path):
    repo = tmp_path / "repo"
    (repo / "container" / "lxserver-artifact").mkdir(parents=True)
    # BASE_DIR 取自脚本自身位置：脚本须复制进 tmp 仓库再运行（与真实布局一致）
    shutil.copy(BASE / "ensure_sources_native.sh", repo / "ensure_sources_native.sh")
    for req in ("musicdl-service", "musicbox-service", "lxmusic-service", "webui-service"):
        (repo / req).mkdir()
        (repo / req / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    # 预置包：zip 布局对齐真实 v2.1.2（根级 index.js 为运行入口；
    # server/server.js 存在时应打 getLyric Promise 补丁）
    original_line = 'const result = await musicSdk[source].getLyric(songInfo);'
    artifact = repo / "container" / "lxserver-artifact" / "lx-test-server.zip"
    with zipfile.ZipFile(artifact, "w") as zf:
        zf.writestr("lx-music-sync-server/index.js", "#!/usr/bin/env node\nrequire('./server/server');\n")
        zf.writestr("lx-music-sync-server/server/server.js", f"#!/usr/bin/env node\n{original_line}\n")
    import hashlib
    sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (repo / "container" / "lxserver.version").write_text(
        f"LXSERVER_TAG=vtest\nLXSERVER_ZIP_NAME=lx-test-server.zip\nLXSERVER_SHA256={sha}\n",
        encoding="utf-8",
    )
    # venv 桩：脚本只探测 python/pip 可执行并调用 pip install（成功即可）
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (venv / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for f in ("python", "pip"):
        (venv / "bin" / f).chmod(0o755)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_stub(stub_bin, "node", 'case "$1" in -p) echo 20 ;; *) echo "v20.0.0" ;; esac\n')
    _write_stub(stub_bin, "ffmpeg", 'echo "ffmpeg version 6.0" ; exit 0\n')
    env = {
        "BASE_DIR": str(repo),
        "FNMUSIC_VENV_SOURCES_DIR": str(venv),
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
    }
    r = run_bash(f'bash "{repo}/ensure_sources_native.sh"', env=env)
    assert r.returncode == 0, r.stderr
    server_js = repo / ".lxserver" / "server" / "server.js"
    assert server_js.is_file()
    assert (repo / ".lxserver" / "index.js").is_file()
    patched = server_js.read_text(encoding="utf-8")
    assert "const result = await (_res && _res.promise ? _res.promise : _res);" in patched
    assert (repo / ".lxserver" / ".provisioned-version").read_text().strip() == "vtest"
    # 真实包允许主脚本嵌套；幂等判定必须使用根入口而非 server/server.js。
    server_js.unlink()
    # 幂等：同版本重跑直接跳过（不重新解压）
    r2 = run_bash(f'bash "{repo}/ensure_sources_native.sh"', env=env)
    assert r2.returncode == 0, r2.stderr
    assert "已就位" in r2.stdout


def test_ensure_sources_native_rejects_bad_sha256(tmp_path):
    repo = tmp_path / "repo"
    (repo / "container" / "lxserver-artifact").mkdir(parents=True)
    shutil.copy(BASE / "ensure_sources_native.sh", repo / "ensure_sources_native.sh")
    for req in ("musicdl-service", "musicbox-service", "lxmusic-service", "webui-service"):
        (repo / req).mkdir()
        (repo / req / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    artifact = repo / "container" / "lxserver-artifact" / "lx-test-server.zip"
    with zipfile.ZipFile(artifact, "w") as zf:
        zf.writestr("lx-music-sync-server/server/server.js", "x")
    (repo / "container" / "lxserver.version").write_text(
        "LXSERVER_TAG=vtest\nLXSERVER_ZIP_NAME=lx-test-server.zip\n"
        "LXSERVER_SHA256=0000000000000000000000000000000000000000000000000000000000000000\n",
        encoding="utf-8",
    )
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (venv / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for f in ("python", "pip"):
        (venv / "bin" / f).chmod(0o755)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_stub(stub_bin, "node", 'case "$1" in -p) echo 20 ;; *) echo v20 ;; esac\n')
    _write_stub(stub_bin, "ffmpeg", "exit 0\n")
    r = run_bash(f'bash "{repo}/ensure_sources_native.sh"', env={
        "BASE_DIR": str(repo),
        "FNMUSIC_VENV_SOURCES_DIR": str(venv),
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
    })
    assert r.returncode != 0
    assert "校验和不符" in (r.stderr + r.stdout)
    assert not (repo / ".lxserver").exists()


def test_migrate_lx_url_native_to_docker_checks_host_file(tmp_path):
    text = (BASE / "install.sh").read_text()
    func = function(text, "migrate_lx_url_between_modes")
    uploads = tmp_path / "sources-data" / "lxmusic" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "local.js").write_text("// source")
    (tmp_path / "proxy").mkdir()
    shutil.copy(BASE / "proxy" / "env_merge.py", tmp_path / "proxy" / "env_merge.py")
    env_path = tmp_path / ".env"
    env_path.write_text(f"LX_SOURCE_URL='file://{uploads}/local.js'\n")
    r = run_bash(f'{func}\ndotenv_escape() {{ printf "%s" "$1"; }}; log_info() {{ :; }}; '
                 f'ENV_PATH="{env_path}" DEPLOY_MODE=docker migrate_lx_url_between_modes',
                 env={"BASE_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert "file:///data/lxmusic/uploads/local.js" in env_path.read_text()


@pytest.mark.parametrize("adopt", [0, 1])
def test_install_native_unit_foreign_owner_requires_explicit_adopt(tmp_path, adopt):
    install = (BASE / "install.sh").read_text()
    src = tmp_path / "new-unit"
    dest = tmp_path / "old-unit"
    src.write_text("unit")
    dest.write_text("WorkingDirectory=/old/checkout\n")
    script = function(install, "install_unit") + '''
log_err() { printf '%s\\n' "$*" >&2; }
log_warn() { :; }
unit_working_dir() { printf '/old/checkout'; }
same_dir() { [ "$1" = "$2" ]; }
sudo() { printf 'mock-sudo %s\\n' "$*"; }
'''
    result = run_bash(script + f"install_unit {shlex.quote(str(src))} {shlex.quote(str(dest))}",
                      env={"BASE_DIR": str(tmp_path), "ADOPT": str(adopt)})
    assert result.returncode == (0 if adopt else 1), result.stderr
    assert ("mock-sudo cp" in result.stdout) == bool(adopt)
    assert ("mock-sudo systemctl restart" in result.stdout) == bool(adopt)
    assert dest.read_text() == "WorkingDirectory=/old/checkout\n"


@pytest.mark.parametrize("name", ["Music & Tools", "Music | Library", "Music 100%"])
def test_native_render_special_paths_and_webui_command(tmp_path, monkeypatch, name):
    repo = tmp_path / name
    (repo / "container").mkdir(parents=True)
    shutil.copy(BASE / "container/supervisord-native.conf.in",
                repo / "container/supervisord-native.conf.in")
    shutil.copy(BASE / "fnmusic-sources-native.service.in", repo / "unit.in")
    common = (BASE / "proxy/install_common.sh").read_text()
    funcs = "".join(function(common, n) for n in (
        "native_venv_dir", "native_run_dir", "native_sup_conf",
        "render_native_placeholders", "render_native_supervisor_conf"))
    result = run_bash(funcs + '\nrender_native_supervisor_conf\n', env={"BASE_DIR": str(repo)})
    assert result.returncode == 0, result.stderr
    conf = configparser.ConfigParser()
    conf.read(repo / "sources-native/supervisord.conf")
    assert conf["program:musicdl"]["directory"] == str(repo / "musicdl-service")
    assert shlex.split(conf["program:musicdl"]["command"])[0] == str(repo / ".venv-sources/bin/uvicorn")
    env = conf["program:webui"]["environment"]
    ctl = env.split('WEBUI_SUPERVISORCTL="', 1)[1].rsplit('"', 1)[0]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "supervisorctl"
    stub.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setitem(webui.CONF, "supervisorctl", ctl)
    code, out = webui.supervisorctl("status", "musicdl")
    assert code == 0, out
    assert json.loads(out) == ["-c", str(repo / "sources-native/supervisord.conf"), "status", "musicdl"]
    result = run_bash(funcs + '\nrender_native_placeholders "$BASE_DIR/unit.in"',
                      env={"BASE_DIR": str(repo)})
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        if line.startswith("Environment="):
            assignments = shlex.split(line.partition("=")[2])
            assert len(assignments) == 1
            if assignments[0].startswith("FNMUSIC_ENV_FILE="):
                assert assignments[0].replace("%%", "%") == f"FNMUSIC_ENV_FILE={repo}/.env"
    assert "@REPO@" not in result.stdout
    unit = repo / "rendered.service"
    unit.write_text(result.stdout)
    result = run_bash(function(common, "unit_working_dir") + '\nunit_working_dir "$BASE_DIR/rendered.service"',
                      env={"BASE_DIR": str(repo)})
    assert result.returncode == 0 and result.stdout.strip() == str(repo)


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_migrate_lx_lists_and_open_sources_between_modes(tmp_path, mode):
    from proxy.env_merge import parse_env_file, render_env

    repo = tmp_path / "Music Library"
    (repo / "proxy").mkdir(parents=True)
    shutil.copy(BASE / "proxy/env_merge.py", repo / "proxy/env_merge.py")
    relative = ["lxmusic/uploads/legacy.js", "lxserver/users/source/_open/My Source"]
    for rel in relative:
        path = repo / "sources-data" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// source")
    old = ([f"file:///data/{rel}" for rel in relative] if mode == "native" else
           [f"file:///old/checkout/sources-data/{rel}" for rel in relative])
    items = [{"name": "legacy", "url": old[0], "active": False},
             {"name": "uploaded", "url": old[1], "active": True},
             {"name": "external", "url": "https://example.com/a.js", "active": True},
             {"name": "missing", "url": "file:///data/lxmusic/uploads/missing.js", "active": False}]
    env_path = repo / ".env"
    env_path.write_text(render_env([("LX_SOURCE_URL", old[1]),
                                   ("LX_SOURCE_LIST", json.dumps(items)), ("SECRET", "keep")],
                                  trailing=["# custom note"]))
    script = function((BASE / "install.sh").read_text(), "migrate_lx_url_between_modes")
    result = run_bash(script + '\nmigrate_lx_url_between_modes',
                      env={"BASE_DIR": str(repo), "ENV_PATH": str(env_path), "DEPLOY_MODE": mode})
    assert result.returncode == 0, result.stderr
    values = dict(parse_env_file(env_path)[0])
    expected = ([(repo / "sources-data" / rel).as_uri() for rel in relative] if mode == "native" else
                ["file:///data/" + rel.replace(" ", "%20") for rel in relative])
    assert values["LX_SOURCE_URL"] == expected[1]
    after = json.loads(values["LX_SOURCE_LIST"])
    assert [i["url"] for i in after[:2]] == expected
    assert [i["active"] for i in after] == [False, True, True, False]
    assert after[2:] == items[2:]
    assert values["SECRET"] == "keep" and "# custom note" in env_path.read_text()
    before = env_path.read_bytes()
    result = run_bash(script + '\nmigrate_lx_url_between_modes',
                      env={"BASE_DIR": str(repo), "ENV_PATH": str(env_path), "DEPLOY_MODE": mode})
    assert result.returncode == 0, result.stderr
    assert env_path.read_bytes() == before


@pytest.mark.parametrize("adopt", [False, True])
def test_restore_adopt_parser_drives_foreign_native_cleanup(tmp_path, adopt):
    text = (BASE / "restore.sh").read_text()
    parser = text[text.index("FULL_RESTORE=0"):text.index("\nlog_info()")]
    start = text.index("for unit in fnmusic-musicdl fnmusic-musicbox fnmusic-lxmusic fnmusic-sources; do")
    cleanup = text[start:text.index("\ndone", start) + len("\ndone")]
    mocks = '''
remove_owned_container() { printf 'remove %s\\n' "$*"; }
stop_owned_source_unit() { printf 'stop %s\\n' "$*"; }
owned_source_unit() { return 1; }
sudo() { printf 'mock-sudo %s\\n' "$*"; }
'''
    script = "set -euo pipefail\n" + parser + mocks + cleanup
    result = subprocess.run([BASH, "-c", script, "review"] + (["--adopt"] if adopt else []),
                            env={**os.environ, "ADOPT": "1"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert ("stop fnmusic-sources --adopt" in result.stdout) == adopt
    assert ("mock-sudo rm -f /etc/systemd/system/fnmusic-sources.service" in result.stdout) == adopt
    assert ("remove fnmusic-sources --adopt" in result.stdout) == adopt
