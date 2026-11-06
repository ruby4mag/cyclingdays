"""Web login: password hashing, signed session cookies and login throttling.

* **Passwords** are hashed with scrypt (standard library, no new dependency).
  The parameters are stored in each hash, so they can be raised later without
  invalidating existing hashes.
* **Sessions** are stateless, HMAC-SHA256-signed cookies holding the user id,
  an expiry and a fingerprint of the user's current password hash. Changing a
  password (or disabling the user) therefore ends every session of that user
  at their next request. The signing key is ``AUTH_SECRET_KEY``.
* **Throttling** is in memory, per username and globally: enough to stop
  online guessing against a single-process app. It resets on restart.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

SESSION_COOKIE = "cycledays_session"
USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{1,64}$")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024  # scrypt of huge inputs is a cheap DoS lever

# scrypt: N=2^15, r=8, p=1 uses 32 MiB and ~0.1 s per hash.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 128 * 1024 * 1024


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_username(username: str) -> str:
    name = normalize_username(username)
    if not USERNAME_PATTERN.fullmatch(name):
        raise ValueError("username: 1-64 characters, lowercase letters, digits, '.', '_' or '-'")
    return name


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    return password


def hash_password(password: str) -> str:
    """``scrypt$N$r$p$<salt>$<hash>`` (salt and hash base64url)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. Malformed hashes and oversized input never match."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _b64d(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64d(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Verified against when the username doesn't exist, so timing doesn't tell."""
    return hash_password(secrets.token_urlsafe(16))


# --------------------------------------------------------------------------
# Session tokens
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionClaims:
    user_id: int
    expires_at: int
    fingerprint: str


class SessionSigner:
    """Signs ``<user_id>.<expires>.<fingerprint>`` with HMAC-SHA256."""

    _CONTEXT = b"cycledays-session-v1|"

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("AUTH_SECRET_KEY must be at least 32 characters")
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def fingerprint(self, password_hash: str) -> str:
        mac = hmac.new(self._key, b"pw|" + password_hash.encode("utf-8"), hashlib.sha256)
        return mac.hexdigest()[:24]

    def _sign(self, payload: str) -> str:
        mac = hmac.new(self._key, self._CONTEXT + payload.encode("ascii"), hashlib.sha256)
        return _b64e(mac.digest())

    def issue(
        self, user_id: int, password_hash: str, ttl_seconds: int, now: float | None = None
    ) -> str:
        expires = int((time.time() if now is None else now) + ttl_seconds)
        payload = f"{user_id}.{expires}.{self.fingerprint(password_hash)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str, now: float | None = None) -> SessionClaims | None:
        """The claims if the signature is valid and unexpired; otherwise None."""
        if not token or len(token) > 256:
            return None
        parts = token.split(".")
        if len(parts) != 4:
            return None
        user_id, expires, fingerprint, signature = parts
        payload = f"{user_id}.{expires}.{fingerprint}"
        try:
            payload.encode("ascii")
        except UnicodeEncodeError:
            return None
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        if not (user_id.isdigit() and expires.isdigit()):
            return None
        if int(expires) <= (time.time() if now is None else now):
            return None
        return SessionClaims(int(user_id), int(expires), fingerprint)

    def matches(self, claims: SessionClaims, password_hash: str) -> bool:
        return hmac.compare_digest(claims.fingerprint, self.fingerprint(password_hash))


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRecord:
    id: int
    username: str
    password_hash: str
    is_active: bool


class UserStore(Protocol):
    async def get_by_username(self, username: str) -> UserRecord | None: ...

    async def get_by_id(self, user_id: int) -> UserRecord | None: ...

    async def record_login(self, user_id: int) -> None: ...


# --------------------------------------------------------------------------
# Throttling
# --------------------------------------------------------------------------


class LoginThrottle:
    """Sliding-window limit on failed logins, per username and in total.

    Per username: after ``per_user`` failures in ``window`` seconds that
    username is refused until the oldest failure leaves the window. Globally:
    ``total`` failures across all usernames, which caps guessing spread over
    many names. Client IPs aren't used: behind the reverse proxy every
    request may come from the same address.
    """

    def __init__(self, per_user: int = 5, total: int = 50, window: float = 900.0) -> None:
        self.per_user = per_user
        self.total = total
        self.window = window
        self._by_user: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def blocked(self, username: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._prune(self._all, now)
        q = self._by_user.get(username)
        if q is not None:
            self._prune(q, now)
            if not q:
                del self._by_user[username]
                q = None
        return len(self._all) >= self.total or (q is not None and len(q) >= self.per_user)

    def failure(self, username: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._all.append(now)
        self._by_user.setdefault(username, deque()).append(now)
        if len(self._by_user) > 10_000:  # bound memory under a username spray
            self._by_user.clear()

    def success(self, username: str) -> None:
        self._by_user.pop(username, None)
