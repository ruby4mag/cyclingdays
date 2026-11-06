"""Dashboard: read-only JSON API and the page that draws it (Phase 8).

* ``GET /``                          redirects to ``/dashboard``.
* ``GET /dashboard``                 the page (static HTML, JS and CSS; no build step).
* ``GET /api/dashboard/<section>``   one JSON document per section, so one slow or
  failing section never blanks the whole page.

All endpoints only read. The page loads no third-party code, and its CSP
allows scripts and styles from this origin only. Personal health data is
sent with ``Cache-Control: no-store``.

Every path here needs a signed-in user (see ``app.api.auth``) and shows
that user's own athlete only (Phase 10). A user who hasn't finished
onboarding gets 409 ``setup_required``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from app.auth import UserRecord
from app.services.dashboard import (
    MAX_ASSESSMENTS,
    MAX_FITNESS_DAYS,
    MAX_WEEKS,
    MAX_WORKOUT_DAYS,
    MAX_ZONE_DAYS,
    AthleteNotFoundError,
    DashboardService,
    InvalidRangeError,
)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
STATIC_DIR = WEB_DIR / "static"

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
# Added to every response (see app.main). The CSP is only sent with the
# dashboard page, so FastAPI's /docs (which loads Swagger UI) keeps working.
BASE_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",  # no-referrer would make form posts send Origin: null
    "X-Frame-Options": "DENY",
}
PAGE_HEADERS = {
    **BASE_SECURITY_HEADERS,
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Cache-Control": "no-cache",
}

pages = APIRouter(tags=["dashboard"], include_in_schema=False)
api = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@pages.get("/")
async def root() -> RedirectResponse:
    # Relative, so "/<prefix>/" behind a reverse proxy leads to "/<prefix>/dashboard".
    return RedirectResponse("dashboard")


@pages.api_route("/dashboard", methods=["GET", "HEAD"])
async def dashboard_page() -> FileResponse:
    return FileResponse(
        WEB_DIR / "index.html",
        media_type="text/html",
        headers=PAGE_HEADERS,
    )


def _service(request: Request) -> DashboardService:
    service: DashboardService = request.app.state.dashboard
    return service


def _slug(request: Request) -> str:
    """The signed-in user's own athlete (Phase 10): never taken from the request."""
    user: UserRecord | None = request.state.user
    if user is None or user.athlete_slug is None:
        raise HTTPException(status_code=409, detail="setup_required")
    return user.athlete_slug


async def _call(response: Response, coro: Any) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result: dict[str, Any] = await coro
    except AthleteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@api.get("/summary")
async def summary(request: Request, response: Response) -> dict[str, Any]:
    """Profile, FTP and zones, latest fitness, readiness, next workout, plan, sync."""
    return await _call(response, _service(request).summary(_slug(request)))


@api.get("/fitness")
async def fitness(
    request: Request,
    response: Response,
    days: Annotated[int, Query(ge=1, le=MAX_FITNESS_DAYS)] = 90,
) -> dict[str, Any]:
    """Daily CTL / ATL / TSB with authoritative flags."""
    return await _call(response, _service(request).fitness(_slug(request), days))


@api.get("/weekly")
async def weekly(
    request: Request,
    response: Response,
    weeks: Annotated[int, Query(ge=1, le=MAX_WEEKS)] = 12,
) -> dict[str, Any]:
    """Weekly TSS, hours, rides and distance, next to the plan's targets."""
    return await _call(response, _service(request).weekly(_slug(request), weeks))


@api.get("/zones")
async def zones(
    request: Request,
    response: Response,
    days: Annotated[int, Query(ge=1, le=MAX_ZONE_DAYS)] = 28,
) -> dict[str, Any]:
    """Time per power zone, with the source of each total."""
    return await _call(response, _service(request).zones(_slug(request), days))


@api.get("/calendar")
async def calendar(
    request: Request,
    response: Response,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> dict[str, Any]:
    """Per-day plan role, coach workout, rides and other calendar events."""
    return await _call(response, _service(request).calendar(_slug(request), start, end))


@api.get("/workouts")
async def workouts(
    request: Request,
    response: Response,
    days: Annotated[int, Query(ge=1, le=MAX_WORKOUT_DAYS)] = 28,
) -> dict[str, Any]:
    """Completed rides with their evaluation (newest first)."""
    return await _call(response, _service(request).workouts(_slug(request), days))


@api.get("/assessments")
async def assessments(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=MAX_ASSESSMENTS)] = 10,
) -> dict[str, Any]:
    """Recent AI coaching decisions: daily plans and ride reviews."""
    return await _call(response, _service(request).assessments(_slug(request), limit))


@api.get("/plan")
async def plan(request: Request, response: Response) -> dict[str, Any]:
    """The active long-term plan with progress per week."""
    return await _call(response, _service(request).plan(_slug(request)))
