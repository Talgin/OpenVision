import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from src.api.v1.api import api_router, internal_router
from src.core import errors
from src.core.config import get_settings
from src.db.session import create_all
from src.schemas import ErrorOut

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    s.check_production()
    if s.db_auto_create:
        await create_all()
    yield


settings = get_settings()
app = FastAPI(
    title=settings.project_name,
    version=settings.version,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)
errors.install(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Declares the real error body for every route; also replaces FastAPI's
# default 422 schema (HTTPValidationError), which this API never returns.
ERROR_RESPONSES = {
    code: {"model": ErrorOut, "description": desc}
    for code, desc in {
        400: "Bad request (error.code says which)",
        401: "Not signed in, or the token expired",
        403: "Not allowed for this role, or account suspended",
        404: "Not found (also: belongs to another account)",
        409: "Conflict (email_taken, camera_limit_reached, ...)",
        422: "Validation failed; details is a list of {field, message}",
        429: "Rate limited; details.retry_after in seconds",
    }.items()
}
app.include_router(api_router, prefix="/api/v1", responses=ERROR_RESPONSES)
app.include_router(internal_router, prefix="/internal")


@app.get("/health", tags=["status"])
async def health():
    return {"service": settings.project_name, "version": settings.version}
