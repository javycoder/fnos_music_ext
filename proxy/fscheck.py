"""Bounded write probes for both the proxy and desktop gateway."""
from __future__ import annotations

import logging
import os
import stat
import threading
import time
from collections import OrderedDict

_logger = logging.getLogger(__name__)
_warning_times: OrderedDict[str, float] = OrderedDict()
_warning_lock = threading.Lock()
_warning_interval = 3600
_warning_limit = 1024


def _warn_cleanup(directory: str, error: OSError) -> None:
    now = time.monotonic()
    with _warning_lock:
        last = _warning_times.get(directory)
        if last is not None and now - last < _warning_interval:
            return
        _warning_times[directory] = now
        _warning_times.move_to_end(directory)
        if len(_warning_times) > _warning_limit:
            _warning_times.popitem(last=False)
    _logger.warning("Directory write probe cleanup failed: directory=%r errno=%s",
                    directory, error.errno)


def _is_empty_probe(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_size == 0 and info.st_nlink == 1


def _remove_probe(directory: str, path: str, expected: os.stat_result) -> bool:
    try:
        current = os.lstat(path)
        if not os.path.samestat(current, expected) or not _is_empty_probe(current):
            return False
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        _warn_cleanup(directory, error)
        return False
    return True


def _recover_probe(directory: str, path: str, uid: int) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return (uid == info.st_uid and _is_empty_probe(info)
            and _remove_probe(directory, path, info))


def probe_dir_writable(directory: str) -> bool:
    """Try exclusive creation, falling back to directory access if the slot stays occupied."""
    uid = getattr(os, "geteuid", lambda: 0)()
    path = os.path.join(directory, f".fnmusic-fscheck-uid-{uid}")
    for attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        except FileExistsError:
            if attempt == 0 and _recover_probe(directory, path, uid):
                continue
            mode = os.W_OK | os.X_OK
            if os.access in os.supports_effective_ids:
                return os.access(directory, mode, effective_ids=True)
            return os.access(directory, mode)
        except OSError:
            return False

        created = None
        try:
            try:
                created = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            return False
        finally:
            if created is not None:
                _remove_probe(directory, path, created)
        return True
    return False
