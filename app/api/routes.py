"""Health endpoints.

* ``GET /health``        — liveness: the process is up. No dependencies checked.
* ``GET /health/ready``  — readiness: database reachable AND schema at the
  expected Alembic head. Returns 503 otherwise, so orchestration (compose
  healthcheck, monitoring) never treats a half-migrated app as healthy.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app import __version__
from app.database.connection import current_revision, ping

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class CheckResult(BaseModel):
    status: Literal["ok", "error"]
    detail: str | None = None
    data: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    service: str
    version: str
    environment: str
    checks: dict[str, CheckResult] = {}


@router.get("/health", response_model=HealthResponse)
async def liveness(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    return HealthResponse(
        status="ok", service=settings.app_name, version=__version__, environment=settings.app_env
    )


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse}},
)
async def readiness(request: Request, response: Response) -> HealthResponse:
    settings = request.app.state.settings
    engine = request.app.state.engine
    expected: str | None = request.app.state.expected_revision
    timeout = settings.health_db_timeout_seconds
    checks: dict[str, CheckResult] = {}

    try:
        await asyncio.wait_for(ping(engine), timeout=timeout)
        checks["database"] = CheckResult(status="ok")
    except Exception as exc:
        logger.warning("readiness_database_failed", extra={"error": type(exc).__name__})
        checks["database"] = CheckResult(status="error", detail=type(exc).__name__)

    if checks["database"].status == "ok":
        try:
            applied = await asyncio.wait_for(current_revision(engine), timeout=timeout)
            ok = applied is not None and applied == expected
            checks["migrations"] = CheckResult(
                status="ok" if ok else "error",
                detail=None if ok else "database schema is not at the expected revision",
                data={"applied": applied, "expected": expected},
            )
        except Exception as exc:
            logger.warning("readiness_migrations_failed", extra={"error": type(exc).__name__})
            checks["migrations"] = CheckResult(status="error", detail=type(exc).__name__)
    else:
        checks["migrations"] = CheckResult(status="error", detail="database unavailable")

    overall: Literal["ok", "error"] = (
        "ok" if all(c.status == "ok" for c in checks.values()) else "error"
    )
    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status=overall,
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
        checks=checks,
    )
