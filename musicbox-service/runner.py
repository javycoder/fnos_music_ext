"""Execute darknessomi/musicbox CLI with proxy env stripped (网易云需直连)."""
from __future__ import annotations

import os
import subprocess

PROXY_VARS = {
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
}


class MusicboxTimeoutError(Exception):
    pass


def ensure_xdg_dirs() -> None:
    """Ensure XDG related directories and netease-musicbox subdirectories exist."""
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    runtime_home = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"

    dir_bases = [
        cache_home,
        config_home,
        data_home,
        runtime_home,
        os.path.expanduser("~/.netease-musicbox"),
    ]

    for base in dir_bases:
        if not base:
            continue
        try:
            os.makedirs(base, exist_ok=True)
            if not base.endswith("netease-musicbox"):
                os.makedirs(os.path.join(base, "netease-musicbox"), exist_ok=True)
        except OSError:
            pass


ensure_xdg_dirs()


def get_clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in PROXY_VARS}


def run_musicbox(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    ensure_xdg_dirs()
    env = get_clean_env()
    try:
        proc = subprocess.run(
            ["musicbox", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        raise MusicboxTimeoutError(f"musicbox timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        return 127, "", f"musicbox executable not found: {exc}"
