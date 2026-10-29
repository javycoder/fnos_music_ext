"""下载目录自动判定：真实挂载和显式配置优先于路径外观。"""
import io

import pytest

from proxy import tee_save_mode as mode


@pytest.mark.parametrize("setting,expected", [("direct", False), ("staged", True)])
def test_explicit_mode_overrides_mount_detection(monkeypatch, setting, expected):
    monkeypatch.setattr(mode, "mount_fstype", lambda _: pytest.fail("explicit mode needs no probe"))
    assert mode.should_stage("/vol02/1000-1-abcdef/music", "/cache", setting)[0] is expected


@pytest.mark.parametrize("path", ["/vol02/1000-1-abcdef/music", "/vol10/1000-1-abcdef/music"])
def test_remote_fnos_volume_is_staged(monkeypatch, path):
    monkeypatch.setattr(mode.os.path, "realpath", lambda p: p)
    assert mode.should_stage(path, "/cache")[0] is True


def test_remote_mount_nested_in_local_fnos_volume_is_staged(monkeypatch):
    monkeypatch.setattr(mode.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(mode, "mount_fstype", lambda _: "fuse.rclone")
    assert mode.should_stage("/vol1/1000/cloud/music", "/vol1/1000/cache")[0] is True


def test_symlink_into_remote_volume_is_staged(monkeypatch):
    monkeypatch.setattr(mode.os.path, "realpath", lambda _: "/vol02/1000-1-abcdef/music")
    assert mode.should_stage("/vol1/1000/music-link", "/cache")[0] is True


def test_unknown_volume_name_uses_mount_probe(monkeypatch):
    monkeypatch.setattr(mode.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(mode, "mount_fstype", lambda _: "nfs4")
    assert mode.classify_fnos_path("/vol1/shared/music")[0] is None
    assert mode.should_stage("/vol1/shared/music", "/cache")[0] is True


@pytest.mark.parametrize("device,expected", [(1, False), (2, True), (None, True)])
def test_generic_directory_fallback(monkeypatch, device, expected):
    monkeypatch.setattr(mode.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(mode, "mount_fstype", lambda _: None)
    monkeypatch.setattr(mode, "_st_dev", lambda path: device if path == "/music" else 1)
    assert mode.should_stage("/music", "/cache")[0] is expected


def test_mount_probe_uses_longest_prefix_and_escaped_spaces(monkeypatch):
    mounts = "root / ext4 rw 0 0\ncloud /music\\040disk fuse.rclone rw 0 0\n"
    monkeypatch.setattr(mode.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(mode, "open", lambda *a, **kw: io.StringIO(mounts), raising=False)
    assert mode.mount_fstype("/music disk/album") == "fuse.rclone"
    assert mode.mount_fstype("/music diskette/album") == "ext4"
