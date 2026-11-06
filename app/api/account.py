"""The signed-in user's own account: onboarding and settings (Phase 10).

* ``GET  /account``                    the page (setup wizard until complete, then settings).
* ``GET  /api/account``                user, athlete profile, Intervals.icu connection, goal.
* ``PUT  /api/account/intervals``      connect: athlete id + API key, checked against
                                       Intervals.icu; creates the athlete on first connect.
* ``DELETE /api/account/intervals``    disconnect (coaching stops; data stays).
* ``PUT  /api/account/profile``        physiology and preferences (FTP, weight, ...).
* ``POST /api/account/goal/preview``   the plan a goal would produce, nothing stored.
* ``PUT  /api/account/goal``           store the goal and (re)build the long-term plan.
* ``PUT  /api/account/notifications``  email address and daily summary on/off.

Every endpoint acts on the session's own user and athlete; none takes a user
or athlete id. Writes need a same-origin JSON request (no form posts, so a
cross-site page can't submit them). The API key is write-only: responses
never include it.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import _same_origin
from app.api.dashboard import PAGE_HEADERS, WEB_DIR
from app.auth import UserRecord
from app.config import ExperienceLevel, Settings, _validate_timezone
from app.database.models import AppUser, AthleteProfile
from app.database.repositories import AthleteProfileRepository, TrainingPlanRepository
from app.integrations.intervals_client import IntervalsError
from app.integrations.intervals_mappers import athlete_snapshot, cycling_ftp
from app.services import connections
from app.services.connections import CredentialsKeyError, InvalidCredentialsError
from app.services.training_plans import DAY_ROLES, Goal, create_plan, goal_of
from app.services.users import validate_email
from app.training.periodization import WEEKDAYS, DayRole, PlanPhases
from app.training.plan_builder import PlanBuildError

logger = logging.getLogger(__name__)

pages = APIRouter(include_in_schema=False)
api = APIRouter(prefix="/api/account", tags=["account"])

EXPERIENCE_LEVELS = ("beginner", "intermediate", "advanced", "elite")


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConnectRequest(_Strict):
    athlete_id: str = Field(min_length=1, max_length=32)
    api_key: str = Field(min_length=1, max_length=256)


class ProfileRequest(_Strict):
    display_name: str = Field(min_length=1, max_length=80)
    ftp_watts: int | None = Field(default=None, gt=0, le=600)
    weight_kg: float | None = Field(default=None, gt=20, le=250)
    height_cm: float | None = Field(default=None, gt=100, le=250)
    age_years: int | None = Field(default=None, gt=10, le=100)
    experience_level: ExperienceLevel
    timezone: str = Field(min_length=1, max_length=64)
    secondary_objective: str | None = Field(default=None, max_length=200)
    indoor_platform: str | None = Field(default=None, max_length=60)
    wearable: str | None = Field(default=None, max_length=60)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        return _validate_timezone(value)


class GoalRequest(_Strict):
    objective: str = Field(min_length=1, max_length=200)
    event_date: dt.date | None = None
    weekly_hours_min: float = Field(ge=1, le=30)
    weekly_hours_max: float = Field(ge=1, le=30)
    start_date: dt.date | None = None
    week_template: dict[str, DayRole] | None = None

    def goal(self) -> Goal:
        return Goal(
            weekly_hours=(self.weekly_hours_min, self.weekly_hours_max),
            objective=self.objective,
            event_date=self.event_date,
            start=self.start_date,
            week_template=self.week_template,
        )


class NotificationsRequest(_Strict):
    email: str | None = Field(default=None, max_length=254)
    daily_summary: bool = True


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _user(request: Request) -> UserRecord:
    user: UserRecord | None = request.state.user
    if user is None:  # unreachable behind the login gate
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def json_write(request: Request) -> None:
    """Writes: same-origin and JSON only (a cross-site form can't send JSON)."""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="Cross-site request refused")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if request.method != "DELETE" and content_type != "application/json":
        raise HTTPException(status_code=415, detail="Send JSON")


Write = Annotated[None, Depends(json_write)]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def _load(session: AsyncSession, user: UserRecord) -> tuple[AppUser, AthleteProfile | None]:
    row = await session.get(AppUser, user.id)
    if row is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    athlete = await session.get(AthleteProfile, row.athlete_id) if row.athlete_id else None
    return row, athlete


def _require_athlete(athlete: AthleteProfile | None) -> AthleteProfile:
    if athlete is None:
        raise HTTPException(status_code=409, detail="Connect Intervals.icu first")
    return athlete


def _today(athlete: AthleteProfile) -> dt.date:
    return dt.datetime.now(ZoneInfo(athlete.timezone)).date()


async def _free_slug(session: AsyncSession, wanted: str) -> str:
    slug, n = wanted, 1
    while await session.scalar(select(AthleteProfile.id).where(AthleteProfile.slug == slug)):
        n += 1
        slug = f"{wanted}-{n}"
    return slug


def _initial_timezone(remote: str | None, fallback: str) -> str:
    try:
        return _validate_timezone(remote) if remote else fallback
    except ValueError:
        return fallback


async def _account(session: AsyncSession, user: UserRecord) -> dict[str, Any]:
    row, athlete = await _load(session, user)
    out: dict[str, Any] = {
        "user": {
            "username": row.username,
            "email": row.email,
            "daily_summary": row.notify_daily_summary,
        },
        "athlete": None,
        "connection": None,
        "goal": None,
        "options": {
            "experience_levels": list(EXPERIENCE_LEVELS),
            "day_roles": list(DAY_ROLES),
            "weekdays": list(WEEKDAYS),
        },
    }
    plan_row = None
    if athlete is not None:
        out["athlete"] = {
            "slug": athlete.slug,
            "display_name": athlete.display_name,
            "ftp_watts": athlete.ftp_watts,
            "intervals_ftp_watts": _remote_ftp(athlete),
            "weight_kg": athlete.weight_kg,
            "height_cm": athlete.height_cm,
            "age_years": athlete.age_years,
            "experience_level": athlete.experience_level,
            "timezone": athlete.timezone,
            "primary_objective": athlete.primary_objective,
            "secondary_objective": athlete.secondary_objective,
            "indoor_platform": athlete.indoor_platform,
            "wearable": athlete.wearable,
            "last_sync_at": _iso(athlete.intervals_synced_at),
        }
        conn = await connections.get_connection(session, athlete.id)
        if conn is not None:
            out["connection"] = {
                "intervals_athlete_id": conn.intervals_athlete_id,
                "status": conn.status,
                "last_error": conn.last_error,
                "connected_at": _iso(conn.created_at),
                "last_verified_at": _iso(conn.last_verified_at),
            }
        plan_row = await TrainingPlanRepository(session).get_active(athlete.id)
        if plan_row is not None:
            goal = goal_of(plan_row.plan_metadata) or {}
            try:
                template = PlanPhases.from_json(plan_row.phases).week_template
            except ValueError:
                template = None
            hours = (athlete.availability or {}).get("weekly_hours")
            out["goal"] = {
                "plan_name": plan_row.name,
                "start_date": plan_row.start_date.isoformat(),
                "end_date": _iso(plan_row.end_date),
                "objective": goal.get("objective") or plan_row.objective,
                "event_date": goal.get("event_date"),
                "weekly_hours": goal.get("weekly_hours") or hours,
                "week_template": template,
            }
    connected = out["connection"] is not None and out["connection"]["status"] == "active"
    profile_ok = athlete is not None and athlete.ftp_watts is not None
    out["setup"] = {
        "connected": connected,
        "profile": profile_ok,
        "goal": plan_row is not None,
        "complete": connected and profile_ok and plan_row is not None,
    }
    return out


def _remote_ftp(athlete: AthleteProfile) -> int | None:
    return cycling_ftp(athlete.intervals_snapshot or {})


def _iso(value: dt.date | dt.datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@pages.api_route("/account", methods=["GET", "HEAD"])
async def account_page() -> FileResponse:
    return FileResponse(WEB_DIR / "account.html", media_type="text/html", headers=PAGE_HEADERS)


@api.get("")
async def get_account(request: Request, response: Response) -> dict[str, Any]:
    _no_store(response)
    async with _sessions(request)() as session:
        return await _account(session, _user(request))


@api.put("/intervals")
async def connect_intervals(
    request: Request, response: Response, body: ConnectRequest, _write: Write
) -> dict[str, Any]:
    """Check the key with Intervals.icu, then store it encrypted."""
    _no_store(response)
    user = _user(request)
    settings = _settings(request)
    try:
        athlete_id = connections.normalize_athlete_id(body.athlete_id)
        api_key = connections.normalize_api_key(body.api_key)
        connections.cipher(settings)  # fail before calling Intervals.icu
        async with connections.client_for(settings, athlete_id, api_key) as client:
            raw, summary = await connections.verify_credentials(client)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CredentialsKeyError as exc:
        logger.error("credentials_key_problem", extra={"error": str(exc)})
        raise HTTPException(
            status_code=503, detail="The server can't store keys yet; tell the administrator"
        ) from exc
    except IntervalsError as exc:
        logger.warning("intervals_verify_failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=502, detail="Intervals.icu didn't answer; try again in a minute"
        ) from exc

    async with _sessions(request)() as session:
        try:
            async with session.begin():
                row, athlete = await _load(session, user)
                created = athlete is None
                if athlete is None:
                    remote_ftp = summary.get("ride_ftp_watts")
                    remote_weight = summary.get("weight_kg")
                    athlete = await AthleteProfileRepository(session).create(
                        slug=await _free_slug(session, row.username),
                        display_name=(summary.get("name") or row.username)[:80],
                        ftp_watts=remote_ftp if remote_ftp and 0 < remote_ftp <= 600 else None,
                        ftp_updated_at=dt.datetime.now(dt.UTC) if remote_ftp else None,
                        weight_kg=(
                            remote_weight if remote_weight and 20 < remote_weight <= 250 else None
                        ),
                        experience_level="intermediate",
                        timezone=_initial_timezone(summary.get("timezone"), settings.app_timezone),
                        intervals_snapshot=athlete_snapshot(raw),
                    )
                    row.athlete_id = athlete.id
                await connections.store_connection(session, settings, athlete, athlete_id, api_key)
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IntegrityError as exc:  # a concurrent connect of the same athlete
            raise HTTPException(
                status_code=409, detail="That Intervals.icu athlete is already connected"
            ) from exc
        logger.info(
            "account_connected",
            extra={"username": user.username, "athlete": athlete.slug, "new_athlete": created},
        )
        return await _account(session, user)


@api.delete("/intervals")
async def disconnect_intervals(
    request: Request, response: Response, _write: Write
) -> dict[str, Any]:
    _no_store(response)
    user = _user(request)
    async with _sessions(request)() as session:
        async with session.begin():
            _, athlete = await _load(session, user)
            athlete = _require_athlete(athlete)
            conn = await connections.get_connection(session, athlete.id)
            if conn is not None:
                await session.delete(conn)
        logger.info("account_disconnected", extra={"username": user.username})
        return await _account(session, user)


@api.put("/profile")
async def update_profile(
    request: Request, response: Response, body: ProfileRequest, _write: Write
) -> dict[str, Any]:
    _no_store(response)
    user = _user(request)
    async with _sessions(request)() as session:
        async with session.begin():
            _, athlete = await _load(session, user)
            athlete = _require_athlete(athlete)
            if body.ftp_watts != athlete.ftp_watts:
                athlete.ftp_updated_at = dt.datetime.now(dt.UTC)
                logger.info(
                    "athlete_ftp_changed",
                    extra={
                        "athlete": athlete.slug,
                        "from": athlete.ftp_watts,
                        "to": body.ftp_watts,
                    },
                )
            for field, value in body.model_dump().items():
                # Optional text fields: empty means "not set".
                setattr(athlete, field, (value or None) if isinstance(value, str) else value)
            athlete.display_name = body.display_name
            athlete.timezone = body.timezone
        return await _account(session, user)


async def _goal(request: Request, body: GoalRequest, *, dry_run: bool) -> dict[str, Any]:
    user = _user(request)
    if body.weekly_hours_min > body.weekly_hours_max:
        raise HTTPException(status_code=422, detail="Minimum weekly hours above the maximum")
    async with _sessions(request)() as session:
        async with session.begin():
            _, athlete = await _load(session, user)
            athlete = _require_athlete(athlete)
            try:
                result = await create_plan(
                    session,
                    athlete,
                    body.goal(),
                    today=_today(athlete),
                    replace=True,
                    dry_run=dry_run,
                )
            except PlanBuildError as exc:
                raise HTTPException(status_code=422, detail=_sentence(str(exc))) from exc
        if not dry_run:
            logger.info(
                "account_goal_set",
                extra={"athlete": athlete.slug, "plan_id": result.get("plan_id")},
            )
            result["account"] = await _account(session, user)
        return result


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


@api.post("/goal/preview")
async def preview_goal(
    request: Request, response: Response, body: GoalRequest, _write: Write
) -> dict[str, Any]:
    _no_store(response)
    return await _goal(request, body, dry_run=True)


@api.put("/goal")
async def set_goal(
    request: Request, response: Response, body: GoalRequest, _write: Write
) -> dict[str, Any]:
    _no_store(response)
    return await _goal(request, body, dry_run=False)


@api.put("/notifications")
async def update_notifications(
    request: Request, response: Response, body: NotificationsRequest, _write: Write
) -> dict[str, Any]:
    _no_store(response)
    user = _user(request)
    try:
        email = validate_email(body.email)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="That isn't an email address") from exc
    async with _sessions(request)() as session:
        async with session.begin():
            row, _athlete = await _load(session, user)
            row.email = email
            row.notify_daily_summary = body.daily_summary
        return await _account(session, user)
