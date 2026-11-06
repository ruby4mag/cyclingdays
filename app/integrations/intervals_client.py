"""Async client for the Intervals.icu REST API.

Endpoints used here come from the official OpenAPI document served at
``https://intervals.icu/api/v1/docs`` (checked 2026-11-05). See
``docs/INTERVALS_ICU.md`` for the endpoint list and the field mapping that was
verified against real responses.

Authentication: HTTP Basic, username ``API_KEY``, password = personal API key
(OpenAPI security scheme ``APIKey``).

Retries: transport errors, timeouts, HTTP 429 and 5xx are retried with
exponential backoff and full jitter. ``Retry-After`` (seconds) is honoured on
429/503 when present. The OpenAPI document does not publish rate limits, so no
fixed request budget is assumed. Other 4xx errors are not retried.

Writes (Phase 5) are idempotent, so they are retried like reads: events are
created or updated with ``POST /events/bulk?upsert=true`` keyed by
``external_id`` (verified 2026-11-05: a plain ``POST /events`` with the same
``external_id`` creates a duplicate; the bulk upsert updates instead, also
with API-key auth). Deleting an event by id is idempotent too.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import Any, Self, cast

import httpx2 as httpx

from app import __version__
from app.config import Settings

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
API_KEY_USERNAME = "API_KEY"  # fixed username for API-key Basic auth
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ERROR_BODY = 500

JSONObject = dict[str, Any]
Sleep = Callable[[float], Awaitable[None]]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class IntervalsError(Exception):
    """Base class for Intervals.icu client errors."""

    def __init__(
        self, message: str, *, status_code: int | None = None, path: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path


class IntervalsConfigError(IntervalsError):
    """API key or athlete id missing."""


class IntervalsAuthError(IntervalsError):
    """401/403: bad API key or no access to the athlete."""


class IntervalsNotFoundError(IntervalsError):
    """404: resource (or athlete) does not exist."""


class IntervalsRateLimitError(IntervalsError):
    """429 after all retries were used."""


class IntervalsServerError(IntervalsError):
    """5xx after all retries were used."""


class IntervalsTransportError(IntervalsError):
    """Network error or timeout after all retries were used."""


class IntervalsResponseError(IntervalsError):
    """Unexpected status or a body that is not the documented JSON shape."""


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def _iso(value: dt.date | dt.datetime) -> str:
    """Intervals.icu takes *local* ISO-8601 dates (or naive local date-times)."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            raise ValueError("Pass a naive local datetime; the API interprets it as local time")
        return value.replace(microsecond=0).isoformat()
    return value.isoformat()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form: fall back to computed backoff


class IntervalsClient:
    """Thin, typed wrapper over the endpoints the coach needs.

    Use as an async context manager::

        async with IntervalsClient.from_settings(settings) as client:
            athlete = await client.get_athlete()
    """

    def __init__(
        self,
        *,
        api_key: str,
        athlete_id: str,
        base_url: str = "https://intervals.icu",
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise IntervalsConfigError("Intervals.icu API key is empty")
        if not athlete_id:
            raise IntervalsConfigError("Intervals.icu athlete id is empty")
        self.athlete_id = athlete_id
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=httpx.BasicAuth(API_KEY_USERNAME, api_key),
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Accept": "application/json",
                "User-Agent": f"ai-cycling-coach/{__version__}",
            },
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> IntervalsClient:
        if settings.intervals_api_key is None or settings.intervals_athlete_id is None:
            raise IntervalsConfigError(
                "Intervals.icu is not configured: set INTERVALS_API_KEY and "
                "INTERVALS_ATHLETE_ID in .env"
            )
        kwargs: dict[str, Any] = {
            "api_key": settings.intervals_api_key.get_secret_value(),
            "athlete_id": settings.intervals_athlete_id,
            "base_url": settings.intervals_base_url,
            "timeout_seconds": settings.intervals_timeout_seconds,
            "max_retries": settings.intervals_max_retries,
            "backoff_base_seconds": settings.intervals_backoff_base_seconds,
            "backoff_max_seconds": settings.intervals_backoff_max_seconds,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- endpoints ----------------------------------------------------------

    async def get_athlete(self) -> JSONObject:
        """``GET /api/v1/athlete/{id}`` — athlete with ``sportSettings``.

        The raw object contains secrets (e.g. ``icu_api_key``); callers must
        sanitise it before storing or logging it.
        """
        return await self._get_object(f"/athlete/{self.athlete_id}")

    async def list_activities(
        self,
        oldest: dt.date | dt.datetime,
        newest: dt.date | dt.datetime | None = None,
        *,
        limit: int | None = None,
    ) -> list[JSONObject]:
        """``GET /api/v1/athlete/{id}/activities`` — newest first.

        ``oldest``/``newest`` filter on the activity's *local* start date.
        Activities the API cannot share (e.g. Strava-sourced) come back as
        "Hidden" stubs with only ``id``, ``start_date_local``, ``source`` and
        ``_note``.
        """
        params: dict[str, str] = {"oldest": _iso(oldest)}
        if newest is not None:
            params["newest"] = _iso(newest)
        if limit is not None:
            params["limit"] = str(limit)
        return await self._get_list(f"/athlete/{self.athlete_id}/activities", params)

    async def get_activity(self, activity_id: str, *, intervals: bool = False) -> JSONObject:
        """``GET /api/v1/activity/{id}``."""
        params = {"intervals": "true"} if intervals else None
        return await self._get_object(f"/activity/{activity_id}", params)

    async def get_activity_streams(
        self, activity_id: str, types: Sequence[str] = ("time", "watts", "heartrate")
    ) -> dict[str, list[Any]]:
        """``GET /api/v1/activity/{id}/streams`` (JSON; ``{ext}`` left empty).

        The OpenAPI schema returns a list of ``ActivityStream`` objects
        (``type``, ``data``, …). They are returned here as ``{type: data}``.
        Streams that are missing, all-null, or not a list are left out.
        ``types`` is an array query parameter, sent comma-separated. The
        response shape is **not yet verified on a real activity** (the account
        had none in Phase 7), so parsing is deliberately tolerant.
        """
        body = await self._request(
            "GET", f"/activity/{activity_id}/streams", {"types": ",".join(types)}
        )
        if not isinstance(body, list):
            raise IntervalsResponseError(
                f"expected a list of streams, got {type(body).__name__}", status_code=200
            )
        streams: dict[str, list[Any]] = {}
        for item in body:
            if not isinstance(item, dict) or item.get("allNull") is True:
                continue
            kind, data = item.get("type"), item.get("data")
            if isinstance(kind, str) and isinstance(data, list):
                streams[kind] = data
        return streams

    async def list_wellness(self, oldest: dt.date, newest: dt.date) -> list[JSONObject]:
        """``GET /api/v1/athlete/{id}/wellness`` (JSON; ``{ext}`` left empty).

        ``newest`` is inclusive. Each record's ``id`` is its local ISO date.
        """
        params = {"oldest": _iso(oldest), "newest": _iso(newest)}
        return await self._get_list(f"/athlete/{self.athlete_id}/wellness", params)

    async def list_events(
        self,
        oldest: dt.date,
        newest: dt.date,
        *,
        categories: list[str] | None = None,
        resolve: bool = False,
    ) -> list[JSONObject]:
        """``GET /api/v1/athlete/{id}/events`` — calendar (workouts, notes, …).

        ``newest`` is inclusive. Without it the API defaults to oldest + 6 days,
        so both bounds are always sent.
        """
        params = {"oldest": _iso(oldest), "newest": _iso(newest)}
        if categories:
            params["category"] = ",".join(categories)
        if resolve:
            params["resolve"] = "true"
        return await self._get_list(f"/athlete/{self.athlete_id}/events", params)

    async def list_planned_workouts(self, oldest: dt.date, newest: dt.date) -> list[JSONObject]:
        """Calendar events of category ``WORKOUT`` (planned workouts)."""
        return await self.list_events(oldest, newest, categories=["WORKOUT"])

    async def get_event(self, event_id: int) -> JSONObject:
        """``GET /api/v1/athlete/{id}/events/{eventId}`` (includes ``workout_doc``)."""
        return await self._get_object(f"/athlete/{self.athlete_id}/events/{event_id}")

    async def upsert_events(self, events: list[JSONObject]) -> list[JSONObject]:
        """``POST /api/v1/athlete/{id}/events/bulk?upsert=true``.

        Events with an ``external_id`` that already exists are updated instead
        of duplicated. Returns the created or updated events.
        """
        body = await self._request(
            "POST",
            f"/athlete/{self.athlete_id}/events/bulk",
            {"upsert": "true"},
            json_body=events,
        )
        if not isinstance(body, list) or not all(isinstance(e, dict) for e in body):
            raise IntervalsResponseError("Expected a JSON array of events from events/bulk")
        return cast(list[JSONObject], body)

    async def delete_event(self, event_id: int) -> None:
        """``DELETE /api/v1/athlete/{id}/events/{eventId}`` (this event only)."""
        await self._request("DELETE", f"/athlete/{self.athlete_id}/events/{event_id}")

    # --- transport ----------------------------------------------------------

    async def _get_object(self, path: str, params: Mapping[str, str] | None = None) -> JSONObject:
        body = await self._request("GET", path, params)
        if not isinstance(body, dict):
            raise IntervalsResponseError(
                f"Expected a JSON object from {path}, got {type(body).__name__}", path=path
            )
        return cast(JSONObject, body)

    async def _get_list(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> list[JSONObject]:
        body = await self._request("GET", path, params)
        if not isinstance(body, list) or not all(isinstance(item, dict) for item in body):
            raise IntervalsResponseError(f"Expected a JSON array of objects from {path}", path=path)
        return cast(list[JSONObject], body)

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None and response.status_code in (429, 503):
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                return min(retry_after, self._backoff_max)
        ceiling = min(self._backoff_max, self._backoff_base * (2**attempt))
        return random.uniform(0, ceiling)  # noqa: S311 - jitter, not crypto

    async def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        json_body: Any = None,
    ) -> Any:
        url = API_PREFIX + path
        attempt = 0
        while True:
            started = time.perf_counter()
            response: httpx.Response | None = None
            error: Exception | None = None
            try:
                response = await self._http.request(method, url, params=params, json=json_body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error = exc
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

            retryable = error is not None or (
                response is not None and response.status_code in RETRYABLE_STATUS
            )
            if retryable and attempt < self._max_retries:
                delay = self._backoff(attempt, response)
                logger.warning(
                    "intervals_request_retry",
                    extra={
                        "method": method,
                        "path": url,
                        "status_code": response.status_code if response else None,
                        "error": repr(error) if error else None,
                        "attempt": attempt + 1,
                        "max_retries": self._max_retries,
                        "retry_in_s": round(delay, 2),
                        "duration_ms": elapsed_ms,
                    },
                )
                await self._sleep(delay)
                attempt += 1
                continue

            if error is not None:
                logger.error(
                    "intervals_request_failed",
                    extra={
                        "method": method,
                        "path": url,
                        "error": repr(error),
                        "attempts": attempt + 1,
                        "duration_ms": elapsed_ms,
                    },
                )
                raise IntervalsTransportError(
                    f"{method} {url} failed after {attempt + 1} attempt(s): {error!r}", path=url
                ) from error

            assert response is not None
            status = response.status_code
            log_extra = {
                "method": method,
                "path": url,
                "status_code": status,
                "attempts": attempt + 1,
                "duration_ms": elapsed_ms,
            }
            if 200 <= status < 300:
                logger.debug("intervals_request", extra=log_extra)
                if not response.content:
                    return None
                try:
                    return response.json()
                except ValueError as exc:
                    raise IntervalsResponseError(
                        f"{method} {url} returned non-JSON body", status_code=status, path=url
                    ) from exc

            detail = response.text[:_MAX_ERROR_BODY]
            logger.error("intervals_request_failed", extra={**log_extra, "detail": detail})
            message = f"{method} {url} -> HTTP {status}: {detail}"
            error_cls: type[IntervalsError]
            if status in (401, 403):
                error_cls = IntervalsAuthError
            elif status == 404:
                error_cls = IntervalsNotFoundError
            elif status == 429:
                error_cls = IntervalsRateLimitError
            elif status >= 500:
                error_cls = IntervalsServerError
            else:
                error_cls = IntervalsResponseError
            raise error_cls(message, status_code=status, path=url)
