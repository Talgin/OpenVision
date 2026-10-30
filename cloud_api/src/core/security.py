"""Passwords, JWT, field encryption, opaque tokens.

Nothing in here logs its inputs: they are passwords, tokens or camera URLs.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from src.core.config import get_settings

ACCESS = "access"
MEDIA = "media"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# Spend the same time on unknown emails as on wrong passwords
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def burn_password_check(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


# --- JWT -------------------------------------------------------------------

def encode_access_token(user_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> tuple[str, int]:
    s = get_settings()
    ttl = s.access_token_minutes * 60
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "type": ACCESS,
        "iat": utcnow(),
        "exp": utcnow() + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm), ttl


def decode_token(token: str, expected_type: str) -> dict:
    """Raises jwt.PyJWTError on any problem, including a wrong token type."""
    s = get_settings()
    payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def encode_media_token(tenant_id: uuid.UUID, object_key: str, ttl: int) -> str:
    s = get_settings()
    payload = {"tid": str(tenant_id), "key": object_key, "type": MEDIA, "exp": utcnow() + timedelta(seconds=ttl)}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


# --- opaque tokens (refresh tokens, push keys, edge agent tokens) ----------

def new_opaque_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def token_digest(token: str) -> str:
    """Stored instead of the token. SHA-256 is enough: tokens are 256-bit random."""
    return hashlib.sha256(token.encode()).hexdigest()


def digest_matches(token: str, digest: str) -> bool:
    return hmac.compare_digest(token_digest(token), digest)


# --- field encryption ------------------------------------------------------

def _fernet() -> Fernet:
    return Fernet(get_settings().secrets_key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("cannot decrypt stored secret (OV_SECRETS_KEY changed?)") from e


def redact_url(url: str) -> str:
    """rtsp://user:pass@host:554/p -> rtsp://***@host:554/p, query dropped."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    if parts.username or parts.password:
        host = f"***@{host}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))
