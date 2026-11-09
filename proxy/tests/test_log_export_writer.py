from __future__ import annotations

import io
import importlib.util
import os
import zipfile
from pathlib import Path

import pytest
_spec = importlib.util.spec_from_file_location(
    "log_export_writer", Path(__file__).resolve().parents[1] / "log_export_writer.py")
writer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(writer)


def test_write_archive_creates_exclusive_valid_zip(tmp_path: Path):
    target = tmp_path / "logs.zip"
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("proxy.log", "safe\n")
    source.seek(0)

    size = writer.write_archive(source, target)

    assert size == target.stat().st_size
    with zipfile.ZipFile(target) as archive:
        assert archive.read("proxy.log") == b"safe\n"


def test_write_archive_removes_invalid_partial_output(tmp_path: Path):
    target = tmp_path / "logs.zip"
    try:
        writer.write_archive(io.BytesIO(b"PK\x03\x04truncated"), target)
    except Exception:
        pass
    else:
        raise AssertionError("invalid ZIP must fail")
    assert not target.exists()


def test_write_archive_does_not_replace_existing_file(tmp_path: Path):
    target = tmp_path / "logs.zip"
    target.write_text("existing", encoding="utf-8")
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("proxy.log", "safe\n")
    source.seek(0)
    try:
        writer.write_archive(source, target)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing target must not be replaced")
    assert target.read_text(encoding="utf-8") == "existing"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform cannot create symlinks")
def test_write_archive_rejects_symlink_parent(tmp_path: Path):
    directory = tmp_path / "directory"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("proxy.log", "safe\n")
    source.seek(0)
    with pytest.raises(OSError):
        writer.write_archive(source, link / "logs.zip")
    assert not list(directory.iterdir())
