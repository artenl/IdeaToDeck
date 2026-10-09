"""Password hashing (stdlib scrypt) and a small in-memory login rate limiter."""

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

_N, _R, _P = 2**14, 8, 1
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,}$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 254


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# Hash of a random password, used to spend the same time on unknown emails.
DUMMY_HASH = hash_password(secrets.token_hex(16))


class LoginLimiter:
    """Allows `max_failures` failed logins per key within `window_s` seconds."""

    def __init__(self, max_failures: int, window_s: float):
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_failures

    def fail(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now).append(now)
        if len(self._failures) > 10_000:  # keep memory bounded under abuse
            for k in [k for k, q in self._failures.items() if not q][:5_000]:
                del self._failures[k]

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
