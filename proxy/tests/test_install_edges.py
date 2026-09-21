"""安装链路深水区行为测试（真脚本/函数抽取 + PATH 桩执行）。

补 test_installation_reliability.py 未覆盖的四块「安装失败/搜索不了」高发区：
- extend.sh get_fnos_gateway_ports：网关端口解析（缺文件默认/JSON/嵌套/正则回退）
- extend.sh verify_acceptance：装完验收（401 快速路径、四级回退、搜索→取流、
  外部音源失败只告警不阻断、洛雪直链诊断）与 rollback 拒绝分支
- install.sh lx_verify_and_activate：docker exec 校验回路（成功激活→平台写回
  .env、非交互失败中止、交互重试、激活失败降级）
- ensure_base_image.sh：基础镜像源探测矩阵（不可用/手动指定/本地短路/缓存复用/
  官方缓存不提前/镜像回退/全军覆没）
- netease_login.sh：扫码登录脚本冒烟（未运行/已登录/803 成功/连续失败终止）

全部桩化外部命令，不触碰宿主 docker/systemd/网络。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
if BASH is None:
    pytest.skip("bash 不可用", allow_module_level=True)


def function(text: str, name: str) -> str:
    start = text.index(name + "() {")
    return text[start:text.index("\n}", start) + 2] + "\n"


def run_bash(body: str, env: dict | None = None, cwd: Path | None = None,
             path: str | None = None) -> subprocess.CompletedProcess:
    env = dict(env or os.environ)
    if path:
        env["PATH"] = path
    return subprocess.run([BASH, "-c", body], env=env, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=90)


def write_stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    if p.is_symlink() or p.exists():
        p.unlink()
    p.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    p.chmod(0o755)


def link_tools(bindir: Path, tools: tuple[str, ...]) -> None:
    for tool in tools:
        real = shutil.which(tool)
        if real:
            (bindir / tool).symlink_to(real)


# ------------------------------------------------------ get_fnos_gateway_ports ---

def gateway_block(conf_path: Path) -> str:
    extend = (BASE / "extend.sh").read_text(encoding="utf-8")
    return function(extend, "get_fnos_gateway_ports").replace(
        "/usr/trim/etc/network_gateway_setting.conf", str(conf_path))


GW_TOOLS = ("cat", "python3")


def test_gateway_ports_defaults_when_conf_missing(tmp_path):
    script = gateway_block(tmp_path / "absent.conf") + 'get_fnos_gateway_ports\n'
    out = run_bash(script)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "5666 5667"


@pytest.mark.parametrize("text,expected", [
    ('{"http_port": 8666, "https_port": 8667}\n', "8666 8667"),
    # 嵌套结构里只要键名含 http/https + port 且值是数字
    ('{"gateway": {"portal_http_port": "7001", "portal_https_port": 7002}}\n', "7001 7002"),
    # 非 JSON 文本走正则回退（等号/冒号都认）
    ('http_port = 7777\nhttps_port: 8888\n', "7777 8888"),
    # 无关键字的垃圾内容 → 默认端口
    ('not json at all\n', "5666 5667"),
])
def test_gateway_ports_parsing(tmp_path, text, expected):
    conf = tmp_path / "gateway.conf"
    conf.write_text(text, encoding="utf-8")
    out = run_bash(gateway_block(conf) + "get_fnos_gateway_ports\n")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected


# ---------------------------------------------------------- verify_acceptance ---

VERIFY_STUBS = """
log_info() { printf 'info %s\\n' "$*"; }
log_warn() { printf 'warn %s\\n' "$*"; }
log_err() { printf 'err %s\\n' "$*" >&2; }
warn_unregistered_mdl_platforms() { :; }
get_fnos_gateway_ports() { printf '5666 5667\\n'; }
TARGET_SOCK=/tmp/verify.sock
MUSICDL_URL=http://mdl.test
MUSICBOX_URL=http://mb.test
LX_URL=http://lx.test
FNMUSIC_ONLINE_SOURCES=
LX_SOURCES=
ENABLE_MUSICDL=0
ENABLE_MUSICBOX=0
ENABLE_LX=0
"""


def curl_stub_body(bindir: Path) -> str:
    log = bindir / "curl.log"
    return f"""
log='{log}'
o_file=""; unix_probe=0; force_000=0
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then o_file="$a"; prev=""; continue; fi
  if [ "$prev" = "--unix-socket" ]; then unix_probe=1; prev=""; continue; fi
  if [ "$prev" = "--max-time" ] || [ "$prev" = "-H" ] || [ "$prev" = "-X" ]; then prev=""; continue; fi
  case "$a" in
    -o|--unix-socket|--max-time|-H|-X) prev="$a" ;;
    http://*|https://*) url="$a" ;;
  esac
done
printf 'curl %s\\n' "${{url:-?}}" >> "$log"
case "${{url:-}}" in
  */search/track?keyword=test*)
    if [ "${{CURL_401_UNIX_000:-0}}" = "1" ] && [ "$unix_probe" = "1" ]; then printf '000 0.1'; exit 0; fi
    printf '%s %s' "${{CURL_401_CODE:-401}}" "${{CURL_401_TIME:-0.5}}"
    [ -n "$o_file" ] && printf '%s' "${{CURL_401_BODY:-INVALID TOKEN}}" > "$o_file"
    ;;
  */track/stream*)
    [ -n "$o_file" ] && head -c "${{STREAM_BYTES:-20000}}" /dev/zero | tr '\\0' 'x' > "$o_file"
    printf '%s' "${{STREAM_CODE:-206}}"
    ;;
  */api/v1/track/url*)
    printf 'diag-data'
    ;;
  $LX_URL/api/v1/search*)
    printf '%s' "${{SEARCH_LX_JSON:-}}"
    ;;
  $MUSICBOX_URL/api/v1/search*)
    printf '%s' "${{SEARCH_MB_JSON:-}}"
    ;;
  $MUSICDL_URL/search?keyword=*)
    printf '%s' "${{SEARCH_MDL_JSON:-}}"
    ;;
  *)
    printf '200 0.5'
    ;;
esac
"""


def acceptance_bindir(tmp_path: Path, **env: str) -> tuple[Path, dict]:
    bindir = tmp_path / "abin"
    bindir.mkdir(exist_ok=True)
    link_tools(bindir, ("python3", "mktemp", "wc", "awk", "tr", "head", "grep", "cat", "rm"))
    write_stub(bindir, "curl", curl_stub_body(bindir))
    # URL 变量必须进环境：curl 桩是独立进程，靠 env 里的 URL 做 case 匹配
    defaults = {"LX_URL": "http://lx.test", "MUSICDL_URL": "http://mdl.test",
                "MUSICBOX_URL": "http://mb.test"}
    defaults.update(env)
    env_full = {**os.environ, "PATH": str(bindir), **defaults}
    return bindir, env_full


def acceptance_script(tmp_path: Path, flags: str, **env: str) -> subprocess.CompletedProcess:
    extend = (BASE / "extend.sh").read_text(encoding="utf-8")
    _, env_full = acceptance_bindir(tmp_path, **env)
    script = ("set -uo pipefail\n" + VERIFY_STUBS + flags + "\n"
              + function(extend, "verify_acceptance") + "verify_acceptance\n")
    return run_bash(script, env=env_full)


def test_acceptance_401_fast_path_then_empty_search_warns_not_fails(tmp_path):
    # 6a 401 秒回 + 所有源搜索为空 → 只告警不阻断（外部网络波动不回滚）
    result = acceptance_script(
        tmp_path, "ENABLE_MUSICDL=1",
        SEARCH_MDL_JSON=json.dumps({"items": []}))
    assert result.returncode == 0, result.stderr
    assert "验收 6a 通过" in result.stdout
    assert "搜索结果均为空" in result.stdout
    assert "不阻断" not in result.stdout  # 告警文案本身不含该词，靠 rc 锁行为
    assert "err " not in result.stderr


def test_acceptance_musicdl_stream_success(tmp_path):
    result = acceptance_script(
        tmp_path, "ENABLE_MUSICDL=1",
        SEARCH_MDL_JSON=json.dumps({"items": [{"id": "mdl-1"}]}),
        STREAM_CODE="206", STREAM_BYTES="20000")
    assert result.returncode == 0, result.stderr
    assert "验收 6b 通过：音源 musicdl" in result.stdout


def test_acceptance_stream_too_small_counts_as_failure(tmp_path):
    # 206 但字节数 < 10KB：不能算取流成功，最终仍是不阻断告警
    result = acceptance_script(
        tmp_path, "ENABLE_MUSICDL=1",
        SEARCH_MDL_JSON=json.dumps({"items": [{"id": "mdl-1"}]}),
        STREAM_CODE="206", STREAM_BYTES="100")
    assert result.returncode == 0, result.stderr
    assert "取流未成功" in result.stdout
    assert "验收 6b 通过" not in result.stdout


def test_acceptance_401_wrong_status_fails(tmp_path):
    result = acceptance_script(tmp_path, "ENABLE_MUSICDL=1", CURL_401_CODE="200",
                               CURL_401_BODY='{"code":0}')
    assert result.returncode == 1
    assert "验收 6a 失败" in result.stderr


def test_acceptance_401_too_slow_fails(tmp_path):
    result = acceptance_script(tmp_path, "ENABLE_MUSICDL=1", CURL_401_TIME="4.2")
    assert result.returncode == 1
    assert "耗时过长" in result.stderr


def test_acceptance_socket_probe_falls_back_to_gateway(tmp_path):
    # socket 探测 000 → 回退网关 https 仍拿 401 → 6a 通过
    result = acceptance_script(tmp_path, "ENABLE_MUSICDL=1", CURL_401_UNIX_000="1")
    assert result.returncode == 0, result.stderr
    assert "验收 6a 通过" in result.stdout


def test_acceptance_lx_failure_runs_direct_url_diagnosis(tmp_path):
    # lx 有搜索候选但取流全失败 → 告警 + 触发洛雪直链诊断，不 return 1
    result = acceptance_script(
        tmp_path, "ENABLE_MUSICDL=0 ENABLE_LX=1",
        SEARCH_LX_JSON=json.dumps({"items": [{"id": "lx:kg:abc"}]}),
        STREAM_CODE="404", STREAM_BYTES="0")
    assert result.returncode == 0, result.stderr
    assert "未能成功取流" in result.stdout
    log = (tmp_path / "abin" / "curl.log").read_text(encoding="utf-8")
    assert "/api/v1/track/url?" in log
    assert "直链诊断" in result.stdout


def test_rollback_refuses_when_restore_plan_unverifiable(tmp_path):
    """restore-plan 无法确认身份时绝不停止服务（保留代理运行状态）。"""
    extend = (BASE / "extend.sh").read_text(encoding="utf-8")
    stubs = """
log_info() { printf 'info %s\\n' "$*"; }
log_warn() { printf 'warn %s\\n' "$*"; }
log_err() { printf 'err %s\\n' "$*" >&2; }
takeover() { if [ "$1" = "restore-plan" ]; then return 1; fi; }
sudo() { printf 'sudo %s\\n' "$*" >&2; exit 99; }
"""
    result = run_bash("set -uo pipefail\n" + stubs + function(extend, "rollback")
                      + "rollback")
    assert result.returncode == 1
    assert "无法在停止前确认可恢复身份" in result.stderr
    # systemctl stop 绝不能被调用
    assert "sudo systemctl stop" not in result.stderr


# ------------------------------------------------------ lx_verify_and_activate ---

LX_STUBS = """
log_info() { printf 'info %s\\n' "$*"; }
log_warn() { printf 'warn %s\\n' "$*"; }
log_err() { printf 'err %s\\n' "$*" >&2; }
run_docker() {
  if [ "$1" = "exec" ]; then
    n="$(cat "${STUB_STATE_DIR}/n" 2>/dev/null || echo 0)"
    n=$((n + 1)); echo "$n" > "${STUB_STATE_DIR}/n"
    if [ "$n" -eq 1 ]; then printf '%s' "${VERIFY_REPORT_1}"; else printf '%s' "${VERIFY_REPORT_2:-${VERIFY_REPORT_1}}"; fi
    return 0
  fi
  return 0
}
prompt() { printf '%s' "${PROMPT_ANSWER:-${2:-}}"; }
curl() {
  printf 'curl %s\\n' "$*" >> "${STUB_STATE_DIR}/curl.log"
  return "${ACTIVATE_RC:-0}"
}
"""


def lx_activation_script(tmp_path: Path, env_url: str, env_path: Path) -> str:
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    state = tmp_path / "lxstate"
    state.mkdir()
    return ("set -uo pipefail\n" + LX_STUBS
            + function(install, "dotenv_escape")
            + f'NON_INTERACTIVE=1\nCONTAINER_NAME=fnmusic-sources\n'
            f'BASE_DIR="{BASE}"\nENV_PATH="{env_path}"\n'
            f'STUB_STATE_DIR="{state}"\nLX_SOURCE_URL_CLI="{env_url}"\n'
            + function(install, "lx_verify_and_activate")
            + f'lx_verify_and_activate "{env_url}"\n')


def test_lx_activation_success_writes_platforms_to_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LX_SOURCE_URL='http://s/x.js'\nFNMUSIC_LX_ENABLED=true\n",
                        encoding="utf-8")
    report = json.dumps({"ok": True, "platforms": ["kg", "wy"]})
    result = run_bash(lx_activation_script(tmp_path, "http://s/x.js", env_file),
                      env={**os.environ,
                           "VERIFY_REPORT_1": report,
                           "STUB_STATE_DIR": str(tmp_path / "lxstate"),
                           "PATH": os.environ["PATH"]})
    assert result.returncode == 0, result.stderr
    # 激活 POST 携带正确 URL
    curl_log = (tmp_path / "lxstate" / "curl.log").read_text(encoding="utf-8")
    assert "-X POST http://127.0.0.1:8772/api/v1/source" in curl_log
    assert "http://s/x.js" in curl_log
    # 平台交集写回 .env（真实 env_merge 增量合并）
    content = env_file.read_text(encoding="utf-8")
    assert "LX_SOURCES='kg,wy'" in content
    assert "已写回" in result.stdout


def test_lx_activation_noninteractive_failure_aborts(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LX_SOURCE_URL='http://bad/x.js'\n", encoding="utf-8")
    report = json.dumps({"ok": False, "category": "music_url", "message": "源脚本解析失败"})
    result = run_bash(lx_activation_script(tmp_path, "http://bad/x.js", env_file),
                      env={**os.environ, "VERIFY_REPORT_1": report,
                           "STUB_STATE_DIR": str(tmp_path / "lxstate")})
    assert result.returncode == 1
    # 错误分类与报告原文都透出（install_callback 会把 [ERROR] 行顶进应用中心弹窗）
    assert "校验未通过（music_url）" in result.stdout
    assert "源脚本解析失败" in result.stderr
    assert "--lx-skip-verify" in result.stderr  # 给出可操作的跳过校验指引


def test_lx_activation_skip_verify_activates_without_probe(tmp_path):
    """--lx-skip-verify：不做全链路校验，直接 POST 激活并继续。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LX_SOURCE_URL='http://s/x.js'\n", encoding="utf-8")
    state = tmp_path / "lxstate"
    state.mkdir()
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    script = ("set -uo pipefail\n" + LX_STUBS
              + function(install, "dotenv_escape")
              + f'NON_INTERACTIVE=1\nCONTAINER_NAME=fnmusic-sources\nLX_SKIP_VERIFY=1\n'
              f'BASE_DIR="{BASE}"\nENV_PATH="{env_file}"\n'
              f'STUB_STATE_DIR="{state}"\nLX_SOURCE_URL_CLI="http://s/x.js"\n'
              + function(install, "lx_verify_and_activate")
              + 'lx_verify_and_activate "http://s/x.js"\n')
    result = run_bash(script, env={**os.environ, "STUB_STATE_DIR": str(state)})
    assert result.returncode == 0, result.stderr
    assert "跳过洛雪源可用性校验" in result.stdout
    # 未走 docker exec 校验（校验次数文件不存在），只有激活 POST
    assert not (state / "n").exists()
    assert "-X POST http://127.0.0.1:8772/api/v1/source" in (
        state / "curl.log").read_text(encoding="utf-8")


def test_lx_activation_empty_report_aborts(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    result = run_bash(lx_activation_script(tmp_path, "http://x/y.js", env_file),
                      env={**os.environ, "VERIFY_REPORT_1": "",
                           "STUB_STATE_DIR": str(tmp_path / "lxstate")})
    assert result.returncode == 1
    assert "校验未通过（无输出）" in result.stdout


def test_lx_activation_interactive_retry_recovers(tmp_path):
    """交互模式：第一次校验失败，重输 URL 后成功并写回新 URL。"""
    env_file = tmp_path / ".env"
    env_file.write_text("LX_SOURCE_URL='http://old/x.js'\n", encoding="utf-8")
    state = tmp_path / "lxstate"
    state.mkdir()
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    ok_report = json.dumps({"ok": True, "platforms": ["kw"]})
    script = ("set -uo pipefail\n" + LX_STUBS
              + function(install, "dotenv_escape")
              + f'NON_INTERACTIVE=0\nCONTAINER_NAME=fnmusic-sources\n'
              f'BASE_DIR="{BASE}"\nENV_PATH="{env_file}"\n'
              f'STUB_STATE_DIR="{state}"\nLX_SOURCE_URL_CLI="http://old/x.js"\n'
              f'PROMPT_ANSWER="http://new/y.js"\n'
              + function(install, "lx_verify_and_activate")
              + 'lx_verify_and_activate "http://old/x.js"\n')
    result = run_bash(script, env={**os.environ,
                                   "VERIFY_REPORT_1": json.dumps({"ok": False, "category": "search"}),
                                   "VERIFY_REPORT_2": ok_report,
                                   "STUB_STATE_DIR": str(state)})
    assert result.returncode == 0, result.stderr
    # docker exec 校验确实重试了两轮
    assert (state / "n").read_text(encoding="utf-8").strip() == "2"
    content = env_file.read_text(encoding="utf-8")
    assert "LX_SOURCE_URL='http://new/y.js'" in content
    assert "LX_SOURCES='kw'" in content


def test_lx_activation_post_failure_degrades_but_persists_platforms(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LX_SOURCE_URL='http://s/x.js'\n", encoding="utf-8")
    result = run_bash(lx_activation_script(tmp_path, "http://s/x.js", env_file),
                      env={**os.environ,
                           "VERIFY_REPORT_1": json.dumps({"ok": True, "platforms": ["kg"]}),
                           "ACTIVATE_RC": "7",
                           "STUB_STATE_DIR": str(tmp_path / "lxstate")})
    assert result.returncode == 0, result.stderr
    assert "激活请求失败" in result.stdout
    assert "LX_SOURCES='kg'" in env_file.read_text(encoding="utf-8")


# ---------------------------------------------------------- ensure_base_image ---

DOCKER_STUB = """
printf 'docker %s\\n' "$*" >> "${STUB_STATE_DIR}/docker.log"
sub="${1:-}"
case "$sub" in
  info) exit ${DOCKER_INFO_RC:-0} ;;
  image)
    # docker image inspect <ref>：$3=ref
    for r in ${DOCKER_LOCAL_REFS:-}; do
      [ "$3" = "$r" ] && exit 0
    done
    exit 1
    ;;
  pull)
    for r in ${DOCKER_PULL_OK_REFS:-}; do
      [ "$3" = "$r" ] && exit 0
    done
    exit 1
    ;;
esac
exit 0
"""


def base_image_bindir(tmp_path: Path) -> Path:
    bindir = tmp_path / "ebin"
    bindir.mkdir()
    # dirname 必须在 PATH：脚本靠它定位 BASE_DIR，缺失会让 .env 写入逃逸沙箱
    link_tools(bindir, ("dirname", "timeout", "mktemp", "date", "chmod", "cp", "rm",
                        "cat", "grep", "cut", "tr", "tail", "sed", "python3"))
    (bindir / "sudo").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    (bindir / "sudo").chmod(0o755)
    write_stub(bindir, "docker", DOCKER_STUB)
    return bindir


def run_base_image(tmp_path: Path, env_text: str | None = None, **env: str
                   ) -> tuple[subprocess.CompletedProcess, Path]:
    # 脚本沙箱副本：BASE_DIR 指向 tmp（proxy/env_merge.py 用真实实现）
    sandbox = tmp_path / "bisandbox"
    (sandbox / "proxy").mkdir(parents=True)
    shutil.copy2(BASE / "ensure_base_image.sh", sandbox / "ensure_base_image.sh")
    (sandbox / "proxy" / "env_merge.py").symlink_to(BASE / "proxy" / "env_merge.py")
    if env_text is not None:
        (sandbox / ".env").write_text(env_text, encoding="utf-8")
    state = tmp_path / "estate"
    state.mkdir(exist_ok=True)
    bindir = base_image_bindir(tmp_path)
    full_env = {**os.environ, "PATH": str(bindir),
                "STUB_STATE_DIR": str(state), "PULL_TIMEOUT": "5",
                **{k: str(v) for k, v in env.items()}}
    # 金丝雀：真实仓库 .env 绝不能被沙箱运行触碰（cwd 兜底同样指向 tmp）
    real_env = BASE / ".env"
    canary = (real_env.exists(), real_env.stat().st_mtime_ns if real_env.exists() else None)
    result = subprocess.run([BASH, str(sandbox / "ensure_base_image.sh")],
                            env=full_env, cwd=str(tmp_path),
                            capture_output=True, text=True, timeout=90)
    if canary[0]:
        assert real_env.stat().st_mtime_ns == canary[1], "沙箱逃逸：真实 .env 被修改！"
    return result, sandbox / ".env"


def docker_log(tmp_path: Path) -> str:
    log = tmp_path / "estate" / "docker.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_base_image_docker_unavailable_exits(tmp_path):
    result, _ = run_base_image(tmp_path, DOCKER_INFO_RC=1)
    assert result.returncode == 1
    assert "Docker daemon 不可用" in result.stderr


def test_base_image_manual_reference_only(tmp_path):
    # BASE_IMAGE 手动指定：仅验证该引用，不轮询镜像源
    result, env_file = run_base_image(tmp_path, env_text=None, BASE_IMAGE="reg.local/py:3.13",
                                      DOCKER_PULL_OK_REFS="reg.local/py:3.13")
    assert result.returncode == 0, result.stderr
    log = docker_log(tmp_path)
    assert "pull --quiet reg.local/py:3.13" in log
    assert "docker.1ms.run" not in log
    assert "FNMUSIC_BASE_IMAGE='reg.local/py:3.13'" in env_file.read_text(encoding="utf-8")
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_base_image_local_short_circuits_pull(tmp_path):
    # 本地已有镜像：零 pull 直接选中
    result, env_file = run_base_image(
        tmp_path, env_text=None,
        DOCKER_LOCAL_REFS="docker.1ms.run/library/python:3.13-slim")
    assert result.returncode == 0, result.stderr
    assert "pull" not in docker_log(tmp_path)
    assert "docker.1ms.run/library/python:3.13-slim" in env_file.read_text(encoding="utf-8")


def test_base_image_cached_mirror_reused_without_rewrite(tmp_path):
    cached = "docker.m.daocloud.io/library/python:3.13-slim"
    result, env_file = run_base_image(
        tmp_path, env_text=f"FNMUSIC_BASE_IMAGE='{cached}'\nOTHER='keep'\n",
        DOCKER_PULL_OK_REFS=cached)
    assert result.returncode == 0, result.stderr
    # 选定==缓存：不重写 .env、不产生备份
    assert env_file.read_text(encoding="utf-8") == f"FNMUSIC_BASE_IMAGE='{cached}'\nOTHER='keep'\n"
    assert not list(tmp_path.glob(".env.bak*")) and not list(
        (tmp_path / "bisandbox").glob(".env.bak*"))
    # 缓存源是第一个被尝试的候选
    assert "pull --quiet docker.m.daocloud.io/library/python:3.13-slim" in docker_log(tmp_path)


def test_base_image_cached_official_ref_not_prioritized(tmp_path):
    # 缓存的官方 docker.io 引用不提前，首个尝试来自默认国内镜像
    result, _ = run_base_image(
        tmp_path, env_text="FNMUSIC_BASE_IMAGE='docker.io/library/python:3.13-slim'\n",
        DOCKER_PULL_OK_REFS="docker.1ms.run/library/python:3.13-slim")
    assert result.returncode == 0, result.stderr
    log = docker_log(tmp_path)
    first_pull = next(line for line in log.splitlines() if " pull " in line)
    assert "docker.1ms.run" in first_pull


def test_base_image_falls_through_failed_mirrors(tmp_path):
    # 第一个镜像失败 → 自动切换下一个入列候选成功；未入列的镜像与官方兜底不会被尝试
    good = "docker.m.daocloud.io/library/python:3.13-slim"
    result, env_file = run_base_image(
        tmp_path, env_text=None,
        FNMUSIC_DOCKER_MIRRORS="docker.1ms.run docker.m.daocloud.io hub.rat.dev",
        FNMUSIC_MIRROR_TRIES="2",
        DOCKER_PULL_OK_REFS=good)
    assert result.returncode == 0, result.stderr
    pulls = [line for line in docker_log(tmp_path).splitlines() if " pull " in line]
    assert len(pulls) == 2  # 1ms 失败一次后 daocloud 命中
    assert "pull --quiet docker.1ms.run" in pulls[0]
    assert "pull --quiet docker.m.daocloud.io" in pulls[1]
    assert "hub.rat.dev" not in docker_log(tmp_path)      # 未入镜像源上限
    assert "pull --quiet python:3.13-slim" not in docker_log(tmp_path)  # 官方兜底未触发
    assert f"FNMUSIC_BASE_IMAGE='{good}'" in env_file.read_text(encoding="utf-8")


def test_base_image_all_candidates_fail_exits_with_hint(tmp_path):
    result, _ = run_base_image(tmp_path, env_text=None, BASE_IMAGE="reg.invalid/py:3.13")
    assert result.returncode == 1
    assert "所有基础镜像候选均拉取失败" in result.stderr
    assert "BASE_IMAGE" in result.stderr  # 给出可操作的手动指定提示


def test_base_image_write_preserves_existing_env(tmp_path):
    # 写入 FNMUSIC_BASE_IMAGE 走 env_merge：其他键原样保留
    result, env_file = run_base_image(
        tmp_path, env_text="LX_SOURCE_URL='http://s/x.js'\nSECRET_KEY='keep-me'\n",
        DOCKER_PULL_OK_REFS="docker.1ms.run/library/python:3.13-slim")
    assert result.returncode == 0, result.stderr
    content = env_file.read_text(encoding="utf-8")
    assert "LX_SOURCE_URL='http://s/x.js'" in content
    assert "SECRET_KEY='keep-me'" in content
    assert "FNMUSIC_BASE_IMAGE='docker.1ms.run/library/python:3.13-slim'" in content


# ----------------------------------------------------------- netease_login.sh ---

NETEASE_TOOLS = ("dirname", "grep", "cut", "tr", "date", "cat", "jq", "printf")


def netease_curl_stub(bindir: Path) -> str:
    # 普通字符串（f-string 会把 JSON 花括号当替换字段）；默认 JSON 放在变量赋值
    # 里而不是 ${VAR:-默认} 展开内（否则花括号被 bash 提前截断）
    body = """
printf 'curl %s\\n' "$*" >> '__LOG__'
url=""; method="GET"; prev=""
for a in "$@"; do
  if [ "$prev" = "-X" ]; then method="$a"; prev=""; continue; fi
  case "$a" in
    -X) prev="$a" ;;
    --max-time) prev="$a" ;;
    http://*|https://*) url="$a" ;;
  esac
done
STATUS_JSON="${NETEASE_STATUS_JSON:-}"
LOGIN_JSON="${NETEASE_LOGIN_JSON:-}"
CHECK_JSON="${NETEASE_CHECK_JSON:-}"
[ -n "$STATUS_JSON" ] || STATUS_JSON='{"ok":true,"data":{"logged_in":false}}'
[ -n "$LOGIN_JSON" ] || LOGIN_JSON='{"ok":true,"data":{"unikey":"u1","qr_ascii":"QR-CODE"}}'
[ -n "$CHECK_JSON" ] || CHECK_JSON='{"ok":true,"data":{"code":803}}'
case "$url" in
  */healthz) [ "${NETEASE_HEALTHZ_OK:-1}" = "1" ] && exit 0 || exit 7 ;;
  */auth/status) printf '%s' "$STATUS_JSON" ;;
  */auth/login)
    if [ "$method" = "POST" ]; then
      if [ "${NETEASE_LOGIN_OK:-1}" = "1" ]; then
        printf '%s' "$LOGIN_JSON"
      else
        printf 'garbage'
      fi
    fi
    ;;
  */auth/login/check*) printf '%s' "$CHECK_JSON" ;;
esac
exit 0
"""
    return body.replace("__LOG__", str(bindir / "netease-curl.log"))


def run_netease(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    bindir = tmp_path / "nbin"
    bindir.mkdir(exist_ok=True)
    link_tools(bindir, NETEASE_TOOLS)
    write_stub(bindir, "curl", netease_curl_stub(bindir))
    write_stub(bindir, "sleep", "exit 0\n")
    # 沙箱副本：BASE_DIR 内的 .env 读取指向 tmp
    sandbox = tmp_path / "nsandbox"
    sandbox.mkdir(exist_ok=True)
    shutil.copy2(BASE / "netease_login.sh", sandbox / "netease_login.sh")
    full_env = {**os.environ, "PATH": str(bindir), "FNMUSIC_MUSICBOX_URL": "http://mb.test", **env}
    return subprocess.run([BASH, str(sandbox / "netease_login.sh")],
                          env=full_env, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shutil.which("jq") is None, reason="需要 jq")
def test_netease_login_service_down_exits_gracefully(tmp_path):
    result = run_netease(tmp_path, NETEASE_HEALTHZ_OK="0")
    assert result.returncode == 0, result.stderr
    assert "musicbox 音源未运行" in result.stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="需要 jq")
def test_netease_login_already_logged_in(tmp_path):
    result = run_netease(tmp_path, NETEASE_STATUS_JSON=(
        '{"ok":true,"data":{"logged_in":true,"nickname":"tester"}}'))
    assert result.returncode == 0, result.stderr
    assert "网易云已登录：tester" in result.stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="需要 jq")
def test_netease_login_qrcode_flow_success(tmp_path):
    result = run_netease(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "QR-CODE" in result.stdout          # 二维码已展示
    assert "网易云登录成功：已登录用户" in result.stdout  # check 803 → status 兜底昵称


@pytest.mark.skipif(shutil.which("jq") is None, reason="需要 jq")
def test_netease_login_consecutive_failures_terminate(tmp_path):
    result = run_netease(tmp_path, NETEASE_LOGIN_OK="0")
    assert result.returncode == 0, result.stderr
    assert "网络请求连续失败" in result.stdout
    log = (tmp_path / "nbin" / "netease-curl.log").read_text(encoding="utf-8")
    assert log.count("-X POST") == 5  # 恰好 5 次失败后终止
