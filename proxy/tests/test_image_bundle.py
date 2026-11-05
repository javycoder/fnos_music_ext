"""3.0.0+：随包应用镜像资产（assets/image）的校验、复用与导入路径测试。

- image_bundle.py：清单字段/架构/归档 hash 的 fail-closed 校验；镜像已就绪时
  直接复用（不重新导入）；镜像按 ID 在场但引用缺失时仅补 tag；完全缺失时走
  docker load 导入并复核身份；架构不匹配默认拒绝、显式放行才继续。
- install.sh / extend.sh / install_common.sh：镜像保障统一走 ensure_app_image
  入口（离线资产优先，git 检出无资产时回落 ensure_image_current 现场构建）。

docker 调用全部桩化（monkeypatch），不触碰宿主 Docker、网络与运行中容器。
"""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("ib", BASE / "proxy/image_bundle.py")
ib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ib)

ARCHIVE_BYTES = b"fake-docker-save-archive"


def write_bundle(tmp_path: Path, **overrides) -> Path:
    bundle = tmp_path / "assets" / "image"
    bundle.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1,
        "app": "fnmusic-ext",
        "arch": "amd64",
        "image_tag": "fnmusic-sources:latest",
        "image_id": "sha256:" + "a" * 64,
        "rootfs_diff_ids": ["sha256:" + "b" * 64, "sha256:" + "c" * 64],
        "archive": "fnmusic-sources-image.tar.gz",
        "archive_sha256": hashlib.sha256(ARCHIVE_BYTES).hexdigest(),
        "archive_bytes": len(ARCHIVE_BYTES),
    }
    manifest.update(overrides)
    (bundle / "fnmusic-sources-image.tar.gz").write_bytes(ARCHIVE_BYTES)
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def matching_local(extra_os="linux", extra_arch="amd64"):
    return {
        "id": "sha256:" + "a" * 64,
        "diff_ids": ["sha256:" + "b" * 64, "sha256:" + "c" * 64],
        "os": extra_os,
        "arch": extra_arch,
    }


# ---------------------------------------------------------------- check 校验

def test_check_ok(tmp_path):
    bundle = write_bundle(tmp_path)
    manifest, archive = ib.check(bundle)
    assert manifest["arch"] == "amd64"
    assert archive.name == "fnmusic-sources-image.tar.gz"


def test_check_missing_manifest(tmp_path):
    with pytest.raises(ib.BundleError):
        ib.check(tmp_path / "assets" / "image")


def test_check_rejects_bad_schema_and_fields(tmp_path):
    bundle = write_bundle(tmp_path, schema=2)
    with pytest.raises(ib.BundleError, match="schema"):
        ib.check(bundle)
    bundle = write_bundle(tmp_path, archive_sha256="")
    with pytest.raises(ib.BundleError, match="缺少字段"):
        ib.check(bundle)


def test_check_rejects_size_or_hash_mismatch(tmp_path):
    bundle = write_bundle(tmp_path, archive_bytes=1)
    with pytest.raises(ib.BundleError, match="大小不符"):
        ib.check(bundle)
    bundle = write_bundle(tmp_path, archive_sha256="0" * 64)
    with pytest.raises(ib.BundleError, match="sha256 不符"):
        ib.check(bundle)


def test_check_arch_expectation(tmp_path):
    bundle = write_bundle(tmp_path, arch="arm64")
    assert ib.check(bundle, expect_arch="arm64")[0]["arch"] == "arm64"
    with pytest.raises(ib.BundleError, match="架构"):
        ib.check(bundle, expect_arch="amd64")


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    bundle = write_bundle(tmp_path)
    assert ib.main(["check", "--bundle-dir", str(bundle)]) == 0
    monkeypatch.setattr(ib, "host_arch", lambda: "amd64")
    monkeypatch.setattr(ib, "load_archive",
                        lambda *_a: (_ for _ in ()).throw(ib.BundleError("docker load 失败")))
    assert ib.main(["ensure", "--bundle-dir", str(bundle),
                    "--docker-cmd", "true"]) == 2
    assert "资产不可用" in capsys.readouterr().err


# ---------------------------------------------------------------- ensure 流程

def test_ensure_reuses_loaded_image_without_load(tmp_path, monkeypatch):
    bundle = write_bundle(tmp_path)
    monkeypatch.setattr(ib, "host_arch", lambda: "amd64")
    monkeypatch.setattr(ib, "inspect_image", lambda docker, ref: matching_local())

    def _no_load(*_a, **_k):
        raise AssertionError("已就绪镜像不得重新导入")
    monkeypatch.setattr(ib, "load_archive", _no_load)
    manifest = ib.ensure(bundle, docker_cmd="docker")
    assert manifest["image_id"] == "sha256:" + "a" * 64


def test_ensure_repairs_missing_tag_by_id(tmp_path, monkeypatch):
    bundle = write_bundle(tmp_path)
    monkeypatch.setattr(ib, "host_arch", lambda: "amd64")
    calls = []

    def fake_run(cmd, **_k):
        calls.append(cmd)

    def fake_inspect(docker, ref):
        # 引用已丢（tag 查不到），但镜像本体按 ID 仍在
        return matching_local() if ref.startswith("sha256:") else None
    monkeypatch.setattr(ib, "run", fake_run)
    monkeypatch.setattr(ib, "inspect_image", fake_inspect)
    monkeypatch.setattr(ib, "load_archive",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("不应导入")))
    ib.ensure(bundle, docker_cmd="docker")
    assert ["docker", "tag", "sha256:" + "a" * 64, "fnmusic-sources:latest"] in calls


def test_ensure_loads_and_verifies(tmp_path, monkeypatch):
    bundle = write_bundle(tmp_path)
    monkeypatch.setattr(ib, "host_arch", lambda: "amd64")
    loaded = {"n": 0}

    def fake_inspect(docker, ref):
        # 前两次（tag / by-id）都不存在；load_archive 之后第三次返回匹配镜像
        loaded["n"] += 1
        return matching_local() if loaded["n"] >= 3 else None

    load_calls = []
    monkeypatch.setattr(ib, "inspect_image", fake_inspect)
    monkeypatch.setattr(ib, "load_archive",
                        lambda docker, archive: load_calls.append(archive.name))
    ib.ensure(bundle, docker_cmd="docker")
    assert load_calls == ["fnmusic-sources-image.tar.gz"]


def test_ensure_rejects_identity_drift_after_load(tmp_path, monkeypatch):
    bundle = write_bundle(tmp_path)
    monkeypatch.setattr(ib, "host_arch", lambda: "amd64")
    monkeypatch.setattr(ib, "inspect_image",
                        lambda docker, ref: {**matching_local(), "id": "sha256:" + "d" * 64}
                        if "latest" in str(ref) else None)
    monkeypatch.setattr(ib, "load_archive", lambda *_a: None)
    with pytest.raises(ib.BundleError, match="身份与清单不符"):
        ib.ensure(bundle, docker_cmd="docker")


def test_ensure_accepts_inspect_without_diff_ids(tmp_path, monkeypatch):
    """containerd image store 的 inspect 不暴露 RootFS.DiffIds。

    image_id（config 摘要）+ os/arch 匹配即视为同一镜像，不得因 inspect
    缺字段而误判不符、反复导入。
    """
    bundle = write_bundle(tmp_path)
    monkeypatch.setattr(ib, "host_arch", lambda: "amd64")
    monkeypatch.setattr(
        ib, "inspect_image",
        lambda docker, ref: {k: v for k, v in matching_local().items()
                             if k != "diff_ids"})
    monkeypatch.setattr(ib, "load_archive",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("不应导入")))
    ib.ensure(bundle, docker_cmd="docker")


def test_ensure_rejects_diff_ids_drift_when_inspect_provides(tmp_path, monkeypatch):
    """inspect 提供了 diff_ids 且与清单不一致（同 id 不同层描述）仍须拒绝。"""
    bundle = write_bundle(tmp_path)
    monkeypatch.setattr(ib, "host_arch", lambda: "amd64")
    monkeypatch.setattr(ib, "inspect_image", lambda docker, ref: {
        **matching_local(), "diff_ids": ["sha256:" + "e" * 64]})
    monkeypatch.setattr(ib, "load_archive", lambda *_a: None)
    with pytest.raises(ib.BundleError, match="身份与清单不符"):
        ib.ensure(bundle, docker_cmd="docker")


def test_ensure_arch_mismatch_guarded(tmp_path, monkeypatch):
    bundle = write_bundle(tmp_path, arch="arm64")
    monkeypatch.setattr(ib, "host_arch", lambda: "amd64")
    state = {"n": 0}

    def fake_inspect(_docker, ref):
        state["n"] += 1
        return matching_local(extra_arch="arm64") if state["n"] >= 3 else None

    monkeypatch.setattr(ib, "inspect_image", fake_inspect)
    monkeypatch.setattr(ib, "load_archive", lambda *_a: None)
    with pytest.raises(ib.BundleError, match="架构"):
        ib.ensure(bundle, docker_cmd="docker")
    ib.ensure(bundle, docker_cmd="docker", allow_mismatch=True)  # 显式放行


# ------------------------------------------------- 安装入口接线（防回归）

def test_install_entrypoints_use_ensure_app_image():
    install = (BASE / "install.sh").read_text(encoding="utf-8")
    extend = (BASE / "extend.sh").read_text(encoding="utf-8")
    common = (BASE / "proxy/install_common.sh").read_text(encoding="utf-8")
    assert "ensure_app_image" in common and "ensure_image_current\n}" in common.replace("    ", "")
    assert "if ! ensure_app_image; then" in install
    assert "ensure_app_image || exit 1" in extend
    # 逃生口与强制开关必须同时存在
    assert "FNMUSIC_IMAGE_BUNDLE" in common


def test_extend_search_verification_opt_in():
    extend = (BASE / "extend.sh").read_text(encoding="utf-8")
    # 3.0.0+：外网搜索验收默认跳过，只有 FNMUSIC_VERIFY_SEARCH=1 才执行
    assert 'FNMUSIC_VERIFY_SEARCH:-0' in extend
    assert extend.index("验收 6b 跳过") < extend.index("验收 6b: 验证音源搜索连通性")
