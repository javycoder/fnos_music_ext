"""WebUI 管理密码：scrypt 哈希与 HMAC 会话。

明文密码只在内存中出现。``.env`` 里保存 ``scrypt$<盐>$<哈希>`` 和单独的会话密钥。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
import time

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MIN_PASSWORD_LEN = 8
SESSION_TTL_S = 7 * 24 * 3600
COOKIE_NAME = "fnmusic_session"
LOCK_AFTER = 8
LOCK_S = 15 * 60


def password_ok(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    return None


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = (stored or "").split("$", 2)
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def new_session_secret() -> str:
    return secrets.token_hex(32)


def issue_token(secret: str, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + SESSION_TTL_S)
    payload = str(exp)
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def token_valid(token: str, secret: str, now: float | None = None) -> bool:
    if not secret or not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        exp = int(payload)
    except ValueError:
        return False
    current = time.time() if now is None else now
    return exp > current


def main() -> int:
    """从 FNMUSIC_WEBUI_PASSWORD 计算哈希与新会话密钥，各占一行。不回显密码。"""
    password = os.environ.get("FNMUSIC_WEBUI_PASSWORD", "")
    err = password_ok(password)
    if err:
        print(err, file=sys.stderr)
        return 2
    print(hash_password(password))
    print(new_session_secret())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
