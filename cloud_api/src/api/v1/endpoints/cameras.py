from fastapi import APIRouter, Depends, Request, Response

from src.api.deps import Principal, client_ip, require
from src.core.errors import AppError
from src.core.security import digest_matches, encrypt, new_opaque_token, redact_url, token_digest
from src.models import Camera, CameraStatus, EdgeAgent, SourceMode
from src.schemas import CameraCreate, CameraCreated, CameraOut, CameraUpdate, ProbeIn, ProbeOut, PushCredentials
from src.services import audit, ingest, live, probe

router = APIRouter()


def _visible(p: Principal):
    return p.scope.select(Camera).where(Camera.deleted.is_(False))


async def _get(p: Principal, camera_id: int) -> Camera:
    cam = await p.scope.get(Camera, camera_id, "camera")
    if cam.deleted:
        raise AppError(404, "not_found", "camera not found")
    return cam


def _push_credentials(camera_id: int, key: str) -> PushCredentials:
    srt, rtsp = ingest.publish_urls(camera_id, ingest.PUSH_USER, key)
    return PushCredentials(srt_url=srt, rtsp_url=rtsp, stream_key=key)


async def _probe_or_fail(url: str) -> ProbeOut:
    result = await probe.probe(url)
    if not result.ok:
        raise AppError(
            422, "probe_failed", "the stream could not be opened",
            {"reason": result.reason, "stream": result.stream.model_dump() if result.stream else None},
        )  # fmt: skip
    return result


async def _publish_status(cam: Camera) -> None:
    await live.publish(cam.tenant_id, "camera_status", CameraOut.model_validate(cam).model_dump(mode="json"))


@router.get("", response_model=list[CameraOut])
async def list_cameras(p: Principal = Depends(require("cameras.read"))):
    return await p.scope.list(_visible(p).order_by(Camera.id))


@router.post("/probe", response_model=ProbeOut)
async def probe_url(body: ProbeIn, p: Principal = Depends(require("cameras.write"))):
    """Try a pull URL without saving it. Always 200; `ok` and `reason` say what happened."""
    return await probe.probe(body.url)


@router.post("", response_model=CameraCreated, status_code=201)
async def create_camera(body: CameraCreate, request: Request, p: Principal = Depends(require("cameras.write"))):
    used = await p.scope.count(Camera, Camera.deleted.is_(False))
    if used >= p.tenant.max_cameras:
        raise AppError(
            409, "camera_limit_reached", f"your plan allows {p.tenant.max_cameras} cameras",
            {"limit": p.tenant.max_cameras},
        )  # fmt: skip

    cam = Camera(name=body.name, location=body.location, mode=body.mode, status=CameraStatus.PENDING)
    push_key = None
    if body.mode == SourceMode.PULL:
        if not body.source_url:
            raise AppError(422, "validation_error", "source_url is required for pull cameras")
        result = await _probe_or_fail(body.source_url)
        cam.stream_info = result.stream.model_dump() if result.stream else None
    elif body.mode == SourceMode.EDGE:
        if not body.source_url or not body.edge_agent_id:
            raise AppError(422, "validation_error", "source_url and edge_agent_id are required for edge cameras")
        if probe.check_url_shape(body.source_url):
            raise AppError(422, "validation_error", "source_url must be rtsp:// or rtsps://")
        await p.scope.get(EdgeAgent, body.edge_agent_id, "edge agent")
        cam.edge_agent_id = body.edge_agent_id
    else:
        push_key = new_opaque_token("ovk")
        cam.push_key_digest = token_digest(push_key)

    if body.source_url and body.mode != SourceMode.PUSH:
        cam.source_url_enc = encrypt(body.source_url)
        cam.source_url_redacted = redact_url(body.source_url)

    p.scope.add(cam)
    await p.scope.session.flush()
    audit.record(
        p.scope.session, "camera.created", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"camera:{cam.id}", ip=client_ip(request), meta={"mode": cam.mode.value},
    )  # fmt: skip
    await p.scope.commit()
    await _publish_status(cam)
    out = CameraCreated.model_validate(cam)
    if push_key:
        out.push = _push_credentials(cam.id, push_key)
    return out


@router.get("/{camera_id}", response_model=CameraOut)
async def get_camera(camera_id: int, p: Principal = Depends(require("cameras.read"))):
    return await _get(p, camera_id)


@router.patch("/{camera_id}", response_model=CameraOut)
async def update_camera(
    camera_id: int, body: CameraUpdate, request: Request, p: Principal = Depends(require("cameras.read"))
):
    changes = body.model_dump(exclude_unset=True)
    # Operators may switch a camera on and off; anything else is configuration.
    p.require("cameras.control" if set(changes) <= {"enabled"} else "cameras.write")
    cam = await _get(p, camera_id)

    if "source_url" in changes:
        url = changes.pop("source_url")
        if cam.mode == SourceMode.PUSH:
            raise AppError(422, "validation_error", "push cameras have no source_url")
        if cam.mode == SourceMode.PULL:
            result = await _probe_or_fail(url)
            cam.stream_info = result.stream.model_dump() if result.stream else None
        elif probe.check_url_shape(url):
            raise AppError(422, "validation_error", "source_url must be rtsp:// or rtsps://")
        cam.source_url_enc = encrypt(url)
        cam.source_url_redacted = redact_url(url)
        cam.ingest_configured = False  # scheduler re-applies the path config
        cam.status = CameraStatus.PENDING
        cam.status_reason = None

    for field, value in changes.items():
        setattr(cam, field, value)
    if changes.get("enabled") is False:
        cam.status, cam.status_reason = CameraStatus.DISABLED, None
    elif changes.get("enabled") is True and cam.status == CameraStatus.DISABLED:
        cam.status, cam.status_reason = CameraStatus.PENDING, None

    audit.record(
        p.scope.session, "camera.updated", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"camera:{cam.id}", ip=client_ip(request),
        meta={"fields": sorted(body.model_dump(exclude_unset=True))},
    )  # fmt: skip
    await p.scope.commit()
    await _publish_status(cam)
    return cam


@router.post("/{camera_id}/rotate-key", response_model=PushCredentials)
async def rotate_push_key(camera_id: int, request: Request, p: Principal = Depends(require("cameras.write"))):
    """New stream key for a push camera. The old key stops working for new connections."""
    cam = await _get(p, camera_id)
    if cam.mode != SourceMode.PUSH:
        raise AppError(422, "not_push_camera", "only push cameras have a stream key")
    key = new_opaque_token("ovk")
    cam.push_key_digest = token_digest(key)
    audit.record(
        p.scope.session, "camera.key_rotated", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"camera:{cam.id}", ip=client_ip(request),
    )  # fmt: skip
    await p.scope.commit()
    return _push_credentials(cam.id, key)


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(camera_id: int, request: Request, p: Principal = Depends(require("cameras.write"))):
    """Hidden at once; the scheduler stops ingest and analytics, then purges it and its events."""
    cam = await _get(p, camera_id)
    cam.deleted = True
    cam.enabled = False
    cam.push_key_digest = None
    audit.record(
        p.scope.session, "camera.deleted", tenant_id=p.tenant.id, actor_user_id=p.user.id,
        target=f"camera:{cam.id}", ip=client_ip(request),
    )  # fmt: skip
    await p.scope.commit()
    await live.publish(cam.tenant_id, "camera_deleted", {"id": cam.id})
    return Response(status_code=204)


def push_key_valid(cam: Camera, key: str) -> bool:
    return bool(cam.push_key_digest) and digest_matches(key, cam.push_key_digest)
