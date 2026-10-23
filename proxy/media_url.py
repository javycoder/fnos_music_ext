"""Validation for externally redirected media URLs."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class MediaURLDecision:
    ok: bool
    reason: str = ""
    host: str = ""


def _unsafe_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = address.ipv4_mapped if address.version == 6 else None
    if mapped is not None and _unsafe_host(str(mapped)):
        return True
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_media_url(raw: str, *, require_https: bool = True, resolver=socket.getaddrinfo) -> MediaURLDecision:
    """Return whether a URL may be exposed to an external client.

    DNS validation checks every answer at publish time. It is not a full DNS
    rebinding defence, and callers must not describe it as one.
    """
    value = str(raw or "")
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return MediaURLDecision(False, "control_characters")
    if "\\" in value or "#" in value:
        return MediaURLDecision(False, "ambiguous_url")
    try:
        parsed = urlsplit(value, allow_fragments=False)
    except ValueError:
        return MediaURLDecision(False, "invalid_url")
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return MediaURLDecision(False, "invalid_scheme")
    if require_https and scheme != "https":
        return MediaURLDecision(False, "scheme_not_https")
    if not parsed.hostname:
        return MediaURLDecision(False, "missing_host")
    if parsed.username is not None or parsed.password is not None:
        return MediaURLDecision(False, "userinfo_present")
    try:
        port = parsed.port
    except ValueError:
        return MediaURLDecision(False, "invalid_port")
    if port is not None and not 1 <= port <= 65535:
        return MediaURLDecision(False, "invalid_port")

    host = parsed.hostname.rstrip(".").lower()
    if _unsafe_host(host):
        return MediaURLDecision(False, "host_not_public", host)
    try:
        records = resolver(host, None)
    except (OSError, UnicodeError):
        return MediaURLDecision(False, "dns_unresolved", host)
    addresses = set()
    try:
        for record in records or []:
            addresses.add(str(record[4][0]))
    except (IndexError, TypeError):
        return MediaURLDecision(False, "dns_invalid", host)
    if not addresses:
        return MediaURLDecision(False, "dns_unresolved", host)
    if any(_unsafe_host(address) for address in addresses):
        return MediaURLDecision(False, "resolved_address_not_public", host)
    return MediaURLDecision(True, host=host)


def hostname_for_log(raw: str) -> str:
    try:
        return (urlsplit(str(raw or "")).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""
