"""Dvarc API entry point.

The service is intentionally read-only for clients: every endpoint is a GET.
Versions are added or removed by editing the ``versions/`` folder on the host.
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .auth import RateLimiter, TokenStore, hash_token, utcnow
from .cloudflare import (
    CloudflareEventStore,
    CloudflareTokenStore,
    CloudflareVersionRegistry,
    R2BodyIterator,
    R2RangeError,
)
from .config import Settings
from .events import EventStore
from .models import (
    BindResponse,
    EventSummary,
    HealthResponse,
    MeResponse,
    TokenResponse,
    VersionDetail,
    VersionSummary,
)
from .versions import VersionError, VersionRegistry

logger = logging.getLogger("dvarcapi")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
MEDIA_TYPES = {
    ".jar": "application/java-archive",
    ".zip": "application/zip",
    ".png": "image/png",
    ".json": "application/json",
}


def create_app(settings: Settings | None = None, *, cloudflare: bool = False) -> FastAPI:
    settings = settings or Settings.from_env()
    if not cloudflare:
        settings.ensure_dirs()

    store = None if cloudflare else TokenStore(settings.data_dir)
    registry = None if cloudflare else VersionRegistry(settings.versions_dir)
    events = None if cloudflare else EventStore(settings.data_dir)
    limiter = None if cloudflare else RateLimiter(settings.token_rate_limit, settings.token_rate_window)

    app = FastAPI(
        title="Dvarc API",
        version="1.0.0",
        description=(
            "API para distribuir versiones preparadas y eventos del Dvarc Launcher."
        ),
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.registry = registry
    app.state.events = events
    app.state.limiter = limiter
    app.state.cloudflare = cloudflare

    # The public website (dvarc.apurc.site) calls this API from another origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["X-Dvarc-Token", "Authorization", "Content-Type"],
        max_age=600,
    )

    # ------------------------------------------------------------- helpers
    def token_from_request(request: Request) -> str:
        raw = request.headers.get("x-dvarc-token", "").strip()
        if not raw:
            header = request.headers.get("authorization", "").strip()
            if header.lower().startswith("bearer "):
                raw = header[7:].strip()
        if not raw:
            raise HTTPException(status_code=401, detail={"error": "missing_token"})
        return raw

    async def require_token(request: Request) -> tuple[str, dict]:
        raw = token_from_request(request)
        if request.app.state.cloudflare:
            db = _cloudflare_db(request)
            record = await CloudflareTokenStore(db).verify(raw)
        else:
            record = request.app.state.store.verify(raw)
        if record is None:
            raise HTTPException(status_code=401, detail={"error": "invalid_token"})
        return hash_token(raw), record

    def current_usernames(auth: tuple[str, dict]) -> list[str]:
        return [str(name) for name in auth[1].get("usernames", [])]

    def base_url(request: Request) -> str:
        if settings.public_base_url:
            return settings.public_base_url
        return str(request.base_url).rstrip("/") if request.app.state.cloudflare else ""

    @app.exception_handler(VersionError)
    async def version_error_handler(_: Request, exc: VersionError) -> JSONResponse:
        if exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 403:
            code = "forbidden"
        elif exc.status_code == 416:
            code = "range_not_satisfiable"
        else:
            code = "invalid_version"
        headers = {"Content-Range": f"bytes */{exc.size}"} if isinstance(exc, R2RangeError) else None
        return JSONResponse(status_code=exc.status_code, content={"error": code, "detail": str(exc)}, headers=headers)

    # ------------------------------------------------------------ endpoints
    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"name": "Dvarc API", "docs": "/docs"}

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        if request.app.state.cloudflare:
            versions = await CloudflareVersionRegistry(_cloudflare_bucket(request)).count()
        else:
            versions = request.app.state.registry.count()
        return HealthResponse(status="ok", versions=versions, time=utcnow())

    @app.get("/api/v1/token", response_model=TokenResponse)
    async def issue_token(request: Request) -> TokenResponse:
        client = request.headers.get("cf-connecting-ip", "").strip()
        if not client:
            client = request.client.host if request.client else "unknown"
        if request.app.state.cloudflare:
            token_store = CloudflareTokenStore(_cloudflare_db(request))
            if not await token_store.rate_allow(client, settings.token_rate_limit, settings.token_rate_window):
                raise HTTPException(status_code=429, detail={"error": "rate_limited"})
            token, record = await token_store.create_token()
        else:
            if not limiter.allow(client):
                raise HTTPException(status_code=429, detail={"error": "rate_limited"})
            token, record = store.create_token()
        logger.info("issued token at %s", record["createdAt"])
        return TokenResponse(token=token, createdAt=record["createdAt"])

    @app.get("/api/v1/token/bind", response_model=BindResponse)
    async def bind_token(request: Request, username: str, auth: tuple[str, dict] = Depends(require_token)):
        digest, _ = auth
        if not USERNAME_RE.match(username):
            raise HTTPException(status_code=400, detail={"error": "invalid_username"})
        if request.app.state.cloudflare:
            status, owner = await CloudflareTokenStore(_cloudflare_db(request)).bind(username, digest)
        else:
            status, owner = store.bind(username, digest)
        if status == "invalid_token":
            raise HTTPException(status_code=401, detail={"error": "invalid_token"})
        if status == "conflict_discarded":
            return JSONResponse(
                status_code=409,
                content={"error": "username_already_bound", "username": owner, "discarded": True},
            )
        if status == "conflict":
            return JSONResponse(
                status_code=409,
                content={"error": "username_already_bound", "username": owner, "discarded": False},
            )
        return BindResponse(status=status, username=owner)

    @app.get("/api/v1/me", response_model=MeResponse)
    async def me(auth: tuple[str, dict] = Depends(require_token)) -> MeResponse:
        return MeResponse(usernames=current_usernames(auth))

    @app.get("/api/v1/events", response_model=list[EventSummary])
    async def list_events(request: Request, auth: tuple[str, dict] = Depends(require_token)) -> list[EventSummary]:
        if request.app.state.cloudflare:
            items = await CloudflareEventStore(_cloudflare_db(request)).list_for(current_usernames(auth))
        else:
            items = events.list_for(current_usernames(auth))
        return [EventSummary(**event) for event in items]

    @app.get("/api/v1/events/{event_id}", response_model=EventSummary)
    async def get_event(request: Request, event_id: str, auth: tuple[str, dict] = Depends(require_token)) -> EventSummary:
        if request.app.state.cloudflare:
            event = await CloudflareEventStore(_cloudflare_db(request)).get(event_id, current_usernames(auth))
        else:
            event = events.get(event_id, current_usernames(auth))
        if event is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return EventSummary(**event)

    @app.get("/api/v1/versions", response_model=list[VersionSummary])
    async def list_versions(request: Request, auth: tuple[str, dict] = Depends(require_token)) -> list[VersionSummary]:
        usernames = current_usernames(auth)
        if request.app.state.cloudflare:
            registry_cf = CloudflareVersionRegistry(_cloudflare_bucket(request))
            versions = await registry_cf.list_all()
            return [VersionSummary(**registry_cf.summary(version, base_url(request))) for version in versions if registry_cf.is_allowed(version, usernames)]
        return [version.summary(base_url(request)) for version in registry.list_all() if version.is_allowed(usernames)]

    @app.get("/api/v1/versions/{version_id}", response_model=VersionDetail)
    async def get_version(request: Request, version_id: str, auth: tuple[str, dict] = Depends(require_token)) -> VersionDetail:
        if request.app.state.cloudflare:
            registry_cf = CloudflareVersionRegistry(_cloudflare_bucket(request))
            version = await registry_cf.get(version_id)
            allowed = registry_cf.is_allowed(version, current_usernames(auth))
            detail = registry_cf.detail(version, base_url(request))
        else:
            version = registry.get(version_id)
            allowed = version.is_allowed(current_usernames(auth))
            detail = version.detail(base_url(request))
        if not allowed:
            raise HTTPException(status_code=403, detail={"error": "version_forbidden"})
        return VersionDetail(**detail) if request.app.state.cloudflare else detail

    @app.get("/api/v1/versions/{version_id}/files/{file_path:path}")
    async def download_file(
        version_id: str,
        file_path: str,
        request: Request,
        auth: tuple[str, dict] = Depends(require_token),
    ):
        if request.app.state.cloudflare:
            registry_cf = CloudflareVersionRegistry(_cloudflare_bucket(request))
            version = await registry_cf.get(version_id)
            if not registry_cf.is_allowed(version, current_usernames(auth)):
                raise HTTPException(status_code=403, detail={"error": "version_forbidden"})
            obj, entry, range_info = await registry_cf.get_file(version, file_path, request.headers.get("range"))
            headers = {
                "ETag": f'"{entry["sha256"]}"',
                "Cache-Control": "private, max-age=0, must-revalidate",
                "X-Content-SHA256": entry["sha256"],
                "Content-Length": str(range_info[1] - range_info[0] + 1 if range_info else entry["size"]),
            }
            if range_info:
                headers["Content-Range"] = f"bytes {range_info[0]}-{range_info[1]}/{entry['size']}"
                return StreamingResponse(R2BodyIterator(obj.body), status_code=206, media_type=MEDIA_TYPES.get("." + file_path.rsplit(".", 1)[-1], "application/octet-stream"), headers=headers)
            return StreamingResponse(R2BodyIterator(obj.body), media_type=MEDIA_TYPES.get("." + file_path.rsplit(".", 1)[-1], "application/octet-stream"), headers=headers)

        version = registry.get(version_id)
        if not version.is_allowed(current_usernames(auth)):
            raise HTTPException(status_code=403, detail={"error": "version_forbidden"})
        target, digest = registry.resolve_file(version, file_path)
        media_type = MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
        headers = {
            "ETag": f'"{digest}"',
            "Cache-Control": "private, max-age=0, must-revalidate",
            "X-Content-SHA256": digest,
        }
        return FileResponse(target, media_type=media_type, filename=target.name, headers=headers)

    return app


def _cloudflare_db(request: Request):
    env = request.scope.get("env")
    if env is None or not hasattr(env, "DB"):
        raise RuntimeError("Cloudflare D1 binding DB is not available")
    return env.DB


def _cloudflare_bucket(request: Request):
    env = request.scope.get("env")
    if env is None or not hasattr(env, "VERSIONS_BUCKET"):
        raise RuntimeError("Cloudflare R2 binding VERSIONS_BUCKET is not available")
    return env.VERSIONS_BUCKET


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

if not os.environ.get("DVARC_CLOUDFLARE"):
    app = create_app()
