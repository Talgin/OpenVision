import uuid

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select

from src.api.deps import Principal, client_ip, require
from src.core.errors import AppError
from src.core.security import hash_password
from src.models import Role, User
from src.schemas import AdminSetPasswordIn, UserCreate, UserOut, UserUpdate
from src.services import audit, tokens

router = APIRouter()


def _guard_target(p: Principal, target: User) -> None:
    if target.role == Role.OWNER:
        raise AppError(403, "owner_protected", "the account owner cannot be changed here")
    if target.id == p.user.id:
        raise AppError(403, "self_protected", "you cannot change your own role or access")


@router.get("", response_model=list[UserOut])
async def list_users(p: Principal = Depends(require("users.read"))):
    return await p.scope.list(p.scope.select(User).order_by(User.created_at))


@router.post("", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, request: Request, p: Principal = Depends(require("users.write"))):
    session = p.scope.session
    taken = (
        await session.execute(select(func.count()).select_from(User).where(func.lower(User.email) == body.email.lower()))
    ).scalar_one()
    if taken:
        raise AppError(409, "email_taken", "an account with this email already exists")
    user = p.scope.add(
        User(
            email=body.email.lower(),
            full_name=body.full_name,
            role=body.role,
            password_hash=hash_password(body.password),
        )
    )
    await session.flush()
    audit.record(
        session, "user.created", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"user:{user.id}", ip=client_ip(request), meta={"role": body.role.value},
    )  # fmt: skip
    await p.scope.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID, body: UserUpdate, request: Request, p: Principal = Depends(require("users.write"))
):
    user = await p.scope.get(User, user_id, "user")
    changes = body.model_dump(exclude_unset=True)
    if "role" in changes or "is_active" in changes:
        _guard_target(p, user)
    for field, value in changes.items():
        setattr(user, field, value)
    if changes.get("is_active") is False:
        await tokens.revoke_all_for_user(p.scope.session, user.id)
    audit.record(
        p.scope.session, "user.updated", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"user:{user.id}", ip=client_ip(request),
        meta={k: (v.value if isinstance(v, Role) else v) for k, v in changes.items() if k != "full_name"},
    )  # fmt: skip
    await p.scope.commit()
    return user


@router.post("/{user_id}/password", status_code=204)
async def set_password(
    user_id: uuid.UUID, body: AdminSetPasswordIn, request: Request, p: Principal = Depends(require("users.write"))
):
    """An admin sets a new password for a team member who forgot theirs.
    Signs them out everywhere; pass the new password on to them yourself."""
    user = await p.scope.get(User, user_id, "user")
    _guard_target(p, user)
    user.password_hash = hash_password(body.new_password)
    await tokens.revoke_all_for_user(p.scope.session, user.id)
    audit.record(
        p.scope.session, "user.password_reset", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"user:{user.id}", ip=client_ip(request),
    )  # fmt: skip
    await p.scope.commit()
    return Response(status_code=204)


@router.delete("/{user_id}", status_code=204)
async def delete_user(user_id: uuid.UUID, request: Request, p: Principal = Depends(require("users.write"))):
    user = await p.scope.get(User, user_id, "user")
    _guard_target(p, user)
    await p.scope.session.delete(user)
    audit.record(
        p.scope.session, "user.deleted", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"user:{user_id}", ip=client_ip(request),
    )  # fmt: skip
    await p.scope.commit()
    return Response(status_code=204)
