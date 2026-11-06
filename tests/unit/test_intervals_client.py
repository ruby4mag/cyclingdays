"""IntervalsClient behaviour against a mock transport (no network)."""

from __future__ import annotations

import base64
import datetime as dt
import json
from collections.abc import Callable
from typing import Any

import httpx2 as httpx
import pytest

from app.integrations.intervals_client import (
    IntervalsAuthError,
    IntervalsClient,
    IntervalsConfigError,
    IntervalsNotFoundError,
    IntervalsRateLimitError,
    IntervalsResponseError,
    IntervalsServerError,
    IntervalsTransportError,
)
from tests.conftest import make_unit_settings

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers or {})


def make_client(handler: Handler, sleeps: list[float], **kwargs: Any) -> IntervalsClient:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    values: dict[str, Any] = {
        "api_key": "secret-key",
        "athlete_id": "i42",
        "max_retries": 3,
        "backoff_base_seconds": 1.0,
        "backoff_max_seconds": 8.0,
    }
    values.update(kwargs)
    return IntervalsClient(transport=httpx.MockTransport(handler), sleep=fake_sleep, **values)


async def test_basic_auth_and_athlete_path() -> None:
    rec = Recorder(_json(200, {"id": "i42"}))
    async with make_client(rec, []) as client:
        assert await client.get_athlete() == {"id": "i42"}
    req = rec.requests[0]
    assert req.url.path == "/api/v1/athlete/i42"
    expected = base64.b64encode(b"API_KEY:secret-key").decode()
    assert req.headers["Authorization"] == f"Basic {expected}"


async def test_list_endpoints_send_documented_params() -> None:
    rec = Recorder(_json(200, []))
    async with make_client(rec, []) as client:
        await client.list_activities(dt.date(2026, 8, 1))
        await client.list_wellness(dt.date(2026, 8, 1), dt.date(2026, 11, 5))
        await client.list_planned_workouts(dt.date(2026, 11, 1), dt.date(2026, 11, 30))
    acts, well, events = rec.requests
    assert acts.url.path == "/api/v1/athlete/i42/activities"
    assert dict(acts.url.params) == {"oldest": "2026-08-01"}  # newest omitted -> "now"
    assert well.url.path == "/api/v1/athlete/i42/wellness"
    assert dict(well.url.params) == {"oldest": "2026-08-01", "newest": "2026-11-05"}
    assert events.url.path == "/api/v1/athlete/i42/events"
    assert dict(events.url.params) == {
        "oldest": "2026-11-01",
        "newest": "2026-11-30",
        "category": "WORKOUT",
    }


async def test_aware_datetime_rejected() -> None:
    async with make_client(Recorder(_json(200, [])), []) as client:
        with pytest.raises(ValueError, match="naive local"):
            await client.list_activities(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))


async def test_retries_429_honouring_retry_after() -> None:
    rec = Recorder(_json(429, {}, {"Retry-After": "3"}), _json(200, {"id": "i42"}))
    sleeps: list[float] = []
    async with make_client(rec, sleeps) as client:
        assert await client.get_athlete() == {"id": "i42"}
    assert sleeps == [3.0]
    assert len(rec.requests) == 2


async def test_retry_after_is_capped() -> None:
    rec = Recorder(_json(429, {}, {"Retry-After": "3600"}), _json(200, {}))
    sleeps: list[float] = []
    async with make_client(rec, sleeps) as client:
        await client.get_athlete()
    assert sleeps == [8.0]


async def test_rate_limit_exhausted_raises() -> None:
    rec = Recorder(_json(429, {"error": "slow down"}))
    sleeps: list[float] = []
    async with make_client(rec, sleeps) as client:
        with pytest.raises(IntervalsRateLimitError) as info:
            await client.get_athlete()
    assert info.value.status_code == 429
    assert len(rec.requests) == 4  # 1 + 3 retries
    assert len(sleeps) == 3
    assert all(0 <= s <= 8.0 for s in sleeps)


async def test_server_error_retried_then_success() -> None:
    rec = Recorder(_json(502, {}), _json(503, {}), _json(200, []))
    async with make_client(rec, []) as client:
        assert await client.list_wellness(dt.date(2026, 1, 1), dt.date(2026, 1, 2)) == []
    assert len(rec.requests) == 3


async def test_server_error_exhausted() -> None:
    async with make_client(Recorder(_json(500, {})), [], max_retries=1) as client:
        with pytest.raises(IntervalsServerError):
            await client.get_athlete()


async def test_transport_error_retried_then_raises() -> None:
    rec = Recorder(httpx.ConnectError("boom"))
    async with make_client(rec, [], max_retries=2) as client:
        with pytest.raises(IntervalsTransportError):
            await client.get_athlete()
    assert len(rec.requests) == 3


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, IntervalsAuthError),
        (403, IntervalsAuthError),
        (404, IntervalsNotFoundError),
        (400, IntervalsResponseError),
    ],
)
async def test_client_errors_not_retried(status: int, error: type[Exception]) -> None:
    rec = Recorder(_json(status, {"error": "nope"}))
    async with make_client(rec, []) as client:
        with pytest.raises(error):
            await client.get_athlete()
    assert len(rec.requests) == 1


async def test_unexpected_shapes_rejected() -> None:
    async with make_client(Recorder(_json(200, [1, 2])), []) as client:
        with pytest.raises(IntervalsResponseError, match="array of objects"):
            await client.list_activities(dt.date(2026, 1, 1))
    async with make_client(Recorder(_json(200, [])), []) as client:
        with pytest.raises(IntervalsResponseError, match="JSON object"):
            await client.get_athlete()
    bad = httpx.Response(200, content=b"<html>")
    async with make_client(Recorder(bad), []) as client:
        with pytest.raises(IntervalsResponseError, match="non-JSON"):
            await client.get_athlete()


async def test_api_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    rec = Recorder(_json(401, {"error": "bad key"}))
    async with make_client(rec, []) as client:
        with pytest.raises(IntervalsAuthError):
            await client.get_athlete()
    assert "secret-key" not in caplog.text
    assert any(r.getMessage() == "intervals_request_failed" for r in caplog.records)


def test_from_settings_requires_configuration() -> None:
    with pytest.raises(IntervalsConfigError, match="INTERVALS_API_KEY"):
        IntervalsClient.from_settings(make_unit_settings())


def test_settings_blank_values_mean_unconfigured() -> None:
    settings = make_unit_settings(intervals_api_key="  ", intervals_athlete_id="")
    assert settings.intervals_api_key is None
    assert settings.intervals_athlete_id is None
    assert not settings.intervals_configured
    configured = make_unit_settings(intervals_api_key="k", intervals_athlete_id=" i42 ")
    assert configured.intervals_athlete_id == "i42"
    assert configured.intervals_configured
    assert "k" not in repr(configured.intervals_api_key)


def test_settings_reject_bad_athlete_id_and_short_history() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        make_unit_settings(intervals_athlete_id="i42/../x")
    with pytest.raises(ValueError):
        make_unit_settings(intervals_history_days=10)
