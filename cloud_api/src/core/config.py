"""Settings, all from env with the OV_ prefix. Secrets have no usable defaults."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OV_", env_file=".env", extra="ignore")

    project_name: str = "OpenVision Cloud API"
    version: str = "0.1.0"
    environment: str = "dev"  # dev | prod; prod refuses weak secrets
    public_base_url: str = "http://localhost:8000"

    database_url: str = "postgresql+asyncpg://openvision:openvision@postgres:5432/openvision"
    db_auto_create: bool = False  # tests/dev only; deployments run `alembic upgrade head`
    redis_url: str = "redis://redis:6379/0"

    # JWT
    jwt_secret: str = Field(default="", min_length=0)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    # Fernet key for camera URLs / credentials at rest
    secrets_key: str = ""

    # Login throttling: attempts per (email, ip) per window
    login_max_attempts: int = 5
    login_window_seconds: int = 60

    # Ingest (mediamtx)
    mediamtx_api_url: str = "http://mediamtx:9997"
    mediamtx_internal_rtsp: str = "rtsp://mediamtx:8554"
    ingest_public_host: str = "localhost"
    ingest_srt_port: int = 8890
    ingest_rtsp_port: int = 8554
    ingest_internal_user: str = "ovinternal"
    ingest_internal_password: str = ""
    # mediamtx calls our auth hook with ?secret=<this>; the hook is internal-only anyway
    ingest_hook_secret: str = ""
    # Pull sources on private / loopback addresses are refused (SSRF into our DC)
    allow_private_sources: bool = False
    probe_timeout_seconds: float = 10.0

    # Pipeline (trt-processor output stream)
    pipeline_redis_url: str = "redis://redis:6379/0"
    pipeline_output_stream: str = "filtered_events"  # trt-processor OUTPUT_REDIS_STREAM_KEY
    pipeline_consumer_group: str = "openvision-bridge"

    # Scheduler
    scheduler_interval_seconds: float = 10.0
    node_fail_threshold: int = 3

    # Media: "minio" (presigned GET) or "local" (signed URL served by this API)
    media_backend: str = "local"
    media_url_ttl_seconds: int = 600
    media_local_dir: str = "/data/media"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_secure: bool = False

    # Default quotas for a new tenant
    default_max_cameras: int = 4
    default_retention_days: int = 30
    default_plan: str = "body"  # body | face

    # Our own staff endpoints (/platform/*): X-Platform-Token header
    platform_token: str = ""

    cors_origins: str = "*"

    def check_production(self) -> None:
        if self.environment != "prod":
            return
        weak = [
            name
            for name in ("jwt_secret", "secrets_key", "ingest_internal_password", "ingest_hook_secret", "platform_token")
            if len(getattr(self, name)) < 32
        ]
        if weak:
            raise RuntimeError(f"weak or missing secrets in prod: {', '.join(weak)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
