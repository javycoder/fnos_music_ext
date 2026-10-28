"""Safe HTTPS upgrade compatibility for NetEase audio direct play."""
from __future__ import annotations

import asyncio
import re
import socket
import ssl
import urllib.parse
from typing import Container
from urllib.parse import urlsplit, urlunsplit

import httpx

try:
    from .media_url import validate_media_url
except ImportError:
    from media_url import validate_media_url  # type: ignore

# Strict narrow CDN allowlist: matches m<digits>.music.126.net (e.g. m7, m8, m701, m801).
# Blanket *.126.net is strictly forbidden.
NETEASE_CDN_HOST_RE = re.compile(r"^m\d+\.music\.126\.net$", re.IGNORECASE)

# Standard browser UA for probe request; probe client must not carry Cookie or Referer.
DEFAULT_PROBE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MAX_PROBE_BYTES = 8192
DEFAULT_PROBE_TIMEOUT = 3.0
MAX_REDIRECT_HOPS = 3


class NeteaseDirectProbeError(RuntimeError):
    """Raised when probing an upgraded NetEase HTTPS target fails."""


def is_allowed_netease_cdn_host(
    host: str,
    extra_allowed_hosts: Container[str] | None = None,
) -> bool:
    """Check if host matches the narrow NetEase CDN allowlist."""
    normalized = str(host or "").rstrip(".").lower()
    if not normalized:
        return False
    if NETEASE_CDN_HOST_RE.fullmatch(normalized):
        return True
    if extra_allowed_hosts and normalized in extra_allowed_hosts:
        return True
    return False


def is_audio_magic(data: bytes) -> bool:
    """Identify recognized audio stream magic signatures from the prefix."""
    if len(data) < 4:
        return False
    # ID3v2 tag container (MP3, etc.)
    if data.startswith(b"ID3"):
        return True
    # FLAC stream
    if data.startswith(b"fLaC"):
        return True
    # Ogg stream container
    if data.startswith(b"OggS"):
        return True
    # WAV container
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return True
    # AIFF container
    if len(data) >= 12 and data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return True
    # MP4 / M4A / AAC container
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return True
    # Monkey's Audio (APE)
    if data.startswith(b"MAC "):
        return True
    # MPEG audio syncword (raw MP3 / MPEG-1/2/2.5 Audio)
    # 11 sync bits: 0xFF followed by high 3 bits set (>= 0xE0).
    # Layer bits (bits 1,2 of byte 1): 00 is reserved, so (byte1 & 0x06) != 0.
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and (data[1] & 0x06) != 0:
        return True
    # ADTS AAC syncword: 12 sync bits: 0xFF followed by high 4 bits set (0xF0).
    if data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:
        return True
    return False


def parse_netease_https_candidate(
    raw_url: str,
    *,
    extra_allowed_hosts: Container[str] | None = None,
) -> tuple[bool, str]:
    """Parse raw URL and determine if it can be upgraded to HTTPS.

    Returns:
        (should_probe, candidate_or_original_url)
        - If already HTTPS: returns (False, raw_url) to support without extra probe.
        - If eligible HTTP on allowlisted CDN: returns (True, https_candidate_url).
        - If ineligible (non-HTTP, credentials, non-standard port, disallowed host):
          returns (False, raw_url) to preserve original URL for downstream rejection.
    """
    val = str(raw_url or "").strip()
    if not val or any(ord(c) <= 32 or ord(c) == 127 for c in val):
        return False, raw_url
    if "\\" in val or "#" in val:
        return False, raw_url
    try:
        parsed = urlsplit(val, allow_fragments=False)
    except ValueError:
        return False, raw_url

    scheme = parsed.scheme.lower()
    if scheme == "https":
        return False, raw_url
    if scheme != "http":
        return False, raw_url

    if parsed.username is not None or parsed.password is not None:
        return False, raw_url

    try:
        port = parsed.port
    except ValueError:
        return False, raw_url
    if port is not None and port != 80:
        return False, raw_url

    host = (parsed.hostname or "").rstrip(".").lower()
    if not is_allowed_netease_cdn_host(host, extra_allowed_hosts=extra_allowed_hosts):
        return False, raw_url

    # Standard HTTP upgraded to HTTPS: preserve path and query verbatim
    candidate = urlunsplit(("https", host, parsed.path, parsed.query, ""))
    return True, candidate


async def _probe_stream_prefix(
    client: httpx.AsyncClient,
    candidate_url: str,
    *,
    resolver=socket.getaddrinfo,
    extra_allowed_hosts: Container[str] | None = None,
    max_probe_bytes: int = MAX_PROBE_BYTES,
) -> str:
    """Perform bounded streamed prefix probe over candidate HTTPS URL."""
    current_url = candidate_url

    for _ in range(MAX_REDIRECT_HOPS + 1):
        parsed = urlsplit(current_url, allow_fragments=False)
        scheme = parsed.scheme.lower()
        if scheme != "https":
            raise NeteaseDirectProbeError(f"redirect_non_https:{scheme}")

        if parsed.username is not None or parsed.password is not None:
            raise NeteaseDirectProbeError("redirect_userinfo_present")

        try:
            port = parsed.port
        except ValueError:
            raise NeteaseDirectProbeError("redirect_invalid_port")
        if port not in (None, 443):
            raise NeteaseDirectProbeError(f"redirect_non_standard_port:{port}")

        host = (parsed.hostname or "").rstrip(".").lower()
        if not is_allowed_netease_cdn_host(host, extra_allowed_hosts=extra_allowed_hosts):
            raise NeteaseDirectProbeError(f"redirect_disallowed_host:{host}")

        decision = await asyncio.to_thread(validate_media_url, current_url, resolver=resolver)
        if not decision.ok:
            raise NeteaseDirectProbeError(f"dns_safety_rejected:{decision.reason}")

        # Clear cookiejar before every hop to strictly prevent carrying cookies across hops/hosts
        client.cookies.clear()

        headers = {
            "User-Agent": DEFAULT_PROBE_UA,
            "Range": f"bytes=0-{max_probe_bytes - 1}",
            "Accept-Encoding": "identity",
        }

        async with client.stream("GET", current_url, headers=headers, follow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise NeteaseDirectProbeError("redirect_missing_location")
                current_url = urllib.parse.urljoin(current_url, location)
                continue

            if resp.status_code not in (200, 206):
                raise NeteaseDirectProbeError(f"http_status_{resp.status_code}")

            if resp.status_code == 206:
                content_range = resp.headers.get("content-range", "").strip().lower()
                if not re.match(r"^bytes\s+0-", content_range):
                    raise NeteaseDirectProbeError(f"invalid_content_range:{content_range}")

            content_type = resp.headers.get("content-type", "").strip().lower()
            if content_type.startswith(("text/", "application/json", "application/xml")):
                raise NeteaseDirectProbeError(f"invalid_content_type:{content_type}")

            encoding = resp.headers.get("content-encoding", "").strip().lower()
            if encoding and encoding != "identity":
                raise NeteaseDirectProbeError(f"non_identity_encoding:{encoding}")

            buffer = bytearray()
            if resp.is_stream_consumed:
                buffer.extend(resp.content[:max_probe_bytes])
            else:
                async for chunk in resp.aiter_raw(chunk_size=max_probe_bytes):
                    remaining = max_probe_bytes - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    if len(buffer) >= 12 and is_audio_magic(bytes(buffer)):
                        break
                    if len(buffer) >= max_probe_bytes:
                        break

            if not is_audio_magic(bytes(buffer)):
                raise NeteaseDirectProbeError("invalid_audio_magic")

            return current_url

    raise NeteaseDirectProbeError("too_many_redirects")


async def upgrade_netease_media_url(
    raw_url: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    resolver=socket.getaddrinfo,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    verify: bool | ssl.SSLContext = True,
    extra_allowed_hosts: Container[str] | None = None,
) -> str:
    """Safely upgrade eligible NetEase HTTP audio URLs to validated HTTPS URLs.

    Rules:
    - If already HTTPS: returns raw_url unchanged without extra probe.
    - If HTTP on allowlisted NetEase CDN (m<digits>.music.126.net):
      Validates DNS safety via validate_media_url (async to thread), probes streaming prefix
      (status 200/206, start 0 on 206, non-text/json content-type, identity encoding,
       audio magic header) within total timeout (<=3s),
      and returns the verified final HTTPS URL.
      If probe fails, raises NeteaseDirectProbeError (RuntimeError) explicitly
      to prevent unvalidated playback without proxy fallback.
    - If non-HTTPS or disallowed host / port / userinfo:
      Returns raw_url unchanged so existing validation downstream can reject it.
    """
    should_probe, candidate = parse_netease_https_candidate(
        raw_url, extra_allowed_hosts=extra_allowed_hosts,
    )
    if not should_probe:
        return candidate

    async def _do_probe() -> str:
        # Pre-probe DNS safety validation (non-blocking thread)
        dns_decision = await asyncio.to_thread(validate_media_url, candidate, resolver=resolver)
        if not dns_decision.ok:
            raise NeteaseDirectProbeError(f"dns_safety_rejected:{dns_decision.reason}")

        if client is not None:
            return await _probe_stream_prefix(
                client,
                candidate,
                resolver=resolver,
                extra_allowed_hosts=extra_allowed_hosts,
            )
        async with httpx.AsyncClient(
            transport=transport,
            verify=verify,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
        ) as probe_client:
            return await _probe_stream_prefix(
                probe_client,
                candidate,
                resolver=resolver,
                extra_allowed_hosts=extra_allowed_hosts,
            )

    try:
        return await asyncio.wait_for(_do_probe(), timeout=timeout)
    except asyncio.TimeoutError:
        raise NeteaseDirectProbeError("probe_timeout")
    except NeteaseDirectProbeError:
        raise
    except Exception as exc:
        raise NeteaseDirectProbeError(f"probe_exception:{type(exc).__name__}") from exc
