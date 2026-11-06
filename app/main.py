"""FastAPI application factory.

Run with: ``uvicorn app.main:create_app --factory``
(The factory avoids reading settings at import time.)
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.auth import authenticate
from app.api.auth import router as auth_router
from app.api.dashboard import BASE_SECURITY_HEADERS, STATIC_DIR
from app.api.dashboard import api as dashboard_api
from app.api.dashboard import pages as dashboard_pages
from app.api.routes import router as health_router
from app.auth import LoginThrottle, SessionSigner
from app.config import Settings, get_settings
from app.database.connection import (
    create_db_engine,
    create_session_factory,
    expected_head_revision,
)
from app.logging_config import configure_logging, request_id_var
from app.services.dashboard import DashboardService
from app.services.users import SqlUserStore

logger = logging.getLogger("app")

_QUIET_PATHS = frozenset({"/health", "/health/ready"})
_REQUEST_ID_HEADER = "X-Request-ID"


def create_app(settings: Settings | None = None, *, athlete_slug: str | None = None) -> FastAPI:
    """``athlete_slug``: the athlete the dashboard shows (default: ``ATHLETE_SLUG``)."""
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_db_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.dashboard = DashboardService(app.state.session_factory)
        app.state.users = SqlUserStore(app.state.session_factory)
        app.state.expected_revision = expected_head_revision()
        logger.info(
            "application_started",
            extra={
                "version": __version__,
                "environment": settings.app_env,
                "expected_db_revision": app.state.expected_revision,
            },
        )
        try:
            yield
        finally:
            await engine.dispose()
            logger.info("application_stopped")

    app = FastAPI(title="AI Cycling Coach", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.athlete_slug = athlete_slug  # None: resolved from ATHLETE_SLUG on first use
    if settings.auth_secret_key is None:
        logger.warning(
            "auth_secret_key_missing",
            extra={"detail": "AUTH_SECRET_KEY unset: random key, sessions end on restart"},
        )
        secret = secrets.token_urlsafe(48)
    else:
        secret = settings.auth_secret_key.get_secret_value()
    app.state.session_signer = SessionSigner(secret)
    app.state.login_throttle = LoginThrottle()
    # scrypt uses 32 MiB per hash: never run many at once.
    app.state.hash_slots = asyncio.Semaphore(2)

    # Registered first, so it runs inside request_context (logged, headers added).
    @app.middleware("http")
    async def require_login(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        denied = await authenticate(request)
        if denied is not None:
            return denied
        return await call_next(request)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(_REQUEST_ID_HEADER, "")
        request_id = incoming if 0 < len(incoming) <= 128 else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed", extra={"method": request.method, "path": request.url.path}
            )
            raise
        finally:
            request_id_var.reset(token)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[_REQUEST_ID_HEADER] = request_id
        if request.url.path.startswith("/static/"):
            # Revalidate every time (a cheap 304), so a deploy never pairs new
            # HTML with a stale stylesheet or script from the browser cache.
            response.headers.setdefault("Cache-Control", "no-cache")
        for name, value in BASE_SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        level = logging.DEBUG if request.url.path in _QUIET_PATHS else logging.INFO
        logger.log(
            level,
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(dashboard_pages)
    app.include_router(dashboard_api)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
