from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxy import fscheck


def test_probe_creates_and_removes_empty_file(tmp_path):
    assert fscheck.probe_dir_writable(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_persistent_eio_is_bounded_and_recovers(tmp_path, monkeypatch, caplog):
    unlink = os.unlink
    unlink_fails = True
    directory = str(tmp_path)
    clock = SimpleNamespace(monotonic=lambda: 5)
    monkeypatch.setattr(fscheck, "time", clock)

    def flaky_unlink(path, *args, **kwargs):
        if unlink_fails and Path(path).parent == tmp_path:
            raise OSError(errno.EIO, "test unlink failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", flaky_unlink)
        for _ in range(5):
            assert fscheck.probe_dir_writable(directory)
        assert len(caplog.records) == 1
        assert "errno=5" in caplog.records[0].message
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].stat().st_size == 0
        clock.monotonic = lambda: 3605
        assert fscheck.probe_dir_writable(directory)
        assert len(caplog.records) == 2

        unlink_fails = False
        assert fscheck.probe_dir_writable(directory)
        assert list(tmp_path.iterdir()) == []


def test_occupied_slot_is_not_modified(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    marker = tmp_path / ".fnmusic-fscheck-uid-1000"
    legacy = tmp_path / ".fnmusic-fscheck-1000-12345678"
    marker.write_bytes(b"unrelated contents")
    legacy.touch()

    assert fscheck.probe_dir_writable(str(tmp_path))
    assert marker.read_bytes() == b"unrelated contents"
    assert legacy.is_file()
    assert set(tmp_path.iterdir()) == {marker, legacy}


@pytest.mark.skipif(os.name == "nt", reason="Windows symlinks may require elevated privileges")
def test_probe_does_not_follow_link(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    marker = tmp_path / ".fnmusic-fscheck-uid-1000"
    target = tmp_path / "target"
    target.touch()
    marker.symlink_to(target)

    assert fscheck.probe_dir_writable(str(tmp_path))
    assert marker.is_symlink()
    assert target.is_file()


def test_create_denied_never_attempts_cleanup(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "test create denied")

    def unexpected_unlink(*args, **kwargs):
        raise AssertionError("a failed create must not delete anything")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", denied)
        patch.setattr(os, "unlink", unexpected_unlink)
        assert not fscheck.probe_dir_writable(str(tmp_path))


def test_occupied_slot_checks_directory_as_effective_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    marker = tmp_path / ".fnmusic-fscheck-uid-1000"
    marker.write_bytes(b"unrelated")

    def denied(path, mode, *, effective_ids=False):
        assert path == str(tmp_path)
        assert mode == os.W_OK | os.X_OK
        assert effective_ids
        return False

    monkeypatch.setattr(os, "access", denied)
    monkeypatch.setattr(os, "supports_effective_ids", {denied})
    assert not fscheck.probe_dir_writable(str(tmp_path))
    assert marker.read_bytes() == b"unrelated"
