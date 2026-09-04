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


def get_clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in PROXY_VARS}


def run_musicbox(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
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
