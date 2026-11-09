"""Read the installed official music version without trusting extension metadata."""
from __future__ import annotations

import logging
import re
import threading
import time

MANIFEST = "/var/apps/trim.music/manifest"
CACHE_SECONDS = 60.0
_lock = threading.Lock()
_cached_path = ""
_cached_until = 0.0
_cached_version: tuple[int, int, int] | None = None
_logger = logging.getLogger("fnmusic_proxy")


def installed_version(path: str | None = None) -> tuple[int, int, int] | None:
    """Return a strict three-component version, caching failures only briefly."""
    global _cached_path, _cached_until, _cached_version
    path = path or MANIFEST
    with _lock:
        now = time.monotonic()
        if path == _cached_path and now < _cached_until:
            return _cached_version
        version = None
        try:
            with open(path, encoding="utf-8") as manifest:
                text = manifest.read(65536)
            matches = re.findall(r"(?m)^\s*version\s*=\s*([^\s#]+)\s*(?:#.*)?$", text)
            if len(matches) == 1:
                raw = matches[0]
                if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
                    raw = raw[1:-1]
                if re.fullmatch(r"\d+\.\d+\.\d+", raw):
                    version = tuple(int(part) for part in raw.split("."))
        except (OSError, UnicodeError, ValueError):
            pass
        if version is None:
            _logger.warning("official music version unavailable; retaining legacy search behavior")
        _cached_path, _cached_until, _cached_version = path, now + CACHE_SECONDS, version
        return version


def supports_search_default_100() -> bool:
    version = installed_version()
    return version is not None and version >= (1, 1, 19)
