"""Known persons: a name and one enrolled face photo each.

Events whose person_id is a person's id carry its name (EventOut.person_name).
Only tenants on the face plan can enroll. Names and photos are personal data:
they never go into the audit log, only the person's id does.
"""

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from sqlalchemy import func

from src.api.deps import Principal, client_ip, require
from src.core.config import get_settings
from src.core.errors import AppError
from src.models import Person, Plan
from src.schemas import Page, PersonOut, PersonUpdate
from src.services import audit, faces, media

router = APIRouter()

PHOTO_TYPES = {"image/jpeg", "image/png"}


def to_out(person: Person) -> PersonOut:
    return PersonOut(
        id=person.id,
        name=person.name,
        note=person.note,
        photo_url=media.sign(person.tenant_id, person.photo_key),
        media_expires_at=media.expires_at() if person.photo_key else None,
        created_at=person.created_at,
    )


def _check_enrollment(p: Principal) -> None:
    if p.tenant.plan != Plan.FACE:
        raise AppError(403, "face_plan_required", "face recognition is not part of your plan")
    if not faces.enabled():
        raise AppError(503, "face_service_unavailable", "face recognition is not available right now, try again later")


async def _read_photo(photo: UploadFile) -> bytes:
    limit = get_settings().max_photo_bytes
    if photo.content_type not in PHOTO_TYPES:
        raise AppError(422, "invalid_photo", "the photo must be a JPEG or PNG image")
    data = await photo.read(limit + 1)
    if len(data) > limit:
        raise AppError(422, "photo_too_large", f"the photo must be at most {limit // (1024 * 1024)} MB", {"limit": limit})
    if not data:
        raise AppError(422, "invalid_photo", "the photo is empty")
    return data


@router.get("", response_model=Page[PersonOut])
async def list_persons(
    p: Principal = Depends(require("persons.read")),
    q: str | None = Query(default=None, max_length=200, description="part of the name, any case"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    """Sorted by name. Pass next_cursor back as `cursor` for the next page."""
    offset = 0
    if cursor:
        if not cursor.isdigit():
            raise AppError(400, "invalid_cursor", "cursor is not valid")
        offset = int(cursor)
    query = p.scope.select(Person)
    if q:
        query = query.where(func.lower(Person.name).contains(q.lower(), autoescape=True))
    rows = await p.scope.list(query.order_by(func.lower(Person.name), Person.id).offset(offset).limit(limit + 1))
    return Page[PersonOut](
        items=[to_out(x) for x in rows[:limit]],
        next_cursor=str(offset + limit) if len(rows) > limit else None,
    )


@router.post("", response_model=PersonOut, status_code=201)
async def create_person(
    request: Request,
    name: str = Form(min_length=1, max_length=200),
    photo: UploadFile = File(description="JPEG or PNG with one clear, front-facing face"),
    note: str | None = Form(default=None, max_length=500),
    p: Principal = Depends(require("persons.write")),
):
    """multipart/form-data. The face is recognised on this account's cameras from now on."""
    _check_enrollment(p)
    limit = get_settings().max_persons
    if await p.scope.count(Person) >= limit:
        raise AppError(409, "person_limit_reached", f"your plan allows {limit} persons", {"limit": limit})
    data = await _read_photo(photo)
    person = Person(id=uuid.uuid4(), tenant_id=p.tenant.id, name=name.strip(), note=note)
    person.photo_key = await faces.enroll(person.id, p.tenant.id, person.name, data, photo.content_type)
    p.scope.add(person)
    audit.record(p.scope.session, "person.created", tenant_id=p.tenant.id, actor_user_id=p.user.id,
                 target=str(person.id), ip=client_ip(request))  # fmt: skip
    await p.scope.commit()
    return to_out(person)


@router.get("/{person_id}", response_model=PersonOut)
async def get_person(person_id: uuid.UUID, p: Principal = Depends(require("persons.read"))):
    return to_out(await p.scope.get(Person, person_id, "person"))


@router.patch("/{person_id}", response_model=PersonOut)
async def update_person(person_id: uuid.UUID, body: PersonUpdate, p: Principal = Depends(require("persons.write"))):
    person = await p.scope.get(Person, person_id, "person")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(person, k, v.strip() if k == "name" else v)
    await p.scope.commit()
    return to_out(person)


@router.put("/{person_id}/photo", response_model=PersonOut)
async def replace_photo(
    person_id: uuid.UUID,
    request: Request,
    photo: UploadFile = File(),
    p: Principal = Depends(require("persons.write")),
):
    """Replaces the enrolled face. If the new photo has no face (422 no_face_found),
    the person is left without a photo and is not recognised until one is uploaded."""
    _check_enrollment(p)
    person = await p.scope.get(Person, person_id, "person")
    data = await _read_photo(photo)
    await faces.remove(person.id)
    person.photo_key = None
    await p.scope.commit()
    person.photo_key = await faces.enroll(person.id, p.tenant.id, person.name, data, photo.content_type)
    audit.record(p.scope.session, "person.photo_replaced", tenant_id=p.tenant.id, actor_user_id=p.user.id,
                 target=str(person.id), ip=client_ip(request))  # fmt: skip
    await p.scope.commit()
    return to_out(person)


@router.delete("/{person_id}", status_code=204)
async def delete_person(person_id: uuid.UUID, request: Request, p: Principal = Depends(require("persons.write"))):
    """Erases the face and the photo. Past events keep the person_id but lose the name."""
    person = await p.scope.get(Person, person_id, "person")
    if person.photo_key:
        await faces.remove(person.id)
    await p.scope.session.delete(person)
    audit.record(p.scope.session, "person.deleted", tenant_id=p.tenant.id, actor_user_id=p.user.id,
                 target=str(person.id), ip=client_ip(request))  # fmt: skip
    await p.scope.commit()
    return Response(status_code=204)
