"""Request / response bodies. This is the contract docs/MOBILE_API_HANDOFF.md describes;
change both together.
"""

import uuid
from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.models import CameraStatus, Plan, Role, SourceMode

T = TypeVar("T")


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _no_null(cls, v):
    """PATCH: an omitted field is left alone, an explicit null clears it. For
    fields that cannot be empty, null is an error instead of a 500."""
    if v is None:
        raise ValueError("cannot be null; omit the field to leave it unchanged")
    return v


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict | list | None = None


class ErrorOut(BaseModel):
    """Every non-2xx response. Branch on error.code."""

    error: ErrorBody


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


# --- auth ------------------------------------------------------------------

Password = Field(min_length=10, max_length=128)


class RegisterIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=200)
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Password
    device_name: str | None = Field(default=None, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    device_name: str | None = Field(default=None, max_length=200)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=200)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds until access_token expires


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Password


class TenantOut(Out):
    id: uuid.UUID
    name: str
    plan: Plan
    max_cameras: int
    retention_days: int


class UserOut(Out):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class MeOut(BaseModel):
    user: UserOut
    tenant: TenantOut
    permissions: list[str]


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    role: Role
    password: str = Password

    @field_validator("role")
    @classmethod
    def _not_owner(cls, v: Role) -> Role:
        if v == Role.OWNER:
            raise ValueError("there is exactly one owner per account")
        return v


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    is_active: bool | None = None

    check_not_null = field_validator("full_name", "role", "is_active", mode="before")(_no_null)

    @field_validator("role")
    @classmethod
    def _not_owner(cls, v: Role | None) -> Role | None:
        if v == Role.OWNER:
            raise ValueError("there is exactly one owner per account")
        return v


# --- cameras ---------------------------------------------------------------

class StreamInfo(BaseModel):
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None


class ProbeIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class ProbeOut(BaseModel):
    ok: bool
    reason: str | None = None  # same codes as Camera.status_reason
    stream: StreamInfo | None = None
    warnings: list[str] = []


class CameraCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=300)
    mode: SourceMode
    source_url: str | None = Field(default=None, min_length=8, max_length=2000)  # pull, edge
    edge_agent_id: uuid.UUID | None = None  # edge


class CameraUpdate(BaseModel):
    """Omitted = unchanged. `location: null` clears it; null elsewhere is a 422."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None
    source_url: str | None = Field(default=None, min_length=8, max_length=2000)

    check_not_null = field_validator("name", "enabled", "source_url", mode="before")(_no_null)


class PushCredentials(BaseModel):
    """Shown once, on create or rotate. We store only a hash."""

    srt_url: str
    rtsp_url: str
    stream_key: str


class CameraOut(Out):
    id: int
    name: str
    location: str | None
    mode: SourceMode
    source_url: str | None = Field(validation_alias="source_url_redacted")
    edge_agent_id: uuid.UUID | None
    enabled: bool
    status: CameraStatus
    status_reason: str | None
    stream_info: StreamInfo | None
    last_seen_at: datetime | None
    last_event_at: datetime | None
    created_at: datetime


class CameraCreated(CameraOut):
    push: PushCredentials | None = None


# --- edge agents -----------------------------------------------------------

class EdgeAgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class EdgeAgentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AgentToken(BaseModel):
    """Shown once, on create or rotate."""

    token: str
    install_command: str


class AdminSetPasswordIn(BaseModel):
    new_password: str = Password


class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class EdgeAgentOut(Out):
    id: uuid.UUID
    name: str
    last_seen_at: datetime | None
    created_at: datetime
    online: bool = False


class EdgeAgentCreated(EdgeAgentOut):
    token: str  # shown once
    install_command: str


class EdgeStreamConfig(BaseModel):
    camera_id: int
    source_url: str
    publish_url: str


class EdgeConfigOut(BaseModel):
    agent_id: uuid.UUID
    poll_seconds: int
    streams: list[EdgeStreamConfig]


# --- events ----------------------------------------------------------------

class EventOut(BaseModel):
    id: uuid.UUID
    camera_id: int
    camera_name: str | None = None
    kind: str
    occurred_at: datetime
    person_id: str | None
    person_name: str | None = None  # set when person_id is one of the tenant's persons
    is_known: bool
    confidence: float | None
    bbox: list[float] | None
    crop_url: str | None
    frame_url: str | None
    media_expires_at: datetime | None
    attributes: dict | None


class PersonOut(BaseModel):
    id: uuid.UUID
    name: str
    note: str | None
    photo_url: str | None
    media_expires_at: datetime | None
    created_at: datetime


class PersonUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=500)  # null clears

    check_not_null = field_validator("name", mode="before")(_no_null)


class CameraCount(BaseModel):
    camera_id: int
    camera_name: str
    events: int
    known: int


class Bucket(BaseModel):
    start: datetime
    events: int


class StatsOut(BaseModel):
    since: datetime
    until: datetime
    total_events: int
    known_events: int
    unique_people: int
    cameras_online: int
    cameras_total: int
    by_camera: list[CameraCount]
    by_hour: list[Bucket]


# --- devices ---------------------------------------------------------------

class DeviceIn(BaseModel):
    platform: Literal["android", "ios", "web"]
    push_token: str = Field(min_length=10, max_length=500)


class DeviceOut(Out):
    id: uuid.UUID
    platform: str
    created_at: datetime


# --- platform (our staff) --------------------------------------------------

class GpuNodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    streamer_url: str
    plan: Plan
    capacity: int = Field(ge=1, le=256)
    enabled: bool = True


class GpuNodeOut(Out):
    id: uuid.UUID
    name: str
    streamer_url: str
    plan: Plan
    capacity: int
    enabled: bool
    healthy: bool
    last_ok_at: datetime | None
    assigned: int = 0


class AuditOut(Out):
    id: int
    actor_user_id: uuid.UUID | None
    action: str
    target: str | None
    ip: str | None
    meta: dict | None
    at: datetime
