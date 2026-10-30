"""One shared async Redis client; tests swap it for fakeredis via set_client()."""

import redis.asyncio as aioredis

from src.core.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url)
    return _client


def set_client(client: aioredis.Redis | None) -> None:
    global _client
    _client = client
