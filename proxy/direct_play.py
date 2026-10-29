"""State for short-lived direct-play targets."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


@dataclass
class DirectMediaTarget:
    provider: str
    platform: str
    canonical_id: str
    url: str
    protocol: str = "https"
    server_headers: dict[str, str] = field(default_factory=dict)
    ext: str = ""
    actual_quality: str = ""
    file_size: int | None = None
    resolved_at: float = field(default_factory=time.time)
    reuse_deadline: float = 0.0
    upstream_expires_at: float | None = None
    expiry_source: str = "unknown"
    source_generation: int = 0
    representation_key: str = ""
    selection_id: str = ""


@dataclass(frozen=True)
class DirectPlayBinding:
    target: DirectMediaTarget
    expires_at: float
    generation: int


class DirectTargetBindings:
    """Bounded same-rendition target pins scoped by the caller's key."""

    def __init__(self, max_entries: int = 512):
        self.max_entries = max_entries
        self._entries: dict[str, DirectPlayBinding] = {}
        self._known: dict[str, None] = {}

    def get(self, scope: str, guid: str) -> DirectPlayBinding | None:
        key = f"{scope}\0{guid}"
        binding = self._entries.get(key)
        if binding is None:
            return None
        if time.monotonic() >= binding.expires_at or binding.generation != binding.target.source_generation:
            self._entries.pop(key, None)
            return None
        return binding

    def known(self, scope: str, guid: str) -> bool:
        return f"{scope}\0{guid}" in self._known

    def forget(self, scope: str, guid: str) -> None:
        key = f"{scope}\0{guid}"
        self._entries.pop(key, None)
        self._known.pop(key, None)

    def put(self, scope: str, guid: str, target: DirectMediaTarget, generation: int, ttl: float) -> DirectPlayBinding:
        now = time.monotonic()
        self.invalidate_source(target.provider, generation)
        for expired in [key for key, value in self._entries.items() if value.expires_at <= now]:
            self._entries.pop(expired, None)
        binding = DirectPlayBinding(target, min(now + max(0.0, ttl), target.reuse_deadline), generation)
        key = f"{scope}\0{guid}"
        self._entries[key] = binding
        self._known.pop(key, None)
        self._known[key] = None
        while len(self._known) > self.max_entries:
            oldest = next(iter(self._known))
            self._known.pop(oldest, None)
            self._entries.pop(oldest, None)
        return binding

    def invalidate_source(self, provider: str, generation: int) -> None:
        for key in list(self._entries):
            binding = self._entries[key]
            if binding.target.provider == provider and binding.generation != generation:
                self._entries.pop(key, None)


class FileTargetBindings:
    def __init__(self, max_entries: int = 512):
        self.max_entries = max_entries
        self._entries: dict[tuple[str, str], tuple] = {}

    @staticmethod
    def fingerprint(path: str) -> tuple:
        stat = os.stat(path)
        return path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def remember(self, scope: str, guid: str, path: str) -> None:
        key = (scope, guid)
        self._entries.pop(key, None)
        self._entries[key] = (time.monotonic() + 300.0, self.fingerprint(path))
        while len(self._entries) > self.max_entries:
            self._entries.pop(next(iter(self._entries)), None)

    def forget(self, scope: str, guid: str) -> None:
        self._entries.pop((scope, guid), None)

    def matches(self, scope: str, guid: str, path: str | None = None) -> bool:
        entry = self._entries.get((scope, guid))
        if entry is None or entry[0] <= time.monotonic():
            return False
        try:
            return entry[1] == self.fingerprint(path or entry[1][0])
        except OSError:
            return False
