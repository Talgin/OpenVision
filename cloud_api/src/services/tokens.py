import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.errors import AppError
from src.core.security import encode_access_token, new_opaque_token, token_digest, utcnow
from src.models import RefreshToken, User
from src.schemas import TokenPair
from src.services import audit


def as_utc(dt: datetime) -> datetime:
    """SQLite hands datetimes back naive; Postgres does not. Both are UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _invalid() -> AppError:
    return AppError(401, "invalid_refresh_token", "session expired, sign in again")


async def issue(
    session: AsyncSession, user: User, *, family_id: uuid.UUID | None = None, device_name: str | None = None
) -> TokenPair:
    raw = new_opaque_token("ovr")
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id or uuid.uuid4(),
            token_digest=token_digest(raw),
            expires_at=utcnow() + timedelta(days=get_settings().refresh_token_days),
            device_name=device_name,
        )
    )
    access, ttl = encode_access_token(user.id, user.tenant_id, user.role.value)
    return TokenPair(access_token=access, refresh_token=raw, expires_in=ttl)


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(update(RefreshToken).where(RefreshToken.family_id == family_id).values(revoked=True))


async def rotate(session: AsyncSession, raw: str, ip: str | None) -> TokenPair:
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_digest == token_digest(raw)))
    ).scalar_one_or_none()
    if row is None:
        raise _invalid()
    user = await session.get(User, row.user_id)
    if row.revoked or row.used_at is not None:
        # A used token came back: either a retry after a lost response or a
        # stolen copy. We cannot tell which, so the whole session dies.
        await revoke_family(session, row.family_id)
        if user:
            audit.record(session, "auth.refresh_reuse", tenant_id=user.tenant_id, actor_user_id=user.id, ip=ip)
        await session.commit()
        raise _invalid()
    if as_utc(row.expires_at) < utcnow() or user is None or not user.is_active:
        raise _invalid()
    row.used_at = utcnow()
    pair = await issue(session, user, family_id=row.family_id, device_name=row.device_name)
    await session.commit()
    return pair


async def revoke(session: AsyncSession, raw: str) -> RefreshToken | None:
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_digest == token_digest(raw)))
    ).scalar_one_or_none()
    if row is not None:
        await revoke_family(session, row.family_id)
    return row


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(update(RefreshToken).where(RefreshToken.user_id == user_id).values(revoked=True))
