"""Bounded asynchronous NetEase playback resolution with explicit quality."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import time

import httpx

logger = logging.getLogger("musicbox_service.playback")
BUDGET_S = 12.0
PRIMARY_TIMEOUT_S = 10.0
CACHE_MAX = 4096
_lock = threading.Lock()
_generation = 0
_api = None
_cache: dict = {}
_tasks: dict = {}


class CapabilityError(Exception):
    pass


class UpstreamError(Exception):
    pass


class ResolutionTimeout(Exception):
    pass


def reset() -> None:
    global _generation, _api
    with _lock:
        _generation += 1
        if _api is not None:
            _api.session.close()
        _api = None
        _cache.clear()


def _snapshot(generation: int):
    global _api
    from runner import ensure_xdg_dirs

    ensure_xdg_dirs()
    try:
        from NEMbox.api import NetEase
    except ImportError as exc:
        raise CapabilityError("NEMbox playback protocol unavailable") from exc
    with _lock:
        if generation != _generation:
            raise UpstreamError("login state changed")
        if _api is None:
            _api = NetEase()
        return (
            dict(_api._eapi_header_cookie()), dict(_api.header),
            [copy.copy(cookie) for cookie in _api.cookie_jar],
        )


def _merge_cookies(generation: int, cookies: httpx.Cookies) -> None:
    with _lock:
        if generation != _generation or _api is None:
            return
        try:
            _api.cookie_jar.load()
        except OSError:
            logger.warning("playback cookie reload failed")
        changed = False
        for cookie in cookies.jar:
            if cookie.domain.lstrip(".") not in ("music.163.com", "interface.music.163.com", "163.com"):
                continue
            _api.cookie_jar.set_cookie(copy.copy(cookie))
            changed = True
        if changed:
            try:
                _api.cookie_jar.save()
            except OSError:
                logger.warning("playback cookie persistence failed")


def _rows(payload) -> list:
    if not isinstance(payload, dict) or payload.get("code", 200) != 200:
        raise UpstreamError("NetEase rejected playback request")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise UpstreamError("invalid playback response")
    return rows


_STANDARD_LEVELS = {
    "standard": "standard", "higher": "higher", "exhigh": "exhigh",
    "lossless": "lossless", "hires": "hires", "jymaster": "jymaster", "sky": "sky",
}
_STANDARD_ENCODE_TYPES = {
    "standard": "mp3", "higher": "mp3", "exhigh": "mp3",
    "lossless": "flac", "hires": "flac", "jymaster": "flac", "sky": "flac",
}


def _safe_level_and_encode_type(quality: str) -> tuple[str, str]:
    """验证真实 NEMbox helper 能力；未知档位（特别是 sky）绝不能被静默改低。"""
    clean_q = str(quality or "").strip().lower()
    level = None
    try:
        from NEMbox.api import music_quality_to_level
        cand_level = music_quality_to_level(clean_q)
        # 若 NEMbox helper 不认识 sky 并将其静默改低，则纠正为 sky
        if clean_q == "sky" and cand_level != "sky":
            level = "sky"
        else:
            level = cand_level or _STANDARD_LEVELS.get(clean_q, clean_q)
    except Exception:
        level = _STANDARD_LEVELS.get(clean_q, clean_q)

    try:
        from NEMbox.api import level_to_encode_type
        cand_encode = level_to_encode_type(level)
        encode_type = cand_encode or _STANDARD_ENCODE_TYPES.get(level, "mp3")
    except Exception:
        encode_type = _STANDARD_ENCODE_TYPES.get(level, "mp3")

    return str(level), str(encode_type)


async def _request(song_id: int, quality: str, generation: int) -> dict:
    try:
        from NEMbox.api import (
            BASE_URL, EAPI_BASE_URL, eapi_encrypt, eapi_response_decrypt,
            encrypted_request,
        )
    except ImportError as exc:
        raise CapabilityError("NEMbox playback protocol unavailable") from exc
    header_cookie, headers, cookies = await asyncio.to_thread(_snapshot, generation)
    level, encode_type = _safe_level_and_encode_type(quality)
    api_path = "/api/song/enhance/player/url/v1"
    payload = {
        "ids": json.dumps([song_id], separators=(",", ":")),
        "level": level, "encodeType": encode_type,
        "e_r": True, "header": header_cookie,
    }
    eapi_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "NeteaseMusic 9.0.90/5038 (iPhone; iOS 16.2; zh_CN)",
        "Cookie": "; ".join(f"{key}={value}" for key, value in header_cookie.items() if value is not None),
        "X-Real-IP": headers.get("X-Real-IP", ""),
        "X-Forwarded-For": headers.get("X-Forwarded-For", ""),
    }
    async with httpx.AsyncClient(timeout=PRIMARY_TIMEOUT_S, trust_env=False) as client:
        for cookie in cookies:
            client.cookies.jar.set_cookie(cookie)
        response = await asyncio.wait_for(client.post(
            f"{EAPI_BASE_URL}/eapi/song/enhance/player/url/v1",
            data=eapi_encrypt(api_path, payload), headers=eapi_headers,
        ), PRIMARY_TIMEOUT_S)
        response.raise_for_status()
        response_cookies = httpx.Cookies(response.cookies)
        try:
            data = response.json()
        except ValueError:
            data = eapi_response_decrypt(response.content.hex().upper())
        rows = _rows(data)
        # 尊重账号权益与源能力限制：sky 与 jymaster 没有对应 weapi 码率，绝不走旧 weapi 伪造高档
        if not rows and level not in ("sky", "jymaster"):
            bitrate_map = {
                "standard": 128000, "higher": 192000, "exhigh": 320000,
                "lossless": 999000, "hires": 999000,
            }
            bitrate = bitrate_map.get(level)
            if bitrate:
                csrf = header_cookie.get("__csrf", "")
                fallback_headers = {key: value for key, value in headers.items() if key.lower() != "host"}
                response = await client.post(
                    f"{BASE_URL}/weapi/song/enhance/player/url",
                    data=encrypted_request({"ids": [song_id], "br": bitrate, "csrf_token": csrf}),
                    headers=fallback_headers,
                )
                response.raise_for_status()
                response_cookies.update(response.cookies)
                rows = _rows(response.json())
        # 只合并响应更新；请求快照里的旧 cookie 不得覆盖 CLI 刚写入的登录态。
        if response_cookies:
            await asyncio.to_thread(_merge_cookies, generation, response_cookies)
    if not rows:
        return {"id": song_id, "code": 404, "url": None}
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise UpstreamError("invalid single-song playback response")
    item = rows[0]
    if item.get("id") is not None and str(item["id"]) != str(song_id):
        raise UpstreamError("mismatched playback song")
    if item.get("code") not in (200, 404):
        raise UpstreamError("NetEase returned a playback error")
    if item.get("code") == 200 and item.get("url") and not str(item["url"]).startswith(("http://", "https://")):
        raise UpstreamError("invalid playback URL")
    return item


async def _cli(song_id: int, quality: str) -> dict:
    from runner import get_clean_env
    try:
        process = await asyncio.create_subprocess_exec(
            "musicbox", "song", "url", str(song_id), "--quality", quality, "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=get_clean_env(),
        )
    except OSError as exc:
        raise UpstreamError("musicbox playback command unavailable") from exc
    try:
        stdout, _stderr = await process.communicate()
        if process.returncode:
            raise UpstreamError("musicbox playback command failed")
        payload = json.loads(stdout)
        if not isinstance(payload, dict) or payload.get("ok") is False or not isinstance(payload.get("data"), dict):
            raise UpstreamError("invalid musicbox playback response")
        return payload["data"]
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _resolve(song_id: int, quality: str, generation: int,
                   purpose: str, context_generation: int) -> dict:
    started = time.monotonic()
    try:
        async with asyncio.timeout(BUDGET_S):
            try:
                item = await _request(song_id, quality, generation)
            except CapabilityError:
                item = await _cli(song_id, quality)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ResolutionTimeout("NetEase playback resolution timed out") from exc
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise UpstreamError(f"NetEase playback request failed ({type(exc).__name__})") from exc
    with _lock:
        if generation != _generation:
            raise UpstreamError("login state changed")
        if item.get("code") not in (200, 404):
            raise UpstreamError("invalid playback status")
        if item.get("url") and not str(item["url"]).startswith(("http://", "https://")):
            raise UpstreamError("invalid playback URL")
        try:
            expi = float(item.get("expi") or 0)
        except (ValueError, TypeError):
            expi = 0
        ttl = min(max(expi - 60, 1), 600) if expi > 0 else 300
        if not item.get("url"):
            ttl = 60
        now = time.monotonic()
        for key in [key for key, value in _cache.items() if value[1] <= now]:
            del _cache[key]
        if len(_cache) >= CACHE_MAX:
            del _cache[min(_cache, key=lambda key: _cache[key][1])]
        _cache[(generation, song_id, quality, purpose, context_generation)] = (item, now + ttl)
    logger.info("playback resolved song=%s quality=%s purpose=%s available=%s ms=%d",
                song_id, quality, purpose, bool(item.get("url")), int((time.monotonic() - started) * 1000))
    return item


def _finish(task, key) -> None:
    if _tasks.get(key) is task:
        del _tasks[key]
    if not task.cancelled():
        task.exception()


async def get_song_url_async(song_id: int, quality: str, fresh: bool = False,
                             purpose: str = "playback", context_generation: int = 0) -> dict:
    purpose = "download" if str(purpose).strip().lower() == "download" else "playback"
    context_generation = max(0, int(context_generation or 0))
    with _lock:
        generation = _generation
        cache_key = (generation, song_id, quality, purpose, context_generation)
        cached = _cache.get(cache_key)
    key = (asyncio.get_running_loop(), *cache_key)
    task = _tasks.get(key)
    if task is None or task.done():
        if not fresh and cached and cached[1] > time.monotonic():
            return {**cached[0], "reuse_for_s": max(0.0, cached[1] - time.monotonic()),
                    "expiry_source": "policy"}
        task = asyncio.create_task(
            _resolve(song_id, quality, generation, purpose, context_generation))
        _tasks[key] = task
        task.add_done_callback(lambda done: _finish(done, key))
    result = await asyncio.shield(task)
    with _lock:
        selected = _cache.get(cache_key)
        remaining = max(0.0, selected[1] - time.monotonic()) if selected else 0.0
    return {**result, "reuse_for_s": remaining, "expiry_source": "policy"}


async def shutdown() -> None:
    tasks = [task for task in _tasks.values() if task.get_loop() is asyncio.get_running_loop()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
