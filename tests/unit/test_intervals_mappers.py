"""Mapping of Intervals.icu objects to table columns.

Fixtures are synthetic but use only field names and types from the official
OpenAPI schemas (Activity, Wellness, Event, WithSportSettings, Hidden).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.integrations import intervals_mappers as m
from app.logging_config import REDACTED

ACTIVITY: dict[str, Any] = {
    "id": "i1001",
    "start_date_local": "2026-11-04T06:30:00",
    "start_date": "2026-11-04T01:00:00Z",
    "type": "VirtualRide",
    "name": "Z2 endurance",
    "trainer": True,
    "moving_time": 3600,
    "elapsed_time": 3700,
    "distance": 30000.0,
    "total_elevation_gain": 0.0,
    "icu_average_watts": 130,
    "icu_weighted_avg_watts": 135,
    "average_heartrate": 128,
    "max_heartrate": 150,
    "icu_ftp": 175,
    "icu_training_load": 60,
    "icu_intensity": 77.1,
    "icu_rpe": 4,
    "feel": 2,
}


def test_activity_values() -> None:
    v = m.activity_values(ACTIVITY)
    assert v["source"] == "intervals_icu"
    assert v["source_activity_id"] == "i1001"
    assert v["start_time"] == dt.datetime(2026, 11, 4, 1, 0, tzinfo=dt.UTC)
    assert v["start_date_local"] == dt.date(2026, 11, 4)
    assert v["is_indoor"] is True
    assert v["normalized_power_watts"] == 135.0
    assert v["source_intensity_factor"] == pytest.approx(0.771)
    assert v["ftp_at_activity_watts"] == 175
    assert v["rpe"] == 4.0
    assert v["raw_payload"] == ACTIVITY


def test_activity_missing_optional_fields_become_none() -> None:
    minimal = {k: ACTIVITY[k] for k in ("id", "start_date_local", "start_date")}
    minimal["icu_average_watts"] = 0  # 0 = no power recorded
    minimal["average_heartrate"] = "n/a"  # wrong type must not crash
    v = m.activity_values(minimal)
    assert v["avg_power_watts"] is None
    assert v["avg_hr_bpm"] is None
    assert v["source_intensity_factor"] is None
    assert v["rpe"] is None


def test_activity_rpe_falls_back_to_perceived_exertion() -> None:
    payload = {**ACTIVITY, "icu_rpe": None, "perceived_exertion": 6.0}
    assert m.activity_values(payload)["rpe"] == 6.0


def test_activity_without_required_fields() -> None:
    with pytest.raises(m.MappingError):
        m.activity_values({"id": "i1"})
    with pytest.raises(m.MappingError):
        m.activity_values({"id": "i1", "start_date_local": "2026-11-04T06:30:00"})


def test_hidden_activity_detected() -> None:
    hidden = {
        "id": "123",
        "icu_athlete_id": "i42",
        "start_date_local": "2026-11-01T07:00:00",
        "source": "STRAVA",
        "_note": "STRAVA activities are not available via the API",
    }
    assert m.is_hidden_activity(hidden)
    assert not m.is_hidden_activity(ACTIVITY)


def test_wellness_values() -> None:
    payload = {
        "id": "2026-11-04",
        "ctl": 40.1,
        "atl": 45.2,
        "restingHR": 52,
        "hrv": 48.0,
        "hrvSDNN": 60.0,
        "sleepSecs": 25200,
        "sleepScore": 80.0,
        "fatigue": 2,
        "soreness": 1,
        "stress": 3,
        "comments": " slept badly ",
    }
    v = m.wellness_values(payload)
    assert v["recovery_date"] == dt.date(2026, 11, 4)
    assert v["resting_hr_bpm"] == 52.0
    assert v["hrv_rmssd_ms"] == 48.0
    assert v["sleep_duration_s"] == 25200
    assert v["subjective_fatigue"] == 2
    assert v["notes"] == "slept badly"
    assert "illness_reported" not in v and "pain_reported" not in v
    assert m.wellness_has_data(payload)


def test_wellness_with_only_computed_fields_has_no_data() -> None:
    computed_only = {
        "id": "2026-11-04",
        "ctl": 40.1,
        "atl": 45.2,
        "rampRate": 1.0,
        "ctlLoad": 50.0,
        "atlLoad": 50.0,
        "sportInfo": [],
        "updated": None,
        "weight": None,
    }
    assert not m.wellness_has_data(computed_only)
    # Real responses carry boolean flags on every record; they are not data.
    flags_only = {**computed_only, "tempWeight": False, "tempRestingHR": False, "locked": None}
    assert not m.wellness_has_data(flags_only)
    assert m.wellness_has_data({**flags_only, "steps": 2236})
    with pytest.raises(m.MappingError):
        m.wellness_values({"ctl": 1.0})


def test_event_values() -> None:
    payload = {
        "id": 777,
        "category": "WORKOUT",
        "type": "Ride",
        "start_date_local": "2026-11-06T00:00:00",
        "name": "Sweet spot 3x10",
        "external_id": "coach-1-2026-11-06",
        "indoor": True,
        "moving_time": 3600,
        "icu_training_load": 70,
        "updated": "2026-11-05T03:00:00Z",
    }
    v = m.event_values(payload)
    assert v["source_event_id"] == "777"
    assert v["start_date_local"] == dt.date(2026, 11, 6)
    assert v["start_time_local"] == dt.datetime(2026, 11, 6)
    assert v["external_id"] == "coach-1-2026-11-06"
    assert v["source_updated_at"] == dt.datetime(2026, 11, 5, 3, tzinfo=dt.UTC)
    assert v["deleted_at"] is None
    with pytest.raises(m.MappingError):
        m.event_values({"id": 1, "start_date_local": "2026-11-06T00:00:00"})


def test_athlete_secrets_are_redacted_and_summary() -> None:
    athlete = {
        "id": "i42",
        "name": "Test Rider",
        "timezone": "Asia/Kolkata",
        "icu_weight": 85.5,
        "icu_api_key": "do-not-store",
        "icu_friend_invite_token": "do-not-store",
        "sportSettings": [
            {"types": ["Run"], "ftp": None},
            {"types": ["Ride", "VirtualRide"], "ftp": 180},
        ],
    }
    athlete["email"] = "rider@example.com"
    clean = m.athlete_snapshot(athlete)
    assert "email" not in clean
    assert clean["icu_api_key"] == REDACTED
    assert clean["icu_friend_invite_token"] == REDACTED
    assert athlete["icu_api_key"] == "do-not-store"  # input untouched
    summary = m.athlete_summary(athlete)
    assert summary == {
        "intervals_athlete_id": "i42",
        "name": "Test Rider",
        "timezone": "Asia/Kolkata",
        "weight_kg": 85.5,
        "resting_hr_bpm": None,
        "ride_ftp_watts": 180,
    }


def test_date_parsers() -> None:
    assert m.parse_local_date("2026-11-04") == dt.date(2026, 11, 4)
    assert m.parse_local_datetime("garbage") is None
    assert m.parse_utc_datetime("2026-11-04T06:30:00+05:30") == dt.datetime(
        2026, 11, 4, 1, 0, tzinfo=dt.UTC
    )
    assert m.parse_utc_datetime("2026-11-04T01:00:00") == dt.datetime(
        2026, 11, 4, 1, 0, tzinfo=dt.UTC
    )
