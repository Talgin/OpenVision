"""Short-lived URLs for crops and frames.

Objects are addressed as "<bucket>/<object>", as trt-processor writes them.
The client gets an opaque URL that expires (media_expires_at); it must not
cache the URL itself, only the bytes.
"""

import uuid
from datetime import timedelta
from functools import lru_cache

from src.core.config import get_settings
from src.core.security import encode_media_token, utcnow


@lru_cache
def _minio():
    from minio import Minio

    s = get_settings()
    return Minio(s.minio_endpoint, access_key=s.minio_access_key, secret_key=s.minio_secret_key, secure=s.minio_secure)


def sign(tenant_id: uuid.UUID, key: str | None) -> str | None:
    if not key:
        return None
    s = get_settings()
    if s.media_backend == "minio":
        bucket, _, obj = key.partition("/")
        return _minio().presigned_get_object(bucket, obj, expires=timedelta(seconds=s.media_url_ttl_seconds))
    token = encode_media_token(tenant_id, key, s.media_url_ttl_seconds)
    return f"{s.public_base_url}/api/v1/media/{token}"


def expires_at():
    return utcnow() + timedelta(seconds=get_settings().media_url_ttl_seconds)
