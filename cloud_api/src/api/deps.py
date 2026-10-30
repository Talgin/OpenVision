import hmac
import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.errors import AppError, forbidden
from src.core.security import ACCESS, decode_token
from src.db.session import get_session
from src.models import Role, Tenant, User
from src.repositories import TenantScope

# What each role may do. The mobile app hides UI from `permissions` in GET /me;
# the server enforces the same table.
PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.VIEWER: frozenset({"cameras.read", "events.read", "persons.read"}),
    Role.OPERATOR: frozenset({"cameras.read", "cameras.control", "events.read", "persons.read"}),
    Role.ADMIN: frozenset(
        {
            "cameras.read", "cameras.control", "cameras.write", "events.read", "persons.read", "persons.write",
            "users.read", "users.write", "audit.read",
        }
    ),  # fmt: skip
}
PERMISSIONS[Role.OWNER] = PERMISSIONS[Role.ADMIN] | {"account.write"}

_bearer = HTTPBearer(auto_error=False)


def unauthorized(message: str = "not authenticated") -> AppError:
    return AppError(401, "unauthorized", message)


@dataclass
class Principal:
    user: User
    tenant: Tenant
    scope: TenantScope

    @property
    def permissions(self) -> frozenset[str]:
        return PERMISSIONS[self.user.role]

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise forbidden()


async def principal_from_token(token: str, session: AsyncSession) -> Principal:
    try:
        payload = decode_token(token, ACCESS)
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized("token is invalid or expired")
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized("token is invalid or expired")
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise AppError(403, "account_suspended", "this account is suspended")
    # The role in the token is informational; the stored role wins, so a
    # demotion takes effect on the next request, not after token expiry.
    return Principal(user=user, tenant=tenant, scope=TenantScope(session, tenant.id))


async def current(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    if creds is None:
        raise unauthorized()
    return await principal_from_token(creds.credentials, session)


def require(permission: str):
    async def _dep(p: Principal = Depends(current)) -> Principal:
        p.require(permission)
        return p

    return _dep


async def platform_staff(x_platform_token: str = Header(default="")) -> None:
    expected = get_settings().platform_token
    if not expected or not hmac.compare_digest(x_platform_token, expected):
        raise unauthorized("platform token required")


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
