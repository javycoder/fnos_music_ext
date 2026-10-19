"""issue #70：应用镜像构建指纹与复用行为测试（临时仓库 + docker/探测桩）。

- build_fingerprint.py：指纹随构建输入（服务源码/依赖/静态资源/Dockerfile/
  .dockerignore/离线构建包/compose 构建参数）变化；不随运行配置、文档、测试
  文件或修改时间变化；参数插值优先级与 compose 一致（环境变量 > .env > 默认值）。
- install_common.sh ensure_image_current：指纹一致时复用本地应用镜像——不执行
  基础镜像探测、不 pull、不构建（删除 python 基础镜像后重启不再拉回）；镜像
  缺失、旧镜像无指纹标签或构建输入变化时探测后重建；探测改写 .env 后按新值
  重算指纹，不被启动脚本已导出的旧值遮蔽；探测/构建失败原样报错且不误标成功。
除真实 Compose 的只读 config 测试外，docker 与 ensure_base_image.sh 全部桩化；不触碰宿主镜像、网络与运行中容器。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
BASH = Path("/bin/bash")

_spec = importlib.util.spec_from_file_location("bf", BASE / "proxy/build_fingerprint.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

COMPOSE = """\
services:
  fnmusic-sources:
    build:
      context: .
      dockerfile: container/Dockerfile
      labels:
        - "io.fnmusic-ext.build-fingerprint=${FNMUSIC_BUILD_FINGERPRINT:-}"
      args:
        BASE_IMAGE: ${FNMUSIC_BASE_IMAGE:-python:3.13-slim}
        PIP_INDEX_URL: ${FNMUSIC_PIP_INDEX:-https://mirrors.tencent.com/pypi/simple/}
        APT_MIRROR: ${FNMUSIC_APT_MIRROR:-https://mirrors.tencent.com}
    image: fnmusic-sources:latest
"""
DOCKERFILE = """\
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}
COPY musicdl-service/app.py musicdl-service/requirements.txt /srv/
COPY webui-service/static/ /srv/static/
COPY container/lxserver-artifact/ /srv/lxserver-artifact/
"""
DOCKERIGNORE = """\
*
!musicdl-service/
!webui-service/
!container/
**/__pycache__/
**/*.md
**/test_*.py
"""


# A deliberately small Compose interpolation stub for shell unit tests only.
# Production exclusively consumes Docker's resolved JSON; real Compose tests below
# cover precedence independently of this fixture.
RESOLVER = r'''import json, os, re, sys, yaml
from pathlib import Path
repo = Path(sys.argv[1])
env = {}
if (repo / ".env").exists():
    for line in (repo / ".env").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep: env[key] = value.strip("\"'")
env.update(os.environ)
text = (repo / "docker-compose.yml").read_text()
text = re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m[1]) or m[2], text)
print(json.dumps(yaml.safe_load(text)))
'''


def resolved_config(repo):
    result = subprocess.run([sys.executable, str(repo / "resolve_config.py"), str(repo)],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def compute(repo):
    return bf.compute(repo, resolved_config(repo))


def make_repo(path: Path) -> Path:
    (path / "container/lxserver-artifact").mkdir(parents=True)
    (path / "proxy").mkdir()
    (path / "musicdl-service").mkdir()
    (path / "webui-service/static").mkdir(parents=True)
    (path / "resolve_config.py").write_text(RESOLVER)
    (path / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    (path / "container/Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (path / ".dockerignore").write_text(DOCKERIGNORE, encoding="utf-8")
    (path / "musicdl-service/app.py").write_text("print('musicdl')\n", encoding="utf-8")
    (path / "musicdl-service/requirements.txt").write_text("musicdl\n", encoding="utf-8")
    (path / "webui-service/static/app.js").write_text("console.log(1)\n", encoding="utf-8")
    (path / "container/lxserver-artifact/lxserver.zip").write_bytes(b"zip-bytes-v1")
    # shell 侧 compute_build_fingerprint 会执行 ${BASE_DIR}/proxy/build_fingerprint.py
    shutil.copy2(BASE / "proxy/build_fingerprint.py",
                 path / "proxy/build_fingerprint.py")
    return path


@pytest.fixture(autouse=True)
def clean_build_env(monkeypatch):
    for key in ("FNMUSIC_BASE_IMAGE", "FNMUSIC_PIP_INDEX", "FNMUSIC_APT_MIRROR",
                "FNMUSIC_BUILD_FINGERPRINT"):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------------- 指纹工具 ---

def test_args_interpolation_env_over_dotenv_over_default(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    assert resolved_config(repo)["services"]["fnmusic-sources"]["build"]["args"]["BASE_IMAGE"] == "python:3.13-slim"
    (repo / ".env").write_text("FNMUSIC_BASE_IMAGE=mirror/python:3.13-slim\n")
    assert resolved_config(repo)["services"]["fnmusic-sources"]["build"]["args"]["BASE_IMAGE"] == "mirror/python:3.13-slim"
    monkeypatch.setenv("FNMUSIC_BASE_IMAGE", "")
    assert resolved_config(repo)["services"]["fnmusic-sources"]["build"]["args"]["BASE_IMAGE"] == "python:3.13-slim"


@pytest.mark.parametrize("path,content", [
    ("musicdl-service/app.py", "changed"),
    ("musicdl-service/requirements.txt", "changed"),
    ("webui-service/static/app.js", "changed"),
    ("container/lxserver-artifact/lxserver.zip", "changed"),
    ("container/Dockerfile", DOCKERFILE + "# layer tweak\n"),
    (".dockerignore", DOCKERIGNORE + "**/docs/\n"),
    (".env", "FNMUSIC_BASE_IMAGE=mirror/python:3.13-slim\n"),
])
def test_fingerprint_tracks_build_inputs(tmp_path, path, content):
    repo = make_repo(tmp_path / "repo")
    baseline = compute(repo)
    (repo / path).write_text(content)
    assert compute(repo) != baseline


def test_fingerprint_ignores_runtime_and_unrelated_changes(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / ".env").write_text(
        "FNMUSIC_MUSICDL_ENABLED=true\nLX_SOURCES='kg,wy'\n", encoding="utf-8")
    baseline = compute(repo)

    (repo / "docker-compose.yml").write_text(COMPOSE + "# comment\n")
    config = resolved_config(repo)
    config["services"]["fnmusic-sources"]["environment"] = {"RUNTIME": "changed"}
    assert bf.compute(repo, config) == baseline
    config["services"]["fnmusic-sources"]["build"]["labels"] = {bf.FINGERPRINT_LABEL: "anything"}
    assert bf.compute(repo, config) == baseline
    # 运行开关只影响进程集（重启容器生效），不参与镜像构建
    (repo / ".env").write_text(
        "FNMUSIC_MUSICDL_ENABLED=false\nLX_SOURCES='mg'\n", encoding="utf-8")
    assert compute(repo) == baseline
    # 未被 COPY 进镜像的文件：文档 / 测试 / 仓库外目录
    (repo / "docs").mkdir()
    (repo / "docs/INSTALL.md").write_text("doc\n", encoding="utf-8")
    (repo / "proxy_tests").mkdir()
    (repo / "proxy_tests/test_new.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert compute(repo) == baseline
    # .dockerignore 排除的类型（构建上下文不含 → 镜像不变 → 指纹不变）
    (repo / "webui-service/static/README.md").write_text("note\n", encoding="utf-8")
    (repo / "webui-service/static/test_wiring.py").write_text("pass\n", encoding="utf-8")
    (repo / "webui-service/static/__pycache__").mkdir()
    (repo / "webui-service/static/__pycache__/app.cpython-311.pyc").write_bytes(b"\x00")
    assert compute(repo) == baseline
    # 只改修改时间（重写同内容）不影响指纹
    app = repo / "musicdl-service/app.py"
    content = app.read_text(encoding="utf-8")
    os.utime(app, (1, 1))
    app.write_text(content, encoding="utf-8")
    assert compute(repo) == baseline


def test_fingerprint_immune_to_label_env_and_stable(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    baseline = compute(repo)
    monkeypatch.setenv("FNMUSIC_BUILD_FINGERPRINT", "should-not-self-reference")
    assert compute(repo) == baseline
    assert compute(repo) == compute(repo)


def test_copy_source_outside_repository_rejected(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "container/Dockerfile").write_text(
        "FROM python:3.13-slim\nCOPY ../outside.txt /srv/\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes repository"):
        compute(repo)


def test_cli_reports_missing_dockerfile(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    result = subprocess.run(
        [sys.executable, str(BASE / "proxy/build_fingerprint.py"), "--repo", str(repo)],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "cannot compute build fingerprint" in result.stderr


# --------------------------------------------------- ensure_image_current ---

def function(text: str, name: str) -> str:
    start = text.index(name + "() {")
    return text[start:text.index("\n}", start) + 2] + "\n"


def common_functions() -> str:
    text = (BASE / "proxy/install_common.sh").read_text(encoding="utf-8")
    return "".join(function(text, name) for name in (
        "compute_build_fingerprint", "app_image_fingerprint", "env_value",
        "ensure_image_current"))


STUB = """\
set -uo pipefail
BASE_DIR=__REPO__
APP_IMAGE=fnmusic-sources:latest
BUILD_FINGERPRINT_LABEL=io.fnmusic-ext.build-fingerprint
PROBE_LOG=__PROBE__
DOCKER_LOG=__DOCKER__
LABEL_FILE=__LABEL__
export BASE_DIR PROBE_LOG DOCKER_LOG LABEL_FILE
log_info() { printf 'info %s\\n' "$*"; }
log_warn() { printf 'warn %s\\n' "$*"; }
log_err() { printf 'err %s\\n' "$*" >&2; }
run_docker() {
  printf 'run_docker %s\\n' "$*" >> "${DOCKER_LOG}"
  case "$1" in
    image)
      [ -f "${LABEL_FILE}" ] || return 1
      cat "${LABEL_FILE}"
      return 0 ;;
    compose)
      if [ "${4-}" = config ]; then
        python3 "${BASE_DIR}/resolve_config.py" "${BASE_DIR}"
        return "${CONFIG_RC:-0}"
      fi
      if [ "${COMPOSE_RC:-0}" = 0 ] && [ "${SKIP_LABEL:-0}" = 0 ] && [ "${6-}" = --build ]; then
        printf '%s' "${FNMUSIC_BUILD_FINGERPRINT}" > "${LABEL_FILE}"
      fi
      printf 'compose-args base=%s fp=%s\\n' "${FNMUSIC_BASE_IMAGE-}" "${FNMUSIC_BUILD_FINGERPRINT-}" >> "${DOCKER_LOG}"
      return "${COMPOSE_RC:-0}" ;;
  esac
  return 0
}
"""


def stub_probe(repo: Path, chosen_ref: str | None) -> None:
    # 探测桩：记录入参（手动 BASE_IMAGE / 镜像候选链透传），成功则把选定引用写进 .env
    (repo / "ensure_base_image.sh").write_text(f"""\
#!/usr/bin/env bash
printf 'probe base_image=%s mirrors=%s\\n' "${{BASE_IMAGE-}}" "${{FNMUSIC_DOCKER_MIRRORS-}}" >> "${{PROBE_LOG}}"
[ "${{PROBE_RC:-0}}" -eq 0 ] || exit 1
printf "FNMUSIC_BASE_IMAGE='{chosen_ref}'\\n" >> "${{BASE_DIR}}/.env"
""", encoding="utf-8")


def run_ensure(repo: Path, env_extra: dict | None = None):
    label = repo / "label.txt"
    log = repo / "docker.log"
    probe = repo / "probe.log"
    # label.txt 由各用例自行准备（模拟本地镜像标签）；只重置调用日志
    for path in (log, probe):
        path.unlink(missing_ok=True)
    script = (STUB.replace("__REPO__", str(repo)).replace("__PROBE__", str(probe))
              .replace("__DOCKER__", str(log)).replace("__LABEL__", str(label)))
    script += common_functions() + "\nensure_image_current\n"
    env = {k: v for k, v in os.environ.items()
           if k not in ("FNMUSIC_BASE_IMAGE", "FNMUSIC_PIP_INDEX", "FNMUSIC_APT_MIRROR",
                        "FNMUSIC_BUILD_FINGERPRINT", "BASE_IMAGE")}
    env.update({k: str(v) for k, v in (env_extra or {}).items()})
    result = subprocess.run([str(BASH), "-c", script], env=env,
                            capture_output=True, text=True)
    docker_log = log.read_text(encoding="utf-8") if log.exists() else ""
    probe_log = probe.read_text(encoding="utf-8") if probe.exists() else ""
    return result, docker_log, probe_log


def label_of(repo: Path) -> Path:
    return repo / "label.txt"


def test_matching_fingerprint_reuses_image_without_probe(tmp_path):
    """issue #70 核心：指纹一致 → 不探测基础镜像（删除 python 镜像后也不拉回）。"""
    repo = make_repo(tmp_path / "repo")
    stub_probe(repo, "docker.m.daocloud.io/library/python:3.13-slim")
    (repo / ".env").write_text(
        "FNMUSIC_BASE_IMAGE='docker.m.daocloud.io/library/python:3.13-slim'\n",
        encoding="utf-8")
    label_of(repo).write_text(compute(repo), encoding="utf-8")

    result, docker_log, probe_log = run_ensure(repo)

    assert result.returncode == 0, result.stderr
    assert probe_log == ""  # 未执行基础镜像探测（也就不可能 pull）
    compose_lines = [l for l in docker_log.splitlines() if l.startswith("run_docker compose") and " config " not in l]
    assert compose_lines == [
        f"run_docker compose -f {repo}/docker-compose.yml up -d --no-build --pull never"]
    assert "--build" not in docker_log
    # 复用路径不传构建参数（--no-build 用不到），但指纹必须与镜像标签一致
    assert f"fp={compute(repo)}" in docker_log


@pytest.mark.parametrize("label_content", ["old-fingerprint", ""],
                         ids=["stale-label", "legacy-image-without-label"])
def test_stale_or_missing_label_probes_then_rebuilds(tmp_path, label_content):
    """镜像缺失 / 旧镜像无指纹 / 指纹过期：探测后重建，重建参数取 .env 的新引用。"""
    repo = make_repo(tmp_path / "repo")
    chosen = "docker.m.daocloud.io/library/python:3.13-slim"
    stub_probe(repo, chosen)
    label_of(repo).write_text(label_content, encoding="utf-8")

    result, docker_log, probe_log = run_ensure(repo)

    assert result.returncode == 0, result.stderr
    assert "probe base_image= mirrors=" in probe_log
    assert f"run_docker compose -f {repo}/docker-compose.yml up -d --build" in docker_log
    # 探测选定的引用进入 .env 后同步给 compose（不被启动环境旧值遮蔽）
    assert f"compose-args base={chosen}" in docker_log
    expected_fp = compute(repo)
    assert f"compose-args base={chosen} fp={expected_fp}" in docker_log


def test_probe_failure_blocks_any_compose_call(tmp_path):
    repo = make_repo(tmp_path / "repo")
    stub_probe(repo, "docker.m.daocloud.io/library/python:3.13-slim")
    result, docker_log, _ = run_ensure(repo, {"PROBE_RC": 1})
    assert result.returncode == 1
    assert " up " not in docker_log
    assert "基础镜像源探测失败" in result.stderr


def test_build_failure_propagates_and_keeps_old_label(tmp_path):
    """构建失败：原样返回非零，不更新镜像指纹（旧镜像不被误标为已更新）。"""
    repo = make_repo(tmp_path / "repo")
    stub_probe(repo, "docker.m.daocloud.io/library/python:3.13-slim")
    result, docker_log, _ = run_ensure(repo, {"COMPOSE_RC": 1})
    assert result.returncode == 1
    assert "up -d --build" in docker_log
    assert "run_docker compose -f" in docker_log


def test_manual_base_image_override_skips_reuse(tmp_path):
    """手动 BASE_IMAGE 与 .env 缓存不同 = 构建输入变化：不偷懒复用，走探测重建。"""
    repo = make_repo(tmp_path / "repo")
    cached = "docker.m.daocloud.io/library/python:3.13-slim"
    stub_probe(repo, "docker.1ms.run/library/python:3.13-slim")
    (repo / ".env").write_text(f"FNMUSIC_BASE_IMAGE='{cached}'\n", encoding="utf-8")
    label_of(repo).write_text(compute(repo), encoding="utf-8")  # 与缓存一致也会被跳过

    result, docker_log, probe_log = run_ensure(
        repo, {"BASE_IMAGE": "docker.1ms.run/library/python:3.13-slim"})

    assert result.returncode == 0, result.stderr
    assert "probe base_image=docker.1ms.run/library/python:3.13-slim" in probe_log
    assert "up -d --build" in docker_log
    assert "--pull never" not in docker_log


def test_probe_refresh_not_masked_by_stale_exported_base(tmp_path):
    """探测更新 .env 后必须重读：启动脚本早先 source 进环境的旧值不得遮蔽新引用。"""
    repo = make_repo(tmp_path / "repo")
    stale = "docker.m.daocloud.io/library/python:3.13-slim"
    fresh = "docker.1ms.run/library/python:3.13-slim"
    stub_probe(repo, fresh)
    # 镜像已按 fresh 构建好；.env 暂存 stale；环境里还残留 stale（启动时 source 的旧值）
    env_file = repo / ".env"
    env_file.write_text(f"FNMUSIC_BASE_IMAGE='{fresh}'\n", encoding="utf-8")
    label_fp = compute(repo)
    label_of(repo).write_text(label_fp, encoding="utf-8")
    env_file.write_text(f"FNMUSIC_BASE_IMAGE='{stale}'\n", encoding="utf-8")

    result, docker_log, probe_log = run_ensure(repo, {"FNMUSIC_BASE_IMAGE": stale})

    assert result.returncode == 0, result.stderr
    assert probe_log  # 首轮指纹（stale）与镜像（fresh）不一致 → 需要探测
    compose_lines = [l for l in docker_log.splitlines() if l.startswith("run_docker compose") and " config " not in l]
    # 探测把 fresh 写入 .env 并重算指纹 → 命中镜像标签 → 复用而非重建
    assert compose_lines == [
        f"run_docker compose -f {repo}/docker-compose.yml up -d --no-build --pull never"]
    assert f"compose-args base={fresh} fp={label_fp}" in docker_log
    assert "--build" not in docker_log


# ------------------------------------------------- extend.sh docker 分支 ---

EXTEND_STUB = """\
set -uo pipefail
CONTAINER_NAME=fnmusic-sources
BASE_DIR=__REPO__
APP_IMAGE=fnmusic-sources:latest
BUILD_FINGERPRINT_LABEL=io.fnmusic-ext.build-fingerprint
PROBE_LOG=__PROBE__
DOCKER_LOG=__DOCKER__
LABEL_FILE=__LABEL__
export BASE_DIR PROBE_LOG DOCKER_LOG LABEL_FILE
ADOPT=0
log_info() { printf 'info %s\\n' "$*"; }
log_warn() { printf 'warn %s\\n' "$*"; }
log_err() { printf 'err %s\\n' "$*" >&2; }
run_docker() {
  printf 'run_docker %s\\n' "$*" >> "${DOCKER_LOG}"
  case "$1" in
    container) return "${CONTAINER_EXISTS:-0}" ;;  # 0=存在 1=缺失
    image)
      [ -f "${LABEL_FILE}" ] || return 1
      cat "${LABEL_FILE}"
      return 0 ;;
    inspect)
      case "$3" in
        '{{.Image}}') printf 'sha256:img' ;;
        *) printf '%s' "${STARTED_AT:?}" ;;
      esac
      return 0 ;;
    compose)
      if [ "${4-}" = config ]; then
        python3 "${BASE_DIR}/resolve_config.py" "${BASE_DIR}"
        return "${CONFIG_RC:-0}"
      fi
      if [ "${COMPOSE_RC:-0}" = 0 ] && [ "${SKIP_LABEL:-0}" = 0 ] && [ "${6-}" = --build ]; then
        printf '%s' "${FNMUSIC_BUILD_FINGERPRINT}" > "${LABEL_FILE}"
      fi
      printf 'compose-args fp=%s\\n' "${FNMUSIC_BUILD_FINGERPRINT-}" >> "${DOCKER_LOG}"
      return "${COMPOSE_RC:-0}" ;;
    restart) return 0 ;;
  esac
  return 0
}
reclaim_container() { printf 'reclaim %s\\n' "$*" >> "${DOCKER_LOG}"; }
"""


def extend_parts() -> str:
    extend = (BASE / "extend.sh").read_text(encoding="utf-8")
    common = (BASE / "proxy/install_common.sh").read_text(encoding="utf-8")
    start = extend.index('if [ "${need_start}" -eq 0 ]; then')
    end = extend.index('\nfi',
                       extend.index('run_docker restart "${CONTAINER_NAME}"', start)) + 3
    block = extend[start:end]
    return (function(extend, "ensure_sources_container") + common_functions()
            + function(common, "env_newer_than_container") + block)


def run_extend(repo: Path, env_extra: dict | None = None):
    log = repo / "docker.log"
    probe = repo / "probe.log"
    for path in (log, probe):
        path.unlink(missing_ok=True)
    script = (EXTEND_STUB.replace("__REPO__", str(repo))
              .replace("__PROBE__", str(probe)).replace("__DOCKER__", str(log))
              .replace("__LABEL__", str(repo / "label.txt")))
    script += extend_parts() + "\n"
    env = {k: v for k, v in os.environ.items()
           if k not in ("FNMUSIC_BASE_IMAGE", "FNMUSIC_PIP_INDEX", "FNMUSIC_APT_MIRROR",
                        "FNMUSIC_BUILD_FINGERPRINT", "BASE_IMAGE")}
    env.update({k: str(v) for k, v in (env_extra or {}).items()})
    result = subprocess.run([str(BASH), "-c", script], env=env,
                            capture_output=True, text=True)
    docker_log = log.read_text(encoding="utf-8") if log.exists() else ""
    probe_log = probe.read_text(encoding="utf-8") if probe.exists() else ""
    return result, docker_log, probe_log


def prepared_repo(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    stub_probe(repo, "docker.m.daocloud.io/library/python:3.13-slim")
    (repo / ".env").write_text(
        "FNMUSIC_BASE_IMAGE='docker.m.daocloud.io/library/python:3.13-slim'\n"
        "FNMUSIC_MUSICDL_ENABLED=true\n", encoding="utf-8")
    label_of(repo).write_text(compute(repo), encoding="utf-8")
    return repo


def test_extend_healthy_container_reuses_image_without_restart(tmp_path):
    """容器健康 + 指纹匹配：复用镜像，不探测不构建，也不重启容器。"""
    repo = prepared_repo(tmp_path)
    result, docker_log, probe_log = run_extend(
        repo, {"need_start": 0, "CONTAINER_EXISTS": 0,
               "STARTED_AT": "2100-01-01T00:00:00Z"})
    assert result.returncode == 0, result.stderr
    assert probe_log == ""
    assert "reclaim fnmusic-sources" in docker_log
    assert "up -d --no-build --pull never" in docker_log
    assert "--build" not in docker_log
    assert "restart" not in docker_log


def test_extend_env_newer_restarts_container_on_reuse(tmp_path):
    """.env 比容器新且镜像未变：复用之外还需 restart 让 entrypoint 重读开关。"""
    repo = prepared_repo(tmp_path)
    result, docker_log, probe_log = run_extend(
        repo, {"need_start": 0, "CONTAINER_EXISTS": 0,
               "STARTED_AT": "2000-01-01T00:00:00Z"})
    assert result.returncode == 0, result.stderr
    assert probe_log == ""
    assert "up -d --no-build --pull never" in docker_log
    assert "restart fnmusic-sources" in docker_log


def test_extend_stopped_container_reuses_and_restarts(tmp_path):
    """容器已停止但应用镜像指纹匹配：直接复用镜像拉起容器并重启对齐进程集。"""
    repo = prepared_repo(tmp_path)
    result, docker_log, probe_log = run_extend(
        repo, {"need_start": 1, "STARTED_AT": "2100-01-01T00:00:00Z"})
    assert result.returncode == 0, result.stderr
    assert probe_log == ""
    assert "up -d --no-build --pull never" in docker_log
    assert "restart fnmusic-sources" in docker_log
    assert "--build" not in docker_log


def test_extend_healthy_but_missing_container_enters_ensure_flow(tmp_path):
    """健康端口在响应但容器缺失：进入容器保障流程（复用镜像创建容器），不再仅告警。"""
    repo = prepared_repo(tmp_path)
    result, docker_log, probe_log = run_extend(
        repo, {"need_start": 0, "CONTAINER_EXISTS": 1,
               "STARTED_AT": "2100-01-01T00:00:00Z"})
    assert result.returncode == 0, result.stderr
    assert "reclaim fnmusic-sources" in docker_log
    assert "up -d --no-build --pull never" in docker_log
    assert "--build" not in docker_log
    assert probe_log == ""


@pytest.mark.parametrize("source", ["missing.txt", "webui-service/static/*.missing"])
def test_missing_copy_source_is_error(tmp_path, source):
    repo = make_repo(tmp_path / "repo")
    (repo / "container/Dockerfile").write_text(f"FROM scratch\nCOPY {source} /srv/\n")
    with pytest.raises(ValueError, match="COPY source missing"):
        compute(repo)


@pytest.mark.parametrize("copy", ["webui-service/static/", "webui-service/static/*", '["webui-service/static/*", "/srv/"]'])
def test_hidden_copy_directories_and_globs_are_hashed(tmp_path, copy):
    repo = make_repo(tmp_path / "repo")
    (repo / "container/Dockerfile").write_text(f"FROM scratch\nCOPY {copy} /srv/\n" if not copy.startswith("[") else f"FROM scratch\nCOPY {copy}\n")
    hidden = repo / "webui-service/static/.assets"
    hidden.mkdir()
    asset = hidden / "data.bin"
    asset.write_bytes(b"one")
    before = compute(repo)
    asset.write_bytes(b"two")
    assert compute(repo) != before
    before = compute(repo)
    (hidden / "ignored.md").write_text("ignored")
    assert compute(repo) == before


@pytest.mark.parametrize("directory", [False, True])
def test_nested_copy_symlinks_cannot_escape_context(tmp_path, directory):
    repo = make_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    if directory:
        outside.mkdir()
        (outside / "secret").write_text("private")
    else:
        outside.write_text("private")
    (repo / "webui-service/static/escape").symlink_to(outside, target_is_directory=directory)
    with pytest.raises(ValueError, match="escapes build context"):
        compute(repo)


@pytest.mark.parametrize("label", ["", "wrong"])
def test_build_success_requires_expected_label(tmp_path, label):
    repo = make_repo(tmp_path / "repo")
    stub_probe(repo, "python:3.13-slim")
    label_of(repo).write_text(label)
    result, log, _ = run_ensure(repo, {"SKIP_LABEL": 1})
    assert result.returncode == 1
    assert "up -d --build" in log
    assert "构建指纹缺失或与预期不符" in result.stderr


def test_config_failure_does_not_probe_or_start(tmp_path):
    repo = make_repo(tmp_path / "repo")
    result, log, probe = run_ensure(repo, {"CONFIG_RC": 1})
    assert result.returncode == 1
    assert not probe
    assert " up " not in log


@pytest.mark.parametrize("script", ["install.sh", "extend.sh"])
def test_sudo_preserves_only_necessary_build_variables(tmp_path, script):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text("#!/bin/bash\n[ \"$1\" = info ] && exit 1\nexit 99\n")
    docker.chmod(0o755)
    sudo = bindir / "sudo"
    sudo.write_text('''#!/bin/bash
if [ "$1" = docker ] && [ "$2" = info ]; then exit 0; fi
[ "$1" = env ] || exit 97
shift
exec /usr/bin/env -i PATH="$PATH" "$@"
''')
    sudo.chmod(0o755)
    docker.write_text('''#!/bin/bash
if [ "$1" = info ]; then exit 1; fi
printf '%s|%s|%s|%s|%s' "${FNMUSIC_BUILD_FINGERPRINT-unset}" "${FNMUSIC_BASE_IMAGE-unset}" "${FNMUSIC_PIP_INDEX-unset}" "${FNMUSIC_APT_MIRROR-unset}" "${UNRELATED-unset}"
''')
    body = function((BASE / script).read_text(), "run_docker")
    body += '\nFNMUSIC_BUILD_FINGERPRINT=internal FNMUSIC_BASE_IMAGE=mirror FNMUSIC_PIP_INDEX= FNMUSIC_APT_MIRROR=apt run_docker compose config --format json\n'
    result = subprocess.run([str(BASH), "-c", body], capture_output=True, text=True,
                            env={**os.environ, "PATH": str(bindir) + ":/usr/bin:/bin", "UNRELATED": "secret"})
    assert result.returncode == 0, result.stderr
    assert result.stdout == "internal|mirror||apt|unset"


def test_real_compose_config_read_only(tmp_path, monkeypatch):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI unavailable")
    version = subprocess.run([docker, "compose", "version"], capture_output=True)
    if version.returncode:
        pytest.skip("Compose unavailable")
    repo = make_repo(tmp_path / "repo")
    (repo / ".env").write_text("FNMUSIC_BASE_IMAGE=mirror/python:3.13-slim\n")

    def config():
        result = subprocess.run([docker, "compose", "-f", str(repo / "docker-compose.yml"),
                                 "config", "--format", "json"], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    cached = config()
    assert cached["services"]["fnmusic-sources"]["build"]["args"]["BASE_IMAGE"] == "mirror/python:3.13-slim"
    baseline = bf.compute(repo, cached)
    monkeypatch.setenv("FNMUSIC_BASE_IMAGE", "")
    empty = config()
    assert empty["services"]["fnmusic-sources"]["build"]["args"]["BASE_IMAGE"] == "python:3.13-slim"
    assert bf.compute(repo, empty) != baseline
    monkeypatch.delenv("FNMUSIC_BASE_IMAGE")
    monkeypatch.setenv("FNMUSIC_BUILD_FINGERPRINT", "self-label")
    (repo / "docker-compose.yml").write_text(COMPOSE.replace("    image:", '    environment:\n      RUNTIME: changed\n    image:') + "# harmless comment\n")
    assert bf.compute(repo, config()) == baseline
    monkeypatch.setenv("FNMUSIC_PIP_INDEX", "https://example.invalid/simple")
    assert bf.compute(repo, config()) != baseline
    # CLI accepts both file and stdin, without any build/pull/container operations.
    final = config()
    configfile = repo / "resolved.json"
    configfile.write_text(json.dumps(final))
    command = [sys.executable, str(BASE / "proxy/build_fingerprint.py"), "--repo", str(repo)]
    for extra, stdin in [(["--config", str(configfile)], None), ([], json.dumps(final))]:
        result = subprocess.run(command + extra, input=stdin, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == bf.compute(repo, final)
