"""Password hashing and access tokens.

Passwords are hashed with Argon2id via ``pwdlib``. Tokens are JWTs signed with
the configured ``OPSPILOT_SECRET_KEY``. Neither secret is ever logged; the only
thing that leaves this module toward a client is the token string itself, and
only from the login endpoint.

The requirements are explicit that backend permission checks are the security
boundary — hiding a button in the UI is not a control. Token verification is
therefore paired with a database-backed permission lookup in
``backend.app.auth.deps``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from typing import Any

import jwt
from pwdlib import PasswordHash

from backend.app.core.errors import Unauthenticated
from backend.app.core.settings import Settings, get_settings

# Argon2id — memory-hard, current best practice for password storage.
_password_hash = PasswordHash.recommended()

ALGORITHM = "HS256"
_TOKEN_TYPE = "access"  # noqa: S105 - JWT typ claim, not a secret


class TokenPayload:
    """Parsed contents of an access token."""

    __slots__ = ("expires_at", "user_id", "username")

    def __init__(self, user_id: int, username: str, expires_at: dt.datetime) -> None:
        self.user_id = user_id
        self.username = username
        self.expires_at = expires_at


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Return a salted Argon2id hash of *password*."""
    _validate_password_strength(password)
    return _password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time-ish verification of *password* against *encoded*.

    Returns False rather than raising on a malformed hash so a corrupted row
    cannot be used to distinguish accounts.
    """
    try:
        return _password_hash.verify(password, encoded)
    except Exception:
        return False


def verify_password_with_upgrade(password: str, encoded: str) -> tuple[bool, str | None]:
    """Verify *password*, returning an upgraded hash when parameters are stale.

    Returns ``(ok, upgraded_hash_or_None)``. Callers persist the upgraded hash
    on a successful login so hash parameters can be strengthened over time
    without invalidating existing accounts.
    """
    try:
        # pwdlib names this verify_and_update: it returns (ok, new_hash_or_None).
        ok, upgraded = _password_hash.verify_and_update(password, encoded)
    except Exception:
        return False, None
    return bool(ok), (upgraded if ok else None)


MIN_PASSWORD_LENGTH = 12


def _validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.lower() in {"password", "changeme", "opspilot", "admin123456"}:
        raise ValueError("password is too common")


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
def create_access_token(
    user_id: int,
    username: str,
    *,
    settings: Settings | None = None,
    now: dt.datetime | None = None,
) -> str:
    """Mint a signed access token for *user_id*."""
    cfg = settings or get_settings()
    secret = cfg.secret_key.get_secret_value()
    if not secret:
        raise RuntimeError("OPSPILOT_SECRET_KEY is not configured")

    issued = now or dt.datetime.now(dt.UTC)
    expires = issued + dt.timedelta(seconds=cfg.access_token_ttl)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "typ": _TOKEN_TYPE,
        "iat": issued,
        "exp": expires,
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, *, settings: Settings | None = None) -> TokenPayload:
    """Verify *token* and return its payload, or raise :class:`Unauthenticated`."""
    cfg = settings or get_settings()
    secret = cfg.secret_key.get_secret_value()
    if not secret:
        raise Unauthenticated("authentication is not configured")

    try:
        raw = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthenticated("access token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthenticated("access token is invalid") from exc

    if raw.get("typ") != _TOKEN_TYPE:
        raise Unauthenticated("unexpected token type")

    try:
        user_id = int(raw["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Unauthenticated("access token is missing a subject") from exc

    expires_at = raw.get("exp")
    if isinstance(expires_at, (int, float)):
        expiry = dt.datetime.fromtimestamp(expires_at, tz=dt.UTC)
    else:  # pragma: no cover - jwt always emits exp
        expiry = dt.datetime.now(dt.UTC)

    return TokenPayload(
        user_id=user_id,
        username=str(raw.get("username", "")),
        expires_at=expiry,
    )


# --------------------------------------------------------------------------
# Login rate limiting helpers (pure — the caller owns storage)
# --------------------------------------------------------------------------
def login_limiter_key(username: str, client_ip: str) -> str:
    """Stable key for a login-rate-limit bucket.

    The username is hashed so the limiter state never stores a credential hint
    in plaintext next to an IP address.
    """
    digest = hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()[:16]
    return f"login:{digest}:{client_ip}"


def constant_time_equals(a: str, b: str) -> bool:
    """Compare two strings without early exit on the first differing byte."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
