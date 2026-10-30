"""Client for trt_api, which owns the known-faces collection in Milvus.

Enrollment sends tenant_id with every face (trt_api branch feat/multi-tenant),
so trt-processor (MULTI_TENANT=true) matches it only on that tenant's cameras.
Person ids are ours: Person.id is the person_id in Milvus and in events.

face_api_url "demo" keeps the photo in the local media store and talks to no
one, for client-app development without a GPU.
"""

import logging
import uuid
from pathlib import Path

import httpx

from src.core.config import get_settings
from src.core.errors import AppError

logger = logging.getLogger("faces")

# trt_api stores one row per category; cloud cameras send none, so the
# processor matches on tenant alone and the category is only a label.
CATEGORY = "cloud"


def enabled() -> bool:
    return bool(get_settings().face_api_url)


def _unavailable() -> AppError:
    return AppError(503, "face_service_unavailable", "face recognition is not available right now, try again later")


async def enroll(person_id: uuid.UUID, tenant_id: uuid.UUID, name: str, photo: bytes, content_type: str) -> str:
    """Store the face; returns the photo's media key. 422 no_face_found if there is none."""
    s = get_settings()
    if s.face_api_url == "demo":
        key = f"{s.face_photos_bucket}/{person_id}/{uuid.uuid4()}.jpg"
        path = Path(s.media_local_dir) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(photo)
        return key
    form = {
        "name": name, "surname": "", "person_id": str(person_id), "category_ids": CATEGORY,
        "gender": "", "birth_date": "", "tenant_id": str(tenant_id),
    }  # fmt: skip
    try:
        async with httpx.AsyncClient(timeout=s.face_api_timeout_seconds) as c:
            r = await c.post(
                f"{s.face_api_url.rstrip('/')}/insert_face/", data=form, files={"photo": ("photo", photo, content_type)}
            )
    except httpx.HTTPError as e:
        logger.warning("trt_api insert_face failed: %s", e)
        raise _unavailable()
    if r.status_code == 400:
        raise AppError(422, "no_face_found", "no face was found in the photo, use a clear front-facing photo")
    if r.status_code != 200:
        logger.warning("trt_api insert_face HTTP %s", r.status_code)
        raise _unavailable()
    return f"{s.face_photos_bucket}/{r.json()['object_name']}"


async def remove(person_id: uuid.UUID) -> None:
    """Delete the person's face rows and photos. Nothing enrolled is not an error."""
    s = get_settings()
    if not s.face_api_url:
        logger.warning("person %s: face_api_url is not set, nothing erased in trt_api", person_id)
        return
    if s.face_api_url == "demo":
        for f in (Path(s.media_local_dir) / s.face_photos_bucket / str(person_id)).glob("*.jpg"):
            f.unlink(missing_ok=True)
        return
    try:
        async with httpx.AsyncClient(timeout=s.face_api_timeout_seconds) as c:
            r = await c.request("DELETE", f"{s.face_api_url.rstrip('/')}/delete_face/", json={"person_id": str(person_id)})
    except httpx.HTTPError as e:
        logger.warning("trt_api delete_face failed: %s", e)
        raise _unavailable()
    if r.status_code not in (200, 404):
        logger.warning("trt_api delete_face HTTP %s", r.status_code)
        raise _unavailable()


async def purge_tenant(tenant_id: uuid.UUID) -> bool:
    """Erase every face of a deleted tenant (known, unknown, history). False: retry later."""
    s = get_settings()
    if not s.face_api_url or s.face_api_url == "demo":
        return True
    try:
        async with httpx.AsyncClient(timeout=max(s.face_api_timeout_seconds, 120)) as c:
            r = await c.request("DELETE", f"{s.face_api_url.rstrip('/')}/delete_tenant/", json={"tenant_id": str(tenant_id)})
    except httpx.HTTPError as e:
        logger.warning("trt_api delete_tenant failed: %s", e)
        return False
    if r.status_code != 200:
        logger.warning("trt_api delete_tenant HTTP %s", r.status_code)
        return False
    logger.info("tenant %s faces purged: %s", tenant_id, r.json().get("deleted"))
    return True
