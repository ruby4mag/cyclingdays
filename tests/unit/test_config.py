from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.config import AthleteSeedSettings, Settings
from tests.conftest import make_unit_settings


def test_postgres_password_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(ValidationError, match="postgres_password"):
        Settings(_env_file=None)


def test_sqlalchemy_url_uses_psycopg_and_escapes_credentials() -> None:
    settings = make_unit_settings(postgres_password="p@ss:w/rd", postgres_port=5434)
    url = settings.sqlalchemy_url()
    assert url.startswith("postgresql+psycopg://coach:")
    assert "p%40ss%3Aw%2Frd@127.0.0.1:5434/cycling_coach" in url


def test_sqlalchemy_url_database_override() -> None:
    assert make_unit_settings().sqlalchemy_url(database="other").endswith("/other")


def test_database_url_overrides_components() -> None:
    settings = make_unit_settings(database_url="postgresql+psycopg://u:p@h:1/d")
    assert settings.sqlalchemy_url() == "postgresql+psycopg://u:p@h:1/d"


def test_secrets_are_not_rendered() -> None:
    settings = make_unit_settings(postgres_password="super-secret-value")
    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings.model_dump())


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        make_unit_settings(app_timezone="Mars/Olympus")


def test_athlete_seed_defaults_match_initial_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("ATHLETE_"):
            monkeypatch.delenv(key)
    seed = AthleteSeedSettings(_env_file=None)
    assert seed.ftp_watts == 175
    assert seed.weight_kg == 86.0
    assert seed.height_cm == 172.0
    assert seed.age_years == 46
    assert seed.experience_level == "intermediate"
    assert seed.timezone == "Asia/Kolkata"
    assert seed.indoor_platform == "icTrainer"
    assert seed.wearable == "COROS"


def test_athlete_seed_rejects_invalid_ftp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHLETE_FTP_WATTS", "0")
    with pytest.raises(ValidationError, match="ftp_watts"):
        AthleteSeedSettings(_env_file=None)
