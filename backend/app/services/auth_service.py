from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Literal

import structlog
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.user import User

logger = structlog.get_logger()

pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=12, deprecated="auto")

TokenType = Literal["access", "refresh", "temp"]

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# Key helpers — auto-generate in-memory keys when env vars are absent
#               (convenient for development; tokens won't survive restart)
# ---------------------------------------------------------------------------

_auto_private_key: str | None = None
_auto_public_key: str | None = None


def _ensure_auto_keys() -> None:
    global _auto_private_key, _auto_public_key
    if _auto_private_key is not None:
        return
    logger.warning(
        "jwt_keys_not_configured",
        msg="JWT_PRIVATE_KEY/JWT_PUBLIC_KEY not set — generating ephemeral keys. "
        "Tokens will be invalidated on restart. "
        "Run `python scripts/generate_keys.py` and add keys to .env for persistence.",
    )
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _auto_private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    _auto_public_key = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def _private_key() -> str:
    if settings.JWT_PRIVATE_KEY:
        try:
            return base64.b64decode(settings.JWT_PRIVATE_KEY).decode()
        except Exception:
            pass  # fall through to auto-key
    _ensure_auto_keys()
    return _auto_private_key  # type: ignore[return-value]


def _public_key() -> str:
    if settings.JWT_PUBLIC_KEY:
        try:
            return base64.b64decode(settings.JWT_PUBLIC_KEY).decode()
        except Exception:
            pass  # fall through to auto-key
    _ensure_auto_keys()
    return _auto_public_key  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Token creation
# ---------------------------------------------------------------------------


def _make_token(user_id: str, token_type: TokenType, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "token_type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, _private_key(), algorithm="RS256")


def create_access_token(user_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "token_type": "access",
        "iat": now,
        "exp": now + timedelta(hours=2),
    }
    return jwt.encode(payload, _private_key(), algorithm="RS256")


def create_refresh_token(user_id: str) -> str:
    return _make_token(user_id, "refresh", timedelta(days=7))


def create_temp_token(user_id: str) -> str:
    return _make_token(user_id, "temp", timedelta(minutes=15))


def create_image_token(image_id: int, version: int, variant: str, exp: datetime) -> str:
    """A signed link token for one image version and variant ("full" or "small").

    No `sub` and no `iat`: identical inputs give an identical token (RS256 signing is
    deterministic), so a link stays the same URL for its whole expiry bucket and browsers can
    reuse the cached bytes. `get_current_user` and `authenticate_socket` accept only access and
    temp tokens, so this token can't authenticate anything else.
    """
    payload = {
        "token_type": "image",
        "img": image_id,
        "v": version,
        "var": variant,
        "exp": exp,
    }
    return jwt.encode(payload, _private_key(), algorithm="RS256")


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises JWTError on failure."""
    return jwt.decode(token, _public_key(), algorithms=["RS256"])


# ---------------------------------------------------------------------------
# User lookup helpers
# ---------------------------------------------------------------------------


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username.lower()))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    return await db.get(User, user_id)


async def get_or_create_user_by_netid(
    db: AsyncSession, netid: str, email: str | None
) -> User:
    """
    Look up a user by netid; create a new USER record if not found.
    Called from both the OAuth2 callback and the dev login fallback.
    """
    result = await db.execute(select(User).where(User.netid == netid))
    user = result.scalar_one_or_none()

    if user:
        user.last_login = datetime.now(timezone.utc)
        await db.flush()
        return user

    import uuid

    user = User(
        id=str(uuid.uuid4()),
        netid=netid,
        email=email,
        role="USER",
    )
    db.add(user)
    await db.flush()
    logger.info("user_created_via_oauth2", netid=netid)
    return user


async def create_guest_user(db: AsyncSession, display_name: str, email: str) -> User:
    """
    Create or reactivate a GUEST user by email.
    If the email belongs to an existing GUEST, update their display name (rejoin).
    If the email belongs to a non-GUEST account, raise ValueError.
    """
    email = email.lower()
    result = await db.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()

    if existing:
        if existing.role != "GUEST":
            raise ValueError(
                "This email is already associated with an account. Please sign in instead."
            )
        existing.display_name = display_name
        existing.last_login = datetime.now(timezone.utc)
        await db.flush()
        return existing

    import uuid

    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=display_name,
        role="GUEST",
    )
    db.add(user)
    await db.flush()
    logger.info("guest_user_created", email=email)
    return user


async def authenticate_local(
    db: AsyncSession, username: str, password: str
) -> User | None:
    """Verify username/password for Admin and local accounts."""
    user = await get_user_by_username(db, username)
    if not user or not user.password_hash:
        return None
    if not verify_password(password, user.password_hash):
        logger.warning("login_failed", username=username)
        return None
    user.last_login = datetime.now(timezone.utc)
    await db.flush()
    return user
