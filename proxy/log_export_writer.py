"""Write one log archive from stdin as the selected fnOS user."""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path


def _open_exclusive(target: Path) -> int:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    if os.name != "posix":
        for parent in target.parents:
            if parent.is_symlink():
                raise OSError("symlink in target path")
        return os.open(target, flags, 0o600)

    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in target.parent.parts[1:]:
            next_descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return os.open(target.name, flags, 0o600, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def write_archive(stream, target: Path) -> int:
    """Exclusively create target, stream bytes in, then validate its ZIP footer."""
    descriptor = _open_exclusive(target)
    try:
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        with zipfile.ZipFile(target, "r") as archive:
            if not archive.namelist():
                raise ValueError("empty archive")
        return os.stat(target).st_size
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not args[0].startswith("/"):
        print("usage: log_export_writer.py TARGET", file=sys.stderr)
        return 2
    try:
        write_archive(sys.stdin.buffer, Path(args[0]))
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
