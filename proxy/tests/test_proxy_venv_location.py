"""3.0.0+：宿主代理 venv 持久化解析与兼容符号链接测试。

FPK 升级会整体替换 target/repo，repo 内 .venv-proxy 随之丢失，导致每次升级
都重装依赖（2026-11-03 实测首次调用含一段 pip 安装）。3.0.0+ 在 TRIM_PKGVAR
（应用持久目录，升级不替换）下维护 venv，并在 repo 内保留原子符号链接入口，
takeover.py/run_proxy.sh 继续读 <repo>/.venv-proxy 不变。

只测 ensure_proxy_deps.sh 中的纯解析/链接函数（sed 提取后 source），不创建
真实 venv、不联网。需要 bash；无 bash 环境（Windows 本机）跳过，CI 全量执行。
"""
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
BASH = "/bin/bash"

# 符号链接场景需要真实 POSIX 语义（Windows 无特权 symlink + git-bash ln -s 退化为
# 复制），本机 Windows 跳过，CI（Linux）全量执行。
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not (Path(BASH).exists() or shutil.which("bash")),
    reason="需要 POSIX bash 与符号链接（CI Linux 执行）")


def extract_functions() -> str:
    """从 ensure_proxy_deps.sh 提取 resolve_proxy_venv_dir / ensure_proxy_venv_compat_link。"""
    script = (BASE / "ensure_proxy_deps.sh").read_text(encoding="utf-8")
    out = []
    for name in ("resolve_proxy_venv_dir", "ensure_proxy_venv_compat_link"):
        start = script.index(f"{name}()")
        # 函数体到第一个顶格 "}" 结束
        end = script.index("\n}\n", start) + len("\n}\n")
        out.append(script[start:end])
    return "\n".join(out)


def run_scenario(body: str) -> subprocess.CompletedProcess:
    """在独立 bash 进程中执行：提取的函数 + 用例代码（以 echo 输出断言值）。"""
    code = textwrap.dedent(f"""
        set -euo pipefail
        log_info() {{ echo "[INFO] $*"; }}
        {extract_functions()}
        {body}
    """)
    return subprocess.run([BASH, "-c", code], capture_output=True, text=True)


def test_resolution_priority(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pkgvar = tmp_path / "pkgvar"
    pkgvar.mkdir()

    def scenario(env_link: str, make_link: str) -> str:
        return f"""
            export BASE_DIR={repo}
            export TRIM_PKGVAR={pkgvar}
            unset FNMUSIC_VENV_DIR
            {make_link}
            {env_link}
            VENV_DIR="$(resolve_proxy_venv_dir)"
            echo "RESOLVED=$VENV_DIR"
        """

    # 1. 无既有 venv、TRIM_PKGVAR 在场 → 持久目录
    r = run_scenario(scenario("", ""))
    assert f"RESOLVED={pkgvar}/runtime/proxy-venv" in r.stdout, r.stderr

    # 2. 显式 FNMUSIC_VENV_DIR 永远优先
    r = run_scenario(scenario("export FNMUSIC_VENV_DIR=/explicit/venv", ""))
    assert "RESOLVED=/explicit/venv" in r.stdout

    # 3. repo 内真实目录 → 原地沿用（老安装不打扰）
    real = repo / ".venv-proxy"
    real.mkdir()
    r = run_scenario(scenario("", ""))
    assert f"RESOLVED={real}" in r.stdout

    # 4. repo 内符号链接且目标可用 → 沿用目标
    shutil.rmtree(real)
    target = tmp_path / "persistent-venv"
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "python").touch()
    (target / "bin" / "python").chmod(0o755)
    (repo / ".venv-proxy").symlink_to(target, target_is_directory=True)
    r = run_scenario(scenario("", ""))
    assert f"RESOLVED={target}" in r.stdout

    # 5. 无 TRIM_PKGVAR（git 检出，清除上一场景的符号链接还原干净仓库）→ repo 内旧布局
    (repo / ".venv-proxy").unlink()
    code = textwrap.dedent(f"""
        set -euo pipefail
        log_info() {{ echo "[INFO] $*"; }}
        {extract_functions()}
        export BASE_DIR={repo}
        unset TRIM_PKGVAR FNMUSIC_VENV_DIR
        echo "RESOLVED=$(resolve_proxy_venv_dir)"
    """)
    r = subprocess.run([BASH, "-c", code], capture_output=True, text=True)
    assert f"RESOLVED={repo}/.venv-proxy" in r.stdout


def test_compat_link_atomic_and_never_touches_real_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    persistent = tmp_path / "pkgvar" / "runtime" / "proxy-venv"
    (persistent / "bin").mkdir(parents=True)
    (persistent / "bin" / "python").touch()
    (persistent / "bin" / "python").chmod(0o755)

    # 持久 venv 就绪 + repo 内无入口 → 建立符号链接
    code = textwrap.dedent(f"""
        set -euo pipefail
        log_info() {{ echo "[INFO] $*"; }}
        {extract_functions()}
        export BASE_DIR={repo}
        export VENV_DIR={persistent}
        ensure_proxy_venv_compat_link
        [ -L "{repo}/.venv-proxy" ] && echo LINK_OK
        [ "$(readlink "{repo}/.venv-proxy")" = "{persistent}" ] && echo TARGET_OK
        # 幂等：再次执行不报错不换目标
        ensure_proxy_venv_compat_link
        [ "$(readlink "{repo}/.venv-proxy")" = "{persistent}" ] && echo IDEMPOTENT_OK
    """)
    r = subprocess.run([BASH, "-c", code], capture_output=True, text=True)
    assert all(k in r.stdout for k in ("LINK_OK", "TARGET_OK", "IDEMPOTENT_OK")), r.stderr

    # 真实目录在场：绝不删除/替换（本轮已由解析层原地沿用）
    (repo / ".venv-proxy").unlink()
    real = repo / ".venv-proxy"
    real.mkdir()
    (real / "marker").write_text("keep")
    code = textwrap.dedent(f"""
        set -euo pipefail
        log_info() {{ echo "[INFO] $*"; }}
        {extract_functions()}
        export BASE_DIR={repo}
        export VENV_DIR={persistent}
        ensure_proxy_venv_compat_link
        [ -d "{real}" ] && [ ! -L "{real}" ] && echo REAL_DIR_KEPT
    """)
    r = subprocess.run([BASH, "-c", code], capture_output=True, text=True)
    assert "REAL_DIR_KEPT" in r.stdout, r.stderr


def test_persistent_venv_state_survives_repo_replacement(tmp_path):
    """升级语义：target/repo 整体删除重建后，持久 venv 与状态文件仍在原位。"""
    pkgvar = tmp_path / "pkgvar"
    venv = pkgvar / "runtime" / "proxy-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".fnmusic-deps").write_text("state-marker")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".venv-proxy").symlink_to(venv, target_is_directory=True)

    # fnOS 升级：target 整体替换（等价于删除重建 repo 目录）
    shutil.rmtree(repo)
    repo.mkdir()

    code = textwrap.dedent(f"""
        set -euo pipefail
        log_info() {{ echo "[INFO] $*"; }}
        {extract_functions()}
        export BASE_DIR={repo}
        export TRIM_PKGVAR={pkgvar}
        unset FNMUSIC_VENV_DIR
        echo "RESOLVED=$(resolve_proxy_venv_dir)"
        [ -f "{venv}/.fnmusic-deps" ] && echo STATE_SURVIVES
    """)
    r = subprocess.run([BASH, "-c", code], capture_output=True, text=True)
    assert f"RESOLVED={venv}" in r.stdout and "STATE_SURVIVES" in r.stdout, r.stderr
