"""State for short-lived direct-play targets."""
from __future__ import annotations

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

    def get(self, scope: str, guid: str) -> DirectPlayBinding | None:
        key = f"{scope}\0{guid}"
        binding = self._entries.get(key)
        if binding is None:
            return None
        if time.monotonic() >= binding.expires_at or binding.generation != binding.target.source_generation:
            return None
        return binding

    def known(self, scope: str, guid: str) -> bool:
        return f"{scope}\0{guid}" in self._entries

    def put(self, scope: str, guid: str, target: DirectMediaTarget, generation: int, ttl: float) -> DirectPlayBinding:
        now = time.monotonic()
        self.invalidate_source(target.provider, generation)
        binding = DirectPlayBinding(target, min(now + max(0.0, ttl), target.reuse_deadline), generation)
        self._entries[f"{scope}\0{guid}"] = binding
        if len(self._entries) > self.max_entries:
            expired = [key for key, value in self._entries.items() if value.expires_at <= now]
            for key in expired:
                self._entries.pop(key, None)
            while len(self._entries) > self.max_entries:
                oldest = min(self._entries, key=lambda key: self._entries[key].expires_at)
                self._entries.pop(oldest)
        return binding

    def invalidate_source(self, provider: str, generation: int) -> None:
        for key in list(self._entries):
            binding = self._entries[key]
            if binding.target.provider == provider and binding.generation != generation:
                self._entries[key] = DirectPlayBinding(binding.target, 0.0, binding.generation)


def target_still_valid(target: DirectMediaTarget, *, provider: str, generation: int) -> bool:
    return target.provider == provider and target.source_generation == generation
