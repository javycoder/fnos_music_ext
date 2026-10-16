import hashlib
import os
import subprocess
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "container" / "ensure_lxserver_native.sh"


def _write_native_tarball(path: Path, content: bytes = b"arm64-binding") -> None:
    package_dir = path.parent / "package-source"
    binary = package_dir / "package" / "music-tag-native.linux-arm64-gnu.node"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(content)
    (binary.parent / "package.json").write_text('{"name":"test"}\n', encoding="utf-8")
    with tarfile.open(path, "w:gz") as archive:
        archive.add(binary.parent / "package.json", arcname="package/package.json")
        archive.add(binary, arcname="package/music-tag-native.linux-arm64-gnu.node")


def test_version_lock_pins_arm64_binding():
    text = (REPO_ROOT / "container" / "lxserver.version").read_text(encoding="utf-8")
    assert "LXSERVER_NATIVE_VERSION=0.2.5" in text
    assert "LXSERVER_NATIVE_ARM64_SHA256=d3cfe25fc2e6f5d4d11861dadd54a62701b60c4a9901988befcdf4551c886461" in text


def test_helper_installs_locked_arm64_binding_offline(tmp_path):
    artifact_dir = tmp_path / "artifact"
    target_dir = tmp_path / "lxserver"
    artifact_dir.mkdir()
    loader_dir = target_dir / "node_modules" / "music-tag-native"
    loader_dir.mkdir(parents=True)
    (loader_dir / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    tarball = artifact_dir / "music-tag-native-linux-arm64-gnu-0.2.5.tgz"
    _write_native_tarball(tarball, b"locked-arm64-binding")
    version_file = tmp_path / "lxserver.version"
    version_file.write_text(
        "LXSERVER_NATIVE_VERSION=0.2.5\n"
        f"LXSERVER_NATIVE_ARM64_SHA256={hashlib.sha256(tarball.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            str(HELPER),
            "--artifact-dir",
            str(artifact_dir),
            "--lxserver-dir",
            str(target_dir),
            "--required-arch",
            "arm64",
            "--version-file",
            str(version_file),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )
    assert "原生模块已补齐" in result.stdout
    assert (
        loader_dir / "music-tag-native.linux-arm64-gnu.node"
    ).read_bytes() == b"locked-arm64-binding"
    assert (
        target_dir
        / "node_modules"
        / "music-tag-native-linux-arm64-gnu"
        / "music-tag-native.linux-arm64-gnu.node"
    ).read_bytes() == b"locked-arm64-binding"


def test_helper_rejects_checksum_mismatch(tmp_path):
    artifact_dir = tmp_path / "artifact"
    target_dir = tmp_path / "lxserver"
    artifact_dir.mkdir()
    loader_dir = target_dir / "node_modules" / "music-tag-native"
    loader_dir.mkdir(parents=True)
    tarball = artifact_dir / "music-tag-native-linux-arm64-gnu-0.2.5.tgz"
    _write_native_tarball(tarball)
    version_file = tmp_path / "lxserver.version"
    version_file.write_text(
        "LXSERVER_NATIVE_VERSION=0.2.5\n"
        "LXSERVER_NATIVE_ARM64_SHA256=not-the-locked-sha256\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            str(HELPER),
            "--artifact-dir",
            str(artifact_dir),
            "--lxserver-dir",
            str(target_dir),
            "--required-arch",
            "arm64",
            "--version-file",
            str(version_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )
    assert result.returncode != 0
    assert "校验和不符" in result.stderr
