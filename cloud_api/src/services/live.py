"""Live fan-out: one Redis pub/sub channel per tenant.

The bridge and the scheduler publish; each WebSocket subscribes only to its
own tenant's channel, so isolation does not depend on filtering messages.
"""

import json
import uuid

from src.services.redis import get_redis


def channel(tenant_id: uuid.UUID) -> str:
    return f"live:{tenant_id}"


async def publish(tenant_id: uuid.UUID, msg_type: str, data: dict) -> None:
    await get_redis().publish(channel(tenant_id), json.dumps({"type": msg_type, "data": data}, default=str))
