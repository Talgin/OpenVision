"""Tenant isolation lives here, not in each endpoint.

API code never gets a bare session for tenant data; it gets a TenantScope bound
to the caller's tenant, and every query the scope builds starts from
`tenant_id == scope.tenant_id`. Forgetting the filter is not possible from an
endpoint, because the endpoint cannot build the query.

Background workers (scheduler, bridge) work across tenants and use the session
directly; that code is in src/workers and src/services only.
"""

import uuid
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import not_found

T = TypeVar("T")


@dataclass
class TenantScope:
    session: AsyncSession
    tenant_id: uuid.UUID

    def select(self, model: type[T]) -> Select:
        return select(model).where(model.tenant_id == self.tenant_id)

    async def get(self, model: type[T], obj_id, what: str = "resource") -> T:
        """Another tenant's id is indistinguishable from a missing one: 404."""
        row = (await self.session.execute(self.select(model).where(model.id == obj_id))).scalar_one_or_none()
        if row is None:
            raise not_found(what)
        return row

    async def list(self, query: Select) -> list:
        return list((await self.session.execute(query)).scalars().all())

    async def count(self, model: type[T], *where) -> int:
        q = select(func.count()).select_from(model).where(model.tenant_id == self.tenant_id, *where)
        return (await self.session.execute(q)).scalar_one()

    def add(self, obj: T) -> T:
        obj.tenant_id = self.tenant_id
        self.session.add(obj)
        return obj

    async def delete_where(self, model: type[T], *where) -> None:
        await self.session.execute(delete(model).where(model.tenant_id == self.tenant_id, *where))

    async def commit(self) -> None:
        await self.session.commit()
