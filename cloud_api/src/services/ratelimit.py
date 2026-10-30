import hashlib

from src.core.config import get_settings
from src.core.errors import AppError
from src.services.redis import get_redis


async def check_login(email: str, ip: str | None) -> None:
    """Fixed window per (email, ip). Keyed on a hash so emails stay out of Redis."""
    s = get_settings()
    key = "rl:login:" + hashlib.sha256(f"{email.lower()}|{ip}".encode()).hexdigest()[:32]
    r = get_redis()
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, s.login_window_seconds)
    if n > s.login_max_attempts:
        raise AppError(
            429, "rate_limited", "too many login attempts, try again later", {"retry_after": s.login_window_seconds}
        )
