"""Phase 10: per-athlete Intervals.icu credentials (no database)."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import pytest
from cryptography.fernet import Fernet

from app.integrations.intervals_client import IntervalsClient, IntervalsServerError
from app.services import connections
from app.services.connections import CredentialsKeyError, InvalidCredentialsError
from app.services.training_plans import check_week_template
from app.training.plan_builder import DEFAULT_TEMPLATE, PlanBuildError
from tests.conftest import make_unit_settings

KEY = Fernet.generate_key().decode()


def test_api_keys_are_encrypted_and_round_trip() -> None:
    settings = make_unit_settings(credentials_encryption_key=KEY)
    token = connections.encrypt_key(settings, "secret-api-key")
    assert "secret-api-key" not in token
    assert connections.decrypt_key(settings, token) == "secret-api-key"
    other = make_unit_settings(credentials_encryption_key=Fernet.generate_key().decode())
    with pytest.raises(CredentialsKeyError, match="decrypted"):
        connections.decrypt_key(other, token)


def test_missing_or_malformed_encryption_key_is_refused() -> None:
    with pytest.raises(CredentialsKeyError, match="not set"):
        connections.cipher(make_unit_settings())
    with pytest.raises(CredentialsKeyError, match="not a valid"):
        connections.cipher(make_unit_settings(credentials_encryption_key="too-short"))
    assert make_unit_settings(credentials_encryption_key="  ").credentials_encryption_key is None


def test_input_normalisation() -> None:
    assert connections.normalize_athlete_id(" i123 ") == "i123"
    assert connections.normalize_api_key(" abc123 ") == "abc123"
    for bad in ("", "i 1", "i1;drop", "x" * 40):
        with pytest.raises(InvalidCredentialsError):
            connections.normalize_athlete_id(bad)
    for bad in ("", "has space", "tab\tkey"):
        with pytest.raises(InvalidCredentialsError):
            connections.normalize_api_key(bad)


def _client(status: int, body: dict[str, Any] | None = None) -> IntervalsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/athlete/i42"
        return httpx.Response(status, json=body or {})

    return IntervalsClient(
        api_key="k", athlete_id="i42", max_retries=0, transport=httpx.MockTransport(handler)
    )


async def test_verify_credentials() -> None:
    ok = {"id": "i42", "name": "Anna", "sportSettings": [{"types": ["Ride"], "ftp": 230}]}
    async with _client(200, ok) as client:
        _, summary = await connections.verify_credentials(client)
    assert summary["ride_ftp_watts"] == 230 and summary["name"] == "Anna"
    for status, match in ((401, "refused"), (403, "refused"), (404, "no athlete")):
        async with _client(status) as client:
            with pytest.raises(InvalidCredentialsError, match=match):
                await connections.verify_credentials(client)
    async with _client(200, {"id": "i99"}) as client:
        with pytest.raises(InvalidCredentialsError, match="belongs to"):
            await connections.verify_credentials(client)
    async with _client(500) as client:
        with pytest.raises(IntervalsServerError):
            await connections.verify_credentials(client)


def test_week_template_rules() -> None:
    check_week_template(DEFAULT_TEMPLATE)
    week = dict(DEFAULT_TEMPLATE)
    bad: list[tuple[dict[str, str], str]] = [
        ({**week, "mon": "endurance"}, "rest day"),
        ({**week, "wed": "key", "fri": "key"}, "two key"),
        ({**week, "wed": "key", "thu": "endurance"}, "in a row"),
        ({**week, "sun": "long", "wed": "long"}, "two long"),
        ({**week, "sun": "long"}, "three key and long"),
        ({k: v for k, v in week.items() if k != "sun"}, "every day"),
        ({**week, "sun": "sprint"}, "every day"),
    ]
    for template, match in bad:
        with pytest.raises(PlanBuildError, match=match):
            check_week_template(template)
