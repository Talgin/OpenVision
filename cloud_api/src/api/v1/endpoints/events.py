import base64
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import Principal, require
from src.core.config import get_settings
from src.core.errors import AppError, not_found
from src.core.security import MEDIA, decode_token, utcnow
from src.models import Camera, CameraStatus, Event, Person
from src.schemas import Bucket, CameraCount, EventOut, Page, StatsOut
from src.services import media
from src.services.tokens import as_utc

router = APIRouter()
stats_router = APIRouter()
media_router = APIRouter()

MAX_RANGE = timedelta(days=31)


def _encode_cursor(e: Event) -> str:
    return base64.urlsafe_b64encode(f"{as_utc(e.occurred_at).isoformat()}|{e.id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, _, eid = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        return datetime.fromisoformat(ts), uuid.UUID(eid)
    except (ValueError, UnicodeDecodeError):
        raise AppError(400, "invalid_cursor", "cursor is not valid")


async def person_names(session: AsyncSession, tenant_id: uuid.UUID, events) -> dict[str, str]:
    ids = set()
    for e in events:
        if e.is_known and e.person_id:
            try:
                ids.add(uuid.UUID(e.person_id))
            except ValueError:
                pass
    if not ids:
        return {}
    rows = await session.execute(select(Person.id, Person.name).where(Person.tenant_id == tenant_id, Person.id.in_(ids)))
    return {str(pid): name for pid, name in rows.all()}


def to_out(e: Event, camera_names: dict[int, str], names: dict[str, str] | None = None) -> EventOut:
    has_media = bool(e.crop_key or e.frame_key)
    return EventOut(
        id=e.id,
        camera_id=e.camera_id,
        camera_name=camera_names.get(e.camera_id),
        kind=e.kind,
        occurred_at=as_utc(e.occurred_at),
        person_id=e.person_id,
        person_name=(names or {}).get(e.person_id or ""),
        is_known=e.is_known,
        confidence=e.confidence,
        bbox=e.bbox,
        crop_url=media.sign(e.tenant_id, e.crop_key),
        frame_url=media.sign(e.tenant_id, e.frame_key),
        media_expires_at=media.expires_at() if has_media else None,
        attributes=e.attributes,
    )


async def _camera_names(p: Principal) -> dict[int, str]:
    rows = await p.scope.session.execute(
        select(Camera.id, Camera.name).where(Camera.tenant_id == p.tenant.id, Camera.deleted.is_(False))
    )
    return dict(rows.all())


@router.get("", response_model=Page[EventOut])
async def list_events(
    p: Principal = Depends(require("events.read")),
    camera_id: list[int] | None = Query(default=None),
    kind: str | None = None,
    known: bool | None = None,
    person_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    """Newest first. Pass next_cursor back as `cursor` for the next page."""
    q = p.scope.select(Event)
    if camera_id:
        q = q.where(Event.camera_id.in_(camera_id))
    if kind:
        q = q.where(Event.kind == kind)
    if known is not None:
        q = q.where(Event.is_known.is_(known))
    if person_id:
        q = q.where(Event.person_id == person_id)
    if since:
        q = q.where(Event.occurred_at >= since)
    if until:
        q = q.where(Event.occurred_at < until)
    if cursor:
        ts, eid = _decode_cursor(cursor)
        q = q.where(or_(Event.occurred_at < ts, and_(Event.occurred_at == ts, Event.id < eid)))
    rows = await p.scope.list(q.order_by(Event.occurred_at.desc(), Event.id.desc()).limit(limit + 1))
    names = await _camera_names(p)
    people = await person_names(p.scope.session, p.tenant.id, rows[:limit])
    items = [to_out(e, names, people) for e in rows[:limit]]
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return Page[EventOut](items=items, next_cursor=next_cursor)


@router.get("/{event_id}", response_model=EventOut)
async def get_event(event_id: uuid.UUID, p: Principal = Depends(require("events.read"))):
    e = await p.scope.get(Event, event_id, "event")
    return to_out(e, await _camera_names(p), await person_names(p.scope.session, p.tenant.id, [e]))


def _hour_bucket(session):
    if session.bind.dialect.name == "postgresql":
        return func.date_trunc("hour", Event.occurred_at)
    return func.strftime("%Y-%m-%d %H:00:00", Event.occurred_at)


@stats_router.get("/summary", response_model=StatsOut)
async def summary(
    p: Principal = Depends(require("events.read")),
    since: datetime | None = None,
    until: datetime | None = None,
):
    """Counts for the dashboard. Defaults to the last 24 hours; at most 31 days."""
    until = until or utcnow()
    since = since or until - timedelta(hours=24)
    if until - since > MAX_RANGE or since >= until:
        raise AppError(422, "invalid_range", "since must be before until, at most 31 days apart")
    session = p.scope.session
    in_range = (Event.tenant_id == p.tenant.id, Event.occurred_at >= since, Event.occurred_at < until)

    total, known_n, people = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Event.is_known.is_(True)),
                func.count(distinct(Event.person_id)),
            ).where(*in_range)
        )
    ).one()
    per_cam = (
        await session.execute(
            select(Event.camera_id, func.count(), func.count().filter(Event.is_known.is_(True)))
            .where(*in_range)
            .group_by(Event.camera_id)
        )
    ).all()
    bucket = _hour_bucket(session).label("b")
    per_hour = (
        await session.execute(select(bucket, func.count()).where(*in_range).group_by(bucket).order_by(bucket))
    ).all()

    names = await _camera_names(p)
    cams = await p.scope.list(p.scope.select(Camera).where(Camera.deleted.is_(False)))

    def _ts(v) -> datetime:
        dt = v if isinstance(v, datetime) else datetime.fromisoformat(v)
        return as_utc(dt)

    return StatsOut(
        since=since,
        until=until,
        total_events=total,
        known_events=known_n,
        unique_people=people,
        cameras_online=sum(1 for c in cams if c.status == CameraStatus.ONLINE),
        cameras_total=len(cams),
        by_camera=[
            CameraCount(camera_id=cid, camera_name=names.get(cid, f"#{cid}"), events=n, known=k)
            for cid, n, k in per_cam
            if cid in names
        ],
        by_hour=[Bucket(start=_ts(b), events=n) for b, n in per_hour],
    )


@media_router.get("/{token}")
async def get_media(token: str):
    """Local media backend only. The signed token is the authorisation; it expires."""
    s = get_settings()
    if s.media_backend != "local":
        raise not_found("media")
    try:
        payload = decode_token(token, MEDIA)
    except jwt.PyJWTError:
        raise AppError(403, "media_link_expired", "this link has expired, reload the event")
    root = Path(s.media_local_dir).resolve()
    path = (root / payload["key"]).resolve()
    if root not in path.parents or not path.is_file():
        raise not_found("media")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

