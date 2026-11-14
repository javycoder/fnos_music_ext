"""Legacy NetEase/musicbox migration and identity preservation.

Centralizes backward compatibility for:
1. .env configuration keys migration (MUSICBOX_* -> NETEASE_*).
2. Legacy cookie.txt (MozillaCookieJar) import into netease/session.json.
3. Session persistence with strict 0700/0600 permissions and atomic replacement.
4. Public playlist provider and GUID compatibility (freezing legacy 'musicbox' recognizers).
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("proxy.netease_legacy")

KEY_MIGRATION_MAP = {
    "FNMUSIC_MUSICBOX_URL": "FNMUSIC_NETEASE_URL",
    "WEBUI_MUSICBOX_URL": "WEBUI_NETEASE_URL",
    "FNMUSIC_MUSICBOX_ENABLED": "FNMUSIC_NETEASE_ENABLED",
}


def normalize_provider(provider: str | None) -> str:
    """Normalize legacy 'musicbox' provider identifier to 'netease'."""
    p = str(provider or "").strip().lower()
    if p == "musicbox":
        return "netease"
    return p


def is_netease_provider(provider: str | None) -> bool:
    """Recognize both 'netease' and legacy 'musicbox' as the NetEase music source."""
    p = str(provider or "").strip().lower()
    return p in ("netease", "musicbox")


def migrate_env_config(env_path: Path | str) -> bool:
    """Migrate legacy MUSICBOX_* configuration keys to NETEASE_* keys in .env.

    Idempotent and atomic:
    - If the new key is already set, it takes precedence and is NOT overwritten.
    - If the old key is set and the new key is absent, migrate value to new key.
    - Uses env_file_lock and atomic write from env_merge.
    """
    path = Path(env_path)
    if not path.is_file():
        return False

    from proxy.env_merge import env_file_lock, parse_env_file, render_env, write_env_atomic

    with env_file_lock(path, timeout=10.0):
        entries, non_assignment = parse_env_file(path)
        env_dict = {k: v for k, v in entries}
        changed = False

        for old_key, new_key in KEY_MIGRATION_MAP.items():
            if old_key in env_dict and new_key not in env_dict:
                # Old key exists, new key absent -> copy to new key
                val = env_dict[old_key]
                entries.append((new_key, val))
                env_dict[new_key] = val
                changed = True
            elif old_key in env_dict and new_key in env_dict:
                # Both exist: new key takes precedence
                pass

        if changed:
            content = render_env(entries, trailing=non_assignment)
            write_env_atomic(path, content)
            logger.info("Migrated legacy NetEase environment keys in %s", path)
            return True

    return False


def import_legacy_cookie(data_dir: Path | str) -> dict[str, Any] | None:
    """Read NetEase cookies from legacy MozillaCookieJar (cookie.txt).

    Filters out expired cookies individually; non-expired cookies are preserved.
    Returns dict of {cookie_name: cookie_value, ...} or None if not found/invalid.
    """
    data_path = Path(data_dir)
    candidate_files = [
        data_path / "netease-musicbox" / "cookie.txt",
        data_path / "cookie.txt",
        data_path / "config" / "netease-musicbox" / "cookie.txt",
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "netease-musicbox" / "cookie.txt",
        Path.home() / ".netease-musicbox" / "cookie.txt",
    ]

    now = time.time()
    for cookie_file in candidate_files:
        try:
            if not cookie_file.is_file() or not cookie_file.stat().st_size:
                continue
            try:
                jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                text = cookie_file.read_text(encoding="utf-8")
                if not text.startswith("# Netscape HTTP Cookie File"):
                    text = "# Netscape HTTP Cookie File\n" + text
                with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tmp:
                    tmp.write(text)
                    tmp_name = tmp.name
                try:
                    jar = http.cookiejar.MozillaCookieJar(tmp_name)
                    jar.load(ignore_discard=True, ignore_expires=True)
                finally:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
        except Exception as exc:
            logger.warning("Failed to parse legacy cookie file %s: %s", cookie_file, exc)
            continue

        valid_cookies: dict[str, str] = {}
        max_expires = 0
        for cookie in jar:
            domain = cookie.domain.lower().lstrip(".") if cookie.domain else ""
            if not (domain == "163.com" or domain.endswith(".163.com")):
                continue
            if cookie.expires and cookie.expires < now:
                continue
            if cookie.name and cookie.value is not None:
                valid_cookies[cookie.name] = cookie.value
                if cookie.expires:
                    max_expires = max(max_expires, int(cookie.expires))
        if valid_cookies:
            return {
                "cookies": valid_cookies,
                "expires_at": max_expires or int(now + 30 * 86400),
            }
    return None


def migrate_session(data_dir: Path | str, force: bool = False) -> bool:
    """Migrate legacy NetEase session into netease/session.json.

    - Existing valid session has priority and is NOT overwritten unless force=True.
    - Session file is written with mode 0600 in directory mode 0700 atomically.
    - Legacy cookie.txt is retained for recovery.
    """
    data_path = Path(data_dir)
    netease_dir = data_path / "netease"
    session_file = netease_dir / "session.json"

    now = int(time.time())

    if session_file.is_file() and not force:
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("cookies"):
                exp = data.get("expires_at", 0)
                if exp == 0 or exp > now:
                    # Valid session already present; preserve it
                    return True
        except Exception:
            pass

    imported = import_legacy_cookie(data_dir)
    if not imported:
        return False

    cookies_dict = imported["cookies"]
    expires_at = imported["expires_at"]
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())

    session_data = {
        "version": 1,
        "generation": 1,
        "device_id": cookies_dict.get("deviceId", ""),
        "cookies": cookies_dict,
        "cookie_str": cookie_str,
        "updated_at": now,
        "expires_at": expires_at,
    }

    try:
        netease_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(netease_dir, 0o700)
    except Exception:
        pass

    fd, tmp_path = tempfile.mkstemp(dir=str(netease_dir), prefix="session.json.tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(session_file))
        logger.info("Migrated legacy NetEase cookies to %s", session_file)
        return True
    except Exception as exc:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
        logger.error("Failed to persist migrated session: %s", exc)
        return False
