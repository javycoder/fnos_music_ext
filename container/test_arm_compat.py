import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "container" / "ensure_lxserver_native.sh"
LOCKED_VERSION = "0.2.5"
NATIVE_NAME = "music-tag-native.linux-arm64-gnu.node"
X64_NATIVE_NAME = "music-tag-native.linux-x64-gnu.node"
NODE_AVAILABLE = shutil.which("node") is not None


def _write_native_tarball(path: Path, content: bytes = b"locked-arm64-binding") -> None:
    package_dir = path.parent / "package-source"
    binary = package_dir / "package" / NATIVE_NAME
    binary.parent.mkdir(parents=True)
    binary.write_bytes(content)
    (binary.parent / "package.json").write_text(
        json.dumps({"name": "test", "version": LOCKED_VERSION}) + "\n", encoding="utf-8"
    )
    with tarfile.open(path, "w:gz") as archive:
        archive.add(binary.parent / "package.json", arcname="package/package.json")
        archive.add(binary, arcname=f"package/{NATIVE_NAME}")


def _write_version_file(path: Path, tarball_sha256: str) -> None:
    path.write_text(
        f"LXSERVER_NATIVE_VERSION={LOCKED_VERSION}\n"
        f"LXSERVER_NATIVE_ARM64_SHA256={tarball_sha256}\n",
        encoding="utf-8",
    )


def _make_tarball(tmp_path: Path, content: bytes = b"locked-arm64-binding") -> Path:
    tarball = tmp_path / "artifact" / f"music-tag-native-linux-arm64-gnu-{LOCKED_VERSION}.tgz"
    tarball.parent.mkdir(parents=True, exist_ok=True)
    _write_native_tarball(tarball, content)
    return tarball


def _run_helper(tmp_path: Path, *, required_arch: str = "arm64") -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            str(HELPER),
            "--artifact-dir",
            str(tmp_path / "artifact"),
            "--lxserver-dir",
            str(tmp_path / "lxserver"),
            "--required-arch",
            required_arch,
            "--version-file",
            str(tmp_path / "lxserver.version"),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )


def _install_in_place(
    tmp_path: Path,
    *,
    version: str = LOCKED_VERSION,
    node_content: bytes = b"preexisting-binding",
    with_loader_copy: bool = True,
) -> Path:
    target_dir = tmp_path / "lxserver"
    named_dir = target_dir / "node_modules" / "music-tag-native-linux-arm64-gnu"
    named_dir.mkdir(parents=True, exist_ok=True)
    (named_dir / "package.json").write_text(
        json.dumps({"name": "test", "version": version}) + "\n", encoding="utf-8"
    )
    named_node = named_dir / NATIVE_NAME
    named_node.write_bytes(node_content)
    if with_loader_copy:
        loader_dir = target_dir / "node_modules" / "music-tag-native"
        loader_dir.mkdir(parents=True, exist_ok=True)
        (loader_dir / NATIVE_NAME).write_bytes(node_content)
    return named_node


def test_version_lock_pins_arm64_binding():
    text = (REPO_ROOT / "container" / "lxserver.version").read_text(encoding="utf-8")
    assert f"LXSERVER_NATIVE_VERSION={LOCKED_VERSION}" in text
    assert "LXSERVER_NATIVE_ARM64_SHA256=d3cfe25fc2e6f5d4d11861dadd54a62701b60c4a9901988befcdf4551c886461" in text


def test_helper_installs_locked_arm64_binding_offline(tmp_path):
    artifact_dir = tmp_path / "artifact"
    target_dir = tmp_path / "lxserver"
    artifact_dir.mkdir()
    loader_dir = target_dir / "node_modules" / "music-tag-native"
    loader_dir.mkdir(parents=True)
    (loader_dir / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    tarball = artifact_dir / f"music-tag-native-linux-arm64-gnu-{LOCKED_VERSION}.tgz"
    _write_native_tarball(tarball, b"locked-arm64-binding")
    _write_version_file(tmp_path / "lxserver.version", hashlib.sha256(tarball.read_bytes()).hexdigest())

    result = _run_helper(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "已补齐并通过校验" in result.stdout
    assert (loader_dir / NATIVE_NAME).read_bytes() == b"locked-arm64-binding"
    assert (
        target_dir / "node_modules" / "music-tag-native-linux-arm64-gnu" / NATIVE_NAME
    ).read_bytes() == b"locked-arm64-binding"


def test_helper_skips_in_place_module_that_passes_validation(tmp_path):
    tarball = _make_tarball(tmp_path)
    _write_version_file(tmp_path / "lxserver.version", hashlib.sha256(tarball.read_bytes()).hexdigest())
    named_node = _install_in_place(tmp_path, node_content=b"preexisting-binding")

    result = _run_helper(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "校验通过" in result.stdout
    # 合格的已落位模块不被重装覆盖
    assert named_node.read_bytes() == b"preexisting-binding"


def test_helper_reinstalls_in_place_module_with_wrong_version(tmp_path):
    tarball = _make_tarball(tmp_path)
    _write_version_file(tmp_path / "lxserver.version", hashlib.sha256(tarball.read_bytes()).hexdigest())
    named_node = _install_in_place(tmp_path, version="9.9.9", node_content=b"stale-binding")

    result = _run_helper(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "重新补装" in result.stdout
    assert named_node.read_bytes() == b"locked-arm64-binding"
    assert (tmp_path / "lxserver" / "node_modules" / "music-tag-native" / NATIVE_NAME).read_bytes() == (
        b"locked-arm64-binding"
    )


def test_helper_repairs_missing_loader_copy(tmp_path):
    tarball = _make_tarball(tmp_path)
    _write_version_file(tmp_path / "lxserver.version", hashlib.sha256(tarball.read_bytes()).hexdigest())
    _install_in_place(tmp_path, with_loader_copy=False)

    result = _run_helper(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "重新补装" in result.stdout
    assert (tmp_path / "lxserver" / "node_modules" / "music-tag-native" / NATIVE_NAME).read_bytes() == (
        b"locked-arm64-binding"
    )


def test_helper_aborts_when_repair_artifact_fails_checksum(tmp_path):
    # 在位模块版本不符触发重装，但缓存包与锁定 sha256 不符：必须中止而非放行
    _make_tarball(tmp_path, content=b"tampered-binding")
    _write_version_file(tmp_path / "lxserver.version", hashlib.sha256(b"locked-arm64-binding").hexdigest())
    _install_in_place(tmp_path, version="9.9.9")

    result = _run_helper(tmp_path)
    assert result.returncode != 0
    assert "重新补装" in result.stdout
    assert "校验和不符" in result.stderr


@pytest.mark.skipif(
    not (NODE_AVAILABLE and platform.machine() in ("x86_64", "amd64")),
    reason="需要 x86_64 宿主与 node 才能 dlopen 验证 x64 绑定",
)
def test_helper_rejects_corrupt_x64_binding(tmp_path):
    loader_dir = tmp_path / "lxserver" / "node_modules" / "music-tag-native"
    loader_dir.mkdir(parents=True)
    (loader_dir / X64_NATIVE_NAME).write_bytes(b"definitely not an elf module")
    (tmp_path / "lxserver.version").write_text(
        f"LXSERVER_NATIVE_VERSION={LOCKED_VERSION}\n", encoding="utf-8"
    )

    result = _run_helper(tmp_path, required_arch="x64")
    assert result.returncode != 0
    assert "无法加载" in result.stdout + result.stderr


@pytest.mark.parametrize("via_env", [False, True])
def test_helper_uses_selected_node_outside_path_for_arm64_repair(tmp_path, via_env):
    tarball = _make_tarball(tmp_path)
    _write_version_file(tmp_path / "lxserver.version", hashlib.sha256(tarball.read_bytes()).hexdigest())
    named = _install_in_place(tmp_path, node_content=b"corrupt-binding")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # 模拟 ARM64 宿主，且 PATH 中的 Node 不可用于实际校验。
    for name, body in (("uname", "printf 'aarch64\\n'\n"),
                       ("node", 'printf "WRONG_NODE\\n" >> "$NODE_LOG"; exit 1\n')):
        path = bindir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
    selected = tmp_path / "app-node"
    selected.write_text(
        "#!/bin/sh\n"
        'printf "selected\\n" >> "$NODE_LOG"\n'
        '[ "$1" = -e ] || exit 1\n'
        '[ "$(/bin/cat "$3")" = locked-arm64-binding ]\n'
    )
    selected.chmod(0o755)
    log = tmp_path / "node.log"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "NODE_LOG": str(log)}
    args = ["bash", str(HELPER), "--artifact-dir", str(tmp_path / "artifact"),
            "--lxserver-dir", str(tmp_path / "lxserver"),
            "--version-file", str(tmp_path / "lxserver.version")]
    if via_env:
        env["FNMUSIC_NODE_BIN"] = str(selected)
    else:
        env["FNMUSIC_NODE_BIN"] = str(bindir / "node")
        args += ["--node-bin", str(selected)]
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "重新补装" in result.stdout
    assert named.read_bytes() == b"locked-arm64-binding"
    assert log.read_text().splitlines() == ["selected", "selected"]


@pytest.mark.parametrize("arch", ["x64", "arm64"])
def test_helper_refuses_missing_node_on_matching_host(tmp_path, arch):
    tarball = _make_tarball(tmp_path)
    _write_version_file(tmp_path / "lxserver.version", hashlib.sha256(tarball.read_bytes()).hexdigest())
    _install_in_place(tmp_path)
    loader = tmp_path / "lxserver/node_modules/music-tag-native"
    (loader / X64_NATIVE_NAME).write_bytes(b"unchecked-x64-binding")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uname = bindir / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '{'aarch64' if arch == 'arm64' else 'x86_64'}\\n'\n")
    uname.chmod(0o755)
    result = subprocess.run(
        ["bash", str(HELPER), "--artifact-dir", str(tmp_path / "artifact"),
         "--lxserver-dir", str(tmp_path / "lxserver"),
         "--version-file", str(tmp_path / "lxserver.version"),
         "--required-arch", arch, "--node-bin", str(tmp_path / "missing-node")],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "无法执行 Node" in result.stdout + result.stderr
    assert "校验通过" not in result.stdout
