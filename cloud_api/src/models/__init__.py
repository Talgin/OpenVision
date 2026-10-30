"""Tables. Every row a client can see carries tenant_id; see repositories/base.py
for how that is enforced.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.security import utcnow


class Base(DeclarativeBase):
    pass


def _enum(cls):
    return Enum(cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


class Plan(str, enum.Enum):
    BODY = "body"  # detection / tracking / counting, no faces
    FACE = "face"  # adds face detection + recognition


class Role(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class SourceMode(str, enum.Enum):
    PULL = "pull"  # we pull a public rtsp:// URL
    PUSH = "push"  # their NVR/VMS pushes SRT/RTSP to us
    EDGE = "edge"  # our edge agent on their LAN pushes for them


class CameraStatus(str, enum.Enum):
    PENDING = "pending"  # saved, not yet seen by the scheduler
    CONNECTING = "connecting"  # ingest configured, no video yet
    ONLINE = "online"  # video arriving and assigned to a GPU
    OFFLINE = "offline"  # was online, video stopped
    ERROR = "error"  # cannot proceed without the client doing something
    DISABLED = "disabled"  # switched off by the client


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[Plan] = mapped_column(_enum(Plan), default=Plan.BODY)
    max_cameras: Mapped[int] = mapped_column(Integer)
    retention_days: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set when the owner deletes the account: sign-in stops at once, the
    # scheduler tears down and purges cameras and events, then the tenant.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[Role] = mapped_column(_enum(Role))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(Base):
    """Rotated on every use. Presenting a used token revokes its whole family
    (someone copied it)."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    device_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EdgeAgent(Base):
    __tablename__ = "edge_agents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GpuNode(Base):
    """A trt-streamer-pyservicemaker instance with its nvmultiurisrcbin REST API."""

    __tablename__ = "gpu_nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    streamer_url: Mapped[str] = mapped_column(String(500))  # e.g. http://gpu1:9000/api/v1/stream
    plan: Mapped[Plan] = mapped_column(_enum(Plan))  # which pipeline build runs there
    capacity: Mapped[int] = mapped_column(Integer)  # 1080p-equivalent streams
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Camera(Base):
    __tablename__ = "cameras"

    # Global integer id: trt-processor does int(camera_id), and this is also the
    # DeepStream sensor id and the ingest path cam/<id>.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(300))
    mode: Mapped[SourceMode] = mapped_column(_enum(SourceMode))
    source_url_enc: Mapped[str | None] = mapped_column(String(2000))  # pull + edge: Fernet
    source_url_redacted: Mapped[str | None] = mapped_column(String(500))
    push_key_digest: Mapped[str | None] = mapped_column(String(64))  # push only
    edge_agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("edge_agents.id", ondelete="SET NULL"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[CameraStatus] = mapped_column(_enum(CameraStatus), default=CameraStatus.PENDING)
    status_reason: Mapped[str | None] = mapped_column(String(64))
    stream_info: Mapped[dict | None] = mapped_column(JSON)  # codec, width, height, fps
    gpu_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("gpu_nodes.id", ondelete="SET NULL"))
    ingest_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # scheduler tears down, then purges


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_tenant_time", "tenant_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # trt-processor event_id
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # face | person | line_cross ...
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    person_id: Mapped[str | None] = mapped_column(String(64))
    is_known: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    bbox: Mapped[list | None] = mapped_column(JSON)  # [x, y, w, h] in frame pixels
    crop_key: Mapped[str | None] = mapped_column(String(500))  # bucket/object
    frame_key: Mapped[str | None] = mapped_column(String(500))
    attributes: Mapped[dict | None] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Device(Base):
    """A phone registered for push notifications."""

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(16))  # android | ios | web
    push_token: Mapped[str] = mapped_column(String(500), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Security events. Never put tokens, passwords, URLs or personal data in meta."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(200))
    ip: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict | None] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
