"""Explicit administrator authentication used only for music library scans."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid

import httpx


_AUTHX_SALT = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
_AUTHX_API_KEY = "6D5602D4-A342-4799-A0F0-BB795E7167D0"
_GATEWAY_URL = "http://127.0.0.1:5666"


class ScanAuthError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def authx_header(path: str, body: str = "") -> str:
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))
    body_hash = hashlib.md5((body or "").encode("utf-8")).hexdigest()
    raw = "_".join([_AUTHX_SALT, path, nonce, timestamp, body_hash, _AUTHX_API_KEY])
    sign = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return f"nonce={nonce}&timestamp={timestamp}&sign={sign}"


class ScanAdminSession:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._cache_key = ""
        self._token = ""
        self._device_id = ""

    def _credentials(self, conf: dict) -> tuple[str, str, str]:
        username = str(conf.get("scan_admin_username") or "").strip()
        password = str(conf.get("scan_admin_password") or "").strip()
        if not self._device_id:
            self._device_id = str(conf.get("scan_admin_device_id") or "").strip() or uuid.uuid4().hex
        device_id = self._device_id
        if not username or not password:
            raise ScanAuthError("not_configured")
        return username, password, device_id

    @staticmethod
    def _cache_key_for(username: str, password: str, device_id: str) -> str:
        return hashlib.sha256("|".join((username, password, device_id)).encode()).hexdigest()

    @staticmethod
    def _user(payload: dict) -> dict:
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return {}
        user = data.get("user")
        return user if isinstance(user, dict) else data

    def _client_instance(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_GATEWAY_URL,
                timeout=httpx.Timeout(10.0, connect=3.0),
                follow_redirects=False,
            )
        return self._client

    async def _login(self, conf: dict) -> str:
        username, password, device_id = self._credentials(conf)
        body = json.dumps(
            {"username": username, "password": password, "deviceId": device_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        path = "/music/api/v1/user/password-login"
        request = self._client_instance().build_request(
            "POST",
            path,
            content=body.encode("utf-8"),
            headers={"content-type": "application/json", "authx": authx_header(path, body)},
        )
        try:
            response = await self._client_instance().send(request)
            payload = response.json() if response.status_code == 200 else {}
            await response.aclose()
        except (httpx.HTTPError, ValueError):
            if 'response' in locals():
                await response.aclose()
            raise ScanAuthError("login_unavailable") from None
        if not (isinstance(payload, dict) and payload.get("code") == 0):
            raise ScanAuthError("login_rejected")
        token = str(((payload.get("data") or {}) if isinstance(payload.get("data"), dict) else {}).get("userToken") or "").strip()
        if not token:
            raise ScanAuthError("token_missing")
        return token

    async def _verify_admin(self, token: str) -> None:
        request = self._client_instance().build_request(
            "GET", "/music/api/v1/user/me", headers={"cookie": f"music-token={token}"}
        )
        try:
            response = await self._client_instance().send(request)
            payload = response.json() if response.status_code == 200 else {}
            await response.aclose()
        except (httpx.HTTPError, ValueError):
            if 'response' in locals():
                await response.aclose()
            raise ScanAuthError("verify_unavailable") from None
        if not (isinstance(payload, dict) and payload.get("code") == 0):
            raise ScanAuthError("verify_rejected")
        if str(self._user(payload).get("role") or "").lower() not in ("admin", "administrator"):
            raise ScanAuthError("not_admin")

    async def headers(self, conf: dict) -> dict[str, str]:
        username, password, device_id = self._credentials(conf)
        cache_key = self._cache_key_for(username, password, device_id)
        if self._token and self._cache_key == cache_key:
            return {"cookie": f"music-token={self._token}"}
        async with self._lock:
            if self._token and self._cache_key == cache_key:
                return {"cookie": f"music-token={self._token}"}
            token = await self._login(conf)
            await self._verify_admin(token)
            self._token = token
            self._cache_key = cache_key
            return {"cookie": f"music-token={token}"}

    async def verify(self, conf: dict) -> str:
        async with self._lock:
            self.invalidate()
            token = await self._login(conf)
            await self._verify_admin(token)
            username, password, device_id = self._credentials(conf)
            self._token = token
            self._cache_key = self._cache_key_for(username, password, device_id)
            return token

    def invalidate(self) -> None:
        self._token = ""
        self._cache_key = ""

    async def close(self) -> None:
        self.invalidate()
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None if self._owns_client else self._client
