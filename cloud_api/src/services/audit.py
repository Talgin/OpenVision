"""Security event log (SECURITY_POLICY: login, roles, cameras, support access).

`meta` must never hold tokens, passwords, camera URLs or personal data.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AuditLog

logger = logging.getLogger("audit")


def record(
    session: AsyncSession,
    action: str,
    *,
    tenant_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None = None,
    target: str | None = None,
    ip: str | None = None,
    meta: dict | None = None,
) -> None:
    """Adds to the session; the caller's commit persists it with the change itself."""
    session.add(
        AuditLog(tenant_id=tenant_id, actor_user_id=actor_user_id, action=action, target=target, ip=ip, meta=meta)
    )
    logger.info("audit action=%s tenant=%s actor=%s target=%s", action, tenant_id, actor_user_id, target)
