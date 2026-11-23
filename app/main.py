"""Dvarc API entry point.

The service is intentionally read-only for clients: every endpoint is a GET.
Versions are added or removed by editing the ``versions/`` folder on the host.
"""

from __future__ import annotations

import logging
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .auth import RateLimiter, TokenStore, hash_token, utcnow
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()

    store = TokenStore(settings.data_dir)
    registry = VersionRegistry(settings.versions_dir)
    events = EventStore(settings.data_dir)
    limiter = RateLimiter(settings.token_rate_limit, settings.token_rate_window)

    app = FastAPI(
        title="Dvarc API",
        version="1.0.0",
        description=(
            "API de solo lectura para distribuir versiones pre-hechas del Dvarc Launcher. "
            "Las versiones se administran desde el sistema de archivos del host."
        ),
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.registry = registry
    app.state.events = events
    app.state.limiter = limiter

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

    def require_token(request: Request) -> tuple[str, dict]:
        raw = token_from_request(request)
        record = request.app.state.store.verify(raw)
        if record is None:
            raise HTTPException(status_code=401, detail={"error": "invalid_token"})
        return hash_token(raw), record

    def current_usernames(auth: tuple[str, dict]) -> list[str]:
        return [str(name) for name in auth[1].get("usernames", [])]

    def base_url() -> str:
        return settings.public_base_url or ""

    @app.exception_handler(VersionError)
    async def version_error_handler(_: Request, exc: VersionError) -> JSONResponse:
        if exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 403:
            code = "forbidden"
        else:
            code = "invalid_version"
        return JSONResponse(status_code=exc.status_code, content={"error": code, "detail": str(exc)})

    # ------------------------------------------------------------ endpoints
    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"name": "Dvarc API", "docs": "/docs"}

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        return HealthResponse(status="ok", versions=request.app.state.registry.count(), time=utcnow())

    @app.get("/api/v1/token", response_model=TokenResponse)
    def issue_token(request: Request) -> TokenResponse:
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(status_code=429, detail={"error": "rate_limited"})
        token, record = store.create_token()
        logger.info("issued token at %s", record["createdAt"])
        return TokenResponse(token=token, createdAt=record["createdAt"])

    @app.get("/api/v1/token/bind", response_model=BindResponse)
    def bind_token(username: str, auth: tuple[str, dict] = Depends(require_token)):
        digest, _ = auth
        if not USERNAME_RE.match(username):
            raise HTTPException(status_code=400, detail={"error": "invalid_username"})
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
    def me(auth: tuple[str, dict] = Depends(require_token)) -> MeResponse:
        return MeResponse(usernames=current_usernames(auth))

    @app.get("/api/v1/events", response_model=list[EventSummary])
    def list_events(auth: tuple[str, dict] = Depends(require_token)) -> list[EventSummary]:
        return [EventSummary(**event) for event in events.list_for(current_usernames(auth))]

    @app.get("/api/v1/events/{event_id}", response_model=EventSummary)
    def get_event(event_id: str, auth: tuple[str, dict] = Depends(require_token)) -> EventSummary:
        event = events.get(event_id, current_usernames(auth))
        if event is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return EventSummary(**event)

    @app.get("/api/v1/versions", response_model=list[VersionSummary])
    def list_versions(auth: tuple[str, dict] = Depends(require_token)) -> list[VersionSummary]:
        usernames = current_usernames(auth)
        return [version.summary(base_url()) for version in registry.list_all() if version.is_allowed(usernames)]

    @app.get("/api/v1/versions/{version_id}", response_model=VersionDetail)
    def get_version(version_id: str, auth: tuple[str, dict] = Depends(require_token)) -> VersionDetail:
        version = registry.get(version_id)
        if not version.is_allowed(current_usernames(auth)):
            raise HTTPException(status_code=403, detail={"error": "version_forbidden"})
        return version.detail(base_url())

    @app.get("/api/v1/versions/{version_id}/files/{file_path:path}")
    def download_file(
        version_id: str,
        file_path: str,
        auth: tuple[str, dict] = Depends(require_token),
    ) -> FileResponse:
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


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = create_app()
