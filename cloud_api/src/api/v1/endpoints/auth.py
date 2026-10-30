from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import Principal, client_ip, current
from src.core.config import get_settings
from src.core.errors import AppError
from src.core.security import burn_password_check, hash_password, utcnow, verify_password
from src.db.session import get_session
from src.models import Camera, EdgeAgent, Plan, Role, Tenant, User
from src.schemas import ChangePasswordIn, DeleteAccountIn, LoginIn, MeOut, RefreshIn, RegisterIn, TenantOut, TokenPair, UserOut
from src.services import audit, ratelimit, tokens

router = APIRouter()


async def _email_taken(session: AsyncSession, email: str) -> bool:
    q = select(func.count()).select_from(User).where(func.lower(User.email) == email.lower())
    return (await session.execute(q)).scalar_one() > 0


@router.post("/register", response_model=TokenPair, status_code=201)
async def register(body: RegisterIn, request: Request, session: AsyncSession = Depends(get_session)):
    """Self-service sign-up: creates the company account and its owner."""
    if await _email_taken(session, body.email):
        raise AppError(409, "email_taken", "an account with this email already exists")
    s = get_settings()
    tenant = Tenant(
        name=body.company_name,
        plan=Plan(s.default_plan),
        max_cameras=s.default_max_cameras,
        retention_days=s.default_retention_days,
    )
    session.add(tenant)
    await session.flush()
    user = User(
        tenant_id=tenant.id,
        email=body.email.lower(),
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role=Role.OWNER,
        last_login_at=utcnow(),
    )
    session.add(user)
    await session.flush()
    pair = await tokens.issue(session, user, device_name=body.device_name)
    audit.record(session, "tenant.registered", tenant_id=tenant.id, actor_user_id=user.id, ip=client_ip(request))
    await session.commit()
    return pair


@router.post("/login", response_model=TokenPair)
async def login(body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)):
    ip = client_ip(request)
    await ratelimit.check_login(body.email, ip)
    user = (
        await session.execute(select(User).where(func.lower(User.email) == body.email.lower()))
    ).scalar_one_or_none()
    if user is None:
        burn_password_check(body.password)
    if user is None or not verify_password(body.password, user.password_hash) or not user.is_active:
        if user is not None:
            audit.record(session, "auth.login_failed", tenant_id=user.tenant_id, actor_user_id=user.id, ip=ip)
            await session.commit()
        # one message for every cause, so the endpoint does not confirm which emails exist
        raise AppError(401, "invalid_credentials", "wrong email or password")
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise AppError(403, "account_suspended", "this account is suspended")
    user.last_login_at = utcnow()
    pair = await tokens.issue(session, user, device_name=body.device_name)
    audit.record(session, "auth.login", tenant_id=user.tenant_id, actor_user_id=user.id, ip=ip)
    await session.commit()
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshIn, request: Request, session: AsyncSession = Depends(get_session)):
    """Single use: the old refresh token stops working once this returns."""
    return await tokens.rotate(session, body.refresh_token, client_ip(request))


@router.post("/logout", status_code=204)
async def logout(body: RefreshIn, request: Request, session: AsyncSession = Depends(get_session)):
    row = await tokens.revoke(session, body.refresh_token)
    if row is not None:
        user = await session.get(User, row.user_id)
        if user:
            audit.record(session, "auth.logout", tenant_id=user.tenant_id, actor_user_id=user.id, ip=client_ip(request))
    await session.commit()
    return Response(status_code=204)


@router.get("/me", response_model=MeOut)
async def me(p: Principal = Depends(current)):
    return MeOut(
        user=UserOut.model_validate(p.user),
        tenant=TenantOut.model_validate(p.tenant),
        permissions=sorted(p.permissions),
    )


@router.put("/me/password", status_code=204)
async def change_password(body: ChangePasswordIn, request: Request, p: Principal = Depends(current)):
    if not verify_password(body.current_password, p.user.password_hash):
        raise AppError(400, "wrong_password", "current password is wrong")
    p.user.password_hash = hash_password(body.new_password)
    await tokens.revoke_all_for_user(p.scope.session, p.user.id)
    audit.record(
        p.scope.session, "auth.password_changed", tenant_id=p.tenant.id, actor_user_id=p.user.id, ip=client_ip(request)
    )
    await p.scope.commit()
    return Response(status_code=204)


@router.post("/delete-account", status_code=204)
async def delete_account(body: DeleteAccountIn, request: Request, p: Principal = Depends(current)):
    """In-app account deletion (App Store / Google Play requirement).

    A team member deletes only their own user. The owner deletes the whole
    company: sign-in stops and every email is freed immediately; cameras stop
    and cameras, events and the company are purged by the scheduler shortly
    after. Security-log rows stay (they hold no personal data).
    """
    await ratelimit.check_login(p.user.email, client_ip(request))
    if not verify_password(body.password, p.user.password_hash):
        raise AppError(400, "wrong_password", "password is wrong")
    session = p.scope.session
    ip = client_ip(request)
    if p.user.role != Role.OWNER:
        audit.record(session, "user.self_deleted", tenant_id=p.tenant.id, actor_user_id=p.user.id, ip=ip)
        await session.delete(p.user)
        await session.commit()
        return Response(status_code=204)

    tid = p.tenant.id
    p.tenant.is_active = False
    p.tenant.deleted_at = utcnow()
    await session.execute(
        update(Camera).where(Camera.tenant_id == tid).values(deleted=True, enabled=False, push_key_digest=None)
    )
    await session.execute(update(Camera).where(Camera.tenant_id == tid).values(edge_agent_id=None))
    await session.execute(delete(EdgeAgent).where(EdgeAgent.tenant_id == tid))
    for user in (await session.execute(select(User).where(User.tenant_id == tid))).scalars():
        await tokens.revoke_all_for_user(session, user.id)
        await session.delete(user)
    audit.record(session, "tenant.deleted", tenant_id=tid, actor_user_id=p.user.id, ip=ip)
    await session.commit()
    return Response(status_code=204)
