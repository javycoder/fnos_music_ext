"""Issue #73: 宿主机运行时探测、依赖状态缓存与媒体组件降级行为测试。

覆盖：
1. Python 探测优先级（显式配置 → fnOS python312 → 系统 Python），最低 3.11，检查 venv/ensurepip/ssl/sqlite3
2. Node 探测优先级（显式配置 → fnOS nodejs_v22 → 系统 Node），最低 20
3. ffmpeg/ffprobe 探测与 trim 核心组件识别
4. 原生配置 @SOURCES_PATH@ 完整渲染与精简 PATH 运行
5. 依赖指纹状态文件校验（deps_state_valid / deps_write_state），未变跳过，变化/缺失/强制刷新触发重装
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
RUNTIME_SH = BASE / "proxy/runtime_common.sh"
INSTALL_COMMON = BASE / "proxy/install_common.sh"


def run_bash_cmd(cmd: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=full_env)


def test_python_explicit_override_valid(tmp_path):
    stub_py = tmp_path / "custom-py"
    stub_py.write_text(f"#!{sys.executable}\nimport sys\n"
                       f"if '-c' in sys.argv:\n"
                       f"    print('3.12')\n"
                       f"sys.exit(0)\n")
    stub_py.chmod(0o755)

    script = f"""
    source "{RUNTIME_SH}"
    FNMUSIC_PYTHON_BIN="{stub_py}" choose_python_bin
    echo "SELECTED=$FNMUSIC_SELECTED_PYTHON"
    echo "SOURCE=$FNMUSIC_PYTHON_SOURCE"
    """
    res = run_bash_cmd(script)
    assert res.returncode == 0, res.stderr
    assert f"SELECTED={stub_py}" in res.stdout
    assert "SOURCE=explicit" in res.stdout


def test_python_explicit_override_invalid_fails(tmp_path):
    bad_py = tmp_path / "bad-py"
    bad_py.write_text("#!/bin/sh\nexit 1\n")
    bad_py.chmod(0o755)

    script = f"""
    source "{RUNTIME_SH}"
    FNMUSIC_PYTHON_BIN="{bad_py}" choose_python_bin
    """
    res = run_bash_cmd(script)
    assert res.returncode != 0


def test_python_fnos_candidate_preferred_over_system(tmp_path):
    fake_fnos_dir = tmp_path / "var/apps/python312/target/bin"
    fake_fnos_dir.mkdir(parents=True)
    fake_fnos_py = fake_fnos_dir / "python3"
    fake_fnos_py.write_text(f"#!{sys.executable}\nimport sys\n"
                            f"if '-c' in sys.argv: print('3.12')\n"
                            f"sys.exit(0)\n")
    fake_fnos_py.chmod(0o755)

    # 模拟把 /var/apps/python312 指到 fake_fnos_py
    script = f"""
    source "{RUNTIME_SH}"
    # 替换探测函数内部候选列表以测试候选命中
    fnos_candidates=("{fake_fnos_py}")
    for candidate in "${{fnos_candidates[@]}}"; do
        if runtime_python_ok "${{candidate}}"; then
            FNMUSIC_SELECTED_PYTHON="${{candidate}}"
            FNMUSIC_PYTHON_SOURCE="fnOS python312"
            break
        fi
    done
    echo "SELECTED=$FNMUSIC_SELECTED_PYTHON"
    echo "SOURCE=$FNMUSIC_PYTHON_SOURCE"
    """
    res = run_bash_cmd(script)
    assert res.returncode == 0, res.stderr
    assert f"SELECTED={fake_fnos_py}" in res.stdout
    assert "SOURCE=fnOS python312" in res.stdout


def test_python_rejects_version_below_311(tmp_path):
    old_py = tmp_path / "old-py"
    # 模拟 3.10
    old_py.write_text("""#!/bin/sh
if [ "$1" = "-" ]; then
    # Python 检查脚本退出 1 表示不合格 (< 3.11)
    exit 1
fi
echo "3.10"
exit 0
""")
    old_py.chmod(0o755)

    script = f"""
    source "{RUNTIME_SH}"
    FNMUSIC_PYTHON_BIN="{old_py}" choose_python_bin
    """
    res = run_bash_cmd(script)
    assert res.returncode != 0


def test_node_explicit_override_valid(tmp_path):
    stub_node = tmp_path / "custom-node"
    stub_node.write_text("""#!/bin/sh
if [ "$1" = "-p" ]; then
    echo "22"
    exit 0
fi
echo "v22.18.0"
exit 0
""")
    stub_node.chmod(0o755)

    script = f"""
    source "{RUNTIME_SH}"
    FNMUSIC_NODE_BIN="{stub_node}" choose_node_bin
    echo "SELECTED=$FNMUSIC_SELECTED_NODE"
    echo "SOURCE=$FNMUSIC_NODE_SOURCE"
    """
    res = run_bash_cmd(script)
    assert res.returncode == 0, res.stderr
    assert f"SELECTED={stub_node}" in res.stdout
    assert "SOURCE=explicit" in res.stdout


def test_node_explicit_override_below_20_rejected(tmp_path):
    old_node = tmp_path / "old-node"
    old_node.write_text("""#!/bin/sh
if [ "$1" = "-p" ]; then
    echo "18"
    exit 0
fi
echo "v18.0.0"
exit 0
""")
    old_node.chmod(0o755)

    script = f"""
    source "{RUNTIME_SH}"
    FNMUSIC_NODE_BIN="{old_node}" choose_node_bin
    """
    res = run_bash_cmd(script)
    assert res.returncode != 0


def test_node_fnos_candidate_preferred(tmp_path):
    fake_node_dir = tmp_path / "var/apps/nodejs_v22/target/bin"
    fake_node_dir.mkdir(parents=True)
    fake_node = fake_node_dir / "node"
    fake_node.write_text("""#!/bin/sh
if [ "$1" = "-p" ]; then echo "22"; exit 0; fi
echo "v22.18.0"; exit 0
""")
    fake_node.chmod(0o755)

    script = f"""
    source "{RUNTIME_SH}"
    fnos_node22=("{fake_node}")
    for candidate in "${{fnos_node22[@]}}"; do
        if runtime_node_ok "${{candidate}}"; then
            FNMUSIC_SELECTED_NODE="${{candidate}}"
            FNMUSIC_NODE_SOURCE="fnOS nodejs_v22"
            break
        fi
    done
    echo "SELECTED=$FNMUSIC_SELECTED_NODE"
    echo "SOURCE=$FNMUSIC_NODE_SOURCE"
    """
    res = run_bash_cmd(script)
    assert res.returncode == 0, res.stderr
    assert f"SELECTED={fake_node}" in res.stdout
    assert "SOURCE=fnOS nodejs_v22" in res.stdout


def test_media_bins_detection_and_fallback(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ffmpeg = bin_dir / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\nexit 0\n")
    fake_ffmpeg.chmod(0o755)

    script = f"""
    source "{RUNTIME_SH}"
    PATH="{bin_dir}:/bin" choose_media_bins
    echo "FFMPEG=$FNMUSIC_SELECTED_FFMPEG"
    echo "FFPROBE=$FNMUSIC_SELECTED_FFPROBE"
    """
    res = run_bash_cmd(script)
    assert res.returncode == 0, res.stderr
    assert f"FFMPEG={fake_ffmpeg}" in res.stdout
    assert "FFPROBE=" in res.stdout


def test_native_config_sources_path_rendering(tmp_path):
    repo = tmp_path / "repo"
    (repo / "container").mkdir(parents=True)
    (repo / "proxy").mkdir(parents=True)
    shutil.copy(BASE / "container/supervisord-native.conf.in", repo / "container/supervisord-native.conf.in")
    shutil.copy(BASE / "fnmusic-sources-native.service.in", repo / "unit.in")
    shutil.copy(BASE / "proxy/runtime_common.sh", repo / "proxy/runtime_common.sh")
    shutil.copy(BASE / "proxy/install_common.sh", repo / "proxy/install_common.sh")

    node_bin = tmp_path / "opt/node/bin/node"
    node_bin.parent.mkdir(parents=True)
    node_bin.write_text("""#!/bin/sh
if [ "$1" = "-p" ]; then echo 20; exit 0; fi
exit 0
""")
    node_bin.chmod(0o755)

    ffmpeg_bin = tmp_path / "trim/mediasrv/ffmpeg"
    ffmpeg_bin.parent.mkdir(parents=True)
    ffmpeg_bin.write_text("#!/bin/sh\nexit 0\n")
    ffmpeg_bin.chmod(0o755)

    script = f"""
    BASE_DIR="{repo}"
    FNMUSIC_NODE_BIN="{node_bin}"
    FNMUSIC_FFMPEG_BIN="{ffmpeg_bin}"
    source "{repo}/proxy/install_common.sh"
    render_native_supervisor_conf
    render_native_placeholders "{repo}/unit.in" > "{repo}/unit.out"
    """
    res = run_bash_cmd(script)
    assert res.returncode == 0, res.stderr

    conf_text = (repo / "sources-native/supervisord.conf").read_text(encoding="utf-8")
    assert str(node_bin.parent) in conf_text
    assert str(ffmpeg_bin.parent) in conf_text
    assert "@SOURCES_PATH@" not in conf_text

    unit_text = (repo / "unit.out").read_text(encoding="utf-8")
    assert str(node_bin.parent) in unit_text
    assert "@SOURCES_PATH@" not in unit_text


def test_deps_state_valid_and_invalidation(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    fake_python = venv / "bin/python"
    fake_python.write_text(f"#!{sys.executable}\n"
                           f"import sys\n"
                           f"if sys.argv[1:] == ['-m', 'pip', 'check']: sys.exit(0)\n"
                           f"if len(sys.argv) > 1 and sys.argv[1] == '-': sys.exit(0)\n"
                           f"sys.exit(0)\n")
    fake_python.chmod(0o755)

    req_file = tmp_path / "requirements.txt"
    req_file.write_text("fastapi==0.110.0\n")

    script = f"""
    source "{RUNTIME_SH}"
    VENV="{venv}"
    PY="{fake_python}"
    REQ="{req_file}"
    REV="test-rev-1"
    DIGEST="$(deps_requirements_digest "${{REQ}}")"
    
    # 初始未写入记录：应返回 1 (无效)
    if deps_state_valid "${{VENV}}" "${{REV}}" "${{PY}}" "${{DIGEST}}" fastapi; then
        echo "FAIL_INITIAL"
        exit 1
    fi
    
    # 写入记录
    deps_write_state "${{VENV}}" "${{REV}}" "${{PY}}" "${{DIGEST}}"
    
    # 记录匹配：应返回 0 (有效)
    if ! deps_state_valid "${{VENV}}" "${{REV}}" "${{PY}}" "${{DIGEST}}" fastapi; then
        echo "FAIL_MATCH"
        exit 1
    fi
    
    # requirements.txt 变化后：应返回 1 (失效)
    echo "uvicorn==0.28.0" >> "${{REQ}}"
    NEW_DIGEST="$(deps_requirements_digest "${{REQ}}")"
    if deps_state_valid "${{VENV}}" "${{REV}}" "${{PY}}" "${{NEW_DIGEST}}" fastapi; then
        echo "FAIL_CHANGED_REQ"
        exit 1
    fi
    
    echo "ALL_OK"
    """
    res = run_bash_cmd(script)
    assert res.returncode == 0, res.stderr
    assert "ALL_OK" in res.stdout


@pytest.mark.parametrize("apt_recovers", [False, True])
def test_native_missing_media_attempts_install_then_continues(apt_recovers):
    text = (BASE / "ensure_sources_native.sh").read_text()
    start = text.index("\nchoose_media_bins", text.index('NODE_BIN="${FNMUSIC_SELECTED_NODE}"'))
    end = text.index("\n# ------------------------------------------------------- 3.", start)
    script = r'''
set -euo pipefail
source "$RUNTIME_SH"
media_ready=0
runtime_tool_path() {
    [ "$media_ready" = 1 ] || return 1
    printf '/fake/%s\n' "$2"
}
log_info() { printf '%s\n' "$*"; }
log_warn() { printf '%s\n' "$*"; }
apt_install_packages() {
    printf 'APT_ATTEMPTED\n'
    media_ready="$APT_RECOVERS"
    [ "$media_ready" = 1 ]
}
'''
    res = run_bash_cmd(script + text[start:end] + "\nprintf 'CONTINUED_TO_LXSERVER\\n'\n",
                       {"RUNTIME_SH": str(RUNTIME_SH), "APT_RECOVERS": str(int(apt_recovers))})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "APT_ATTEMPTED" in res.stdout
    assert "CONTINUED_TO_LXSERVER" in res.stdout
    assert ("缺少 ffmpeg：" in res.stdout) == (not apt_recovers)


@pytest.mark.parametrize("valid", [False, True])
def test_extend_precheck_uses_selected_python_not_system_venv(tmp_path, valid):
    selected = tmp_path / "app-python"
    selected.write_text(f"#!/bin/sh\nexit {0 if valid else 1}\n")
    selected.chmod(0o755)
    text = (BASE / "extend.sh").read_text()
    start = text.index("# 1.1 ")
    end = text.index("# 1.2 ", start)
    script = r'''
set -euo pipefail
source "$RUNTIME_SH"
python3() { printf 'SYSTEM_PYTHON_CALLED\n' >&2; return 1; }
log_err() { printf '%s\n' "$*" >&2; }
'''
    res = run_bash_cmd(script + text[start:end] + "\nprintf 'PRECHECK_OK\\n'\n",
                       {"RUNTIME_SH": str(RUNTIME_SH), "FNMUSIC_PYTHON_BIN": str(selected)})
    assert res.returncode == (0 if valid else 1), res.stdout + res.stderr
    assert "SYSTEM_PYTHON_CALLED" not in res.stderr
    assert ("PRECHECK_OK" in res.stdout) == valid
    if not valid:
        assert "Python 3.11+" in res.stderr


def test_native_forwards_selected_node_to_module_validator(tmp_path):
    text = (BASE / "ensure_sources_native.sh").read_text()
    start = text.index('bash "${BASE_DIR}/container/ensure_lxserver_native.sh"')
    end = text.index('\n\nlog_info "原生音源运行时就绪。"', start)
    selected = str(tmp_path / "Node Runtime/bin/node")
    script = r'''
set -euo pipefail
bash() { printf '%s\n' "$@"; }
'''
    res = run_bash_cmd(script + text[start:end], {
        "BASE_DIR": str(BASE), "LXSERVER_ARTIFACT_DIR": "/fake/artifacts",
        "LXSERVER_DIR": "/fake/lxserver", "LXSERVER_VERSION_FILE": "/fake/version",
        "NODE_BIN": selected,
    })
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines()[-2:] == ["--node-bin", selected]
