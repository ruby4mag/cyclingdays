from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import AthleteSeedSettings, Settings
from app.main import create_app
from scripts.seed_athlete_profile import seed

pytestmark = pytest.mark.integration


def test_readiness_ok_against_migrated_database(
    integration_settings: Settings, test_database_url: str
) -> None:
    test_db_name = test_database_url.rsplit("/", 1)[1]
    settings = integration_settings.model_copy(update={"postgres_db": test_db_name})
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["migrations"]["data"] == {"applied": "0006", "expected": "0006"}


async def test_seed_athlete_profile_is_idempotent(
    integration_settings: Settings, test_database_url: str, engine: AsyncEngine
) -> None:
    seed_values = AthleteSeedSettings(_env_file=None)
    first = await seed(integration_settings, seed_values, database_url=test_database_url)
    second = await seed(integration_settings, seed_values, database_url=test_database_url)
    assert first["created"] is True
    assert second == {**first, "created": False}

    async with engine.connect() as conn:
        rows = (
            await conn.exec_driver_sql(
                "SELECT ftp_watts, weight_kg, timezone, ftp_updated_at IS NOT NULL "
                "FROM coach.athlete_profile"
            )
        ).all()
    assert [tuple(r) for r in rows] == [(175, 86.0, "Asia/Kolkata", True)]
