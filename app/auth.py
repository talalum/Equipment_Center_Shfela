"""
כניסה עם סיסמה אחת.

הסיסמה לעולם לא נשמרת בקוד ולא במסד — רק hash מסוג PBKDF2 במשתנה סביבה.
ליצירת ה-hash:  python -m app.hashpw
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field

from app import config

_ITERATIONS = 240_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = encoded.split("$")
        if algo != _ALGO:
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected.hex(), digest_hex)


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


@dataclass
class LoginThrottle:
    """הגבלת ניסיונות כניסה, לפי כתובת. מספיק בזיכרון — משתמשת אחת, מופע אחד."""

    attempts: dict[str, _Attempts] = field(default_factory=dict)

    def seconds_remaining(self, key: str) -> int:
        entry = self.attempts.get(key)
        if not entry:
            return 0
        return max(0, int(entry.locked_until - time.monotonic()))

    def record_failure(self, key: str) -> None:
        entry = self.attempts.setdefault(key, _Attempts())
        entry.count += 1
        if entry.count >= config.LOGIN_MAX_ATTEMPTS:
            entry.locked_until = time.monotonic() + config.LOGIN_LOCKOUT_SECONDS
            entry.count = 0

    def reset(self, key: str) -> None:
        self.attempts.pop(key, None)


throttle = LoginThrottle()


def auth_configured() -> bool:
    return bool(config.APP_PASSWORD_HASH)


def check_password(password: str) -> bool:
    if not auth_configured():
        return False
    return verify_password(password, config.APP_PASSWORD_HASH)


def new_session_secret() -> str:
    return secrets.token_urlsafe(48)
