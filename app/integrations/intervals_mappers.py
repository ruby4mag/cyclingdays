"""Map Intervals.icu JSON objects to column values of our tables.

Pure functions, no I/O. Field names come from the official OpenAPI schemas
(``Activity``, ``Wellness``, ``Event``) and were checked against real
responses; see ``docs/INTERVALS_ICU.md`` for the mapping table and units.

Every mapper keeps the full (secret-redacted) source object in ``raw_payload``
so additional fields can be derived later without re-fetching.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.logging_config import redact_secrets

SOURCE = "intervals_icu"

JSONObject = dict[str, Any]


class MappingError(ValueError):
    """The source object lacks a field we cannot do without."""


# --------------------------------------------------------------------------
# Scalar helpers (tolerant: wrong types become None instead of crashing a sync)
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else round(number)


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _id_text(value: Any) -> str | None:
    """Numeric or string ids → text; 0/blank → None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value) if value else None
    return _text(value) if isinstance(value, str) else None


def _positive(value: float | None) -> float | None:
    """0 means "not recorded" for physiological fields in Intervals.icu."""
    return value if value is not None and value > 0 else None


def parse_local_datetime(value: Any) -> dt.datetime | None:
    """``'2026-11-04T06:30:00'`` → naive datetime (athlete-local wall time)."""
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(text), dt.time())
        except ValueError:
            return None
    return parsed.replace(tzinfo=None)


def parse_local_date(value: Any) -> dt.date | None:
    parsed = parse_local_datetime(value)
    return parsed.date() if parsed else None


def parse_utc_datetime(value: Any) -> dt.datetime | None:
    """``'2026-11-04T01:00:00Z'`` → aware UTC datetime. Naive input is taken as UTC."""
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def sanitize(payload: JSONObject) -> JSONObject:
    """Remove secrets (e.g. ``icu_api_key``) before a payload is stored."""
    cleaned = redact_secrets(payload)
    assert isinstance(cleaned, dict)
    return cleaned


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------


def is_hidden_activity(payload: JSONObject) -> bool:
    """Activities the API will not share come back as the ``Hidden`` schema
    (``id``, ``icu_athlete_id``, ``start_date_local``, ``source``, ``_note``),
    e.g. activities imported from Strava."""
    return "_note" in payload and "start_date" not in payload


def activity_values(payload: JSONObject) -> JSONObject:
    """Column values for ``coach.activities`` (without ``athlete_id``)."""
    source_id = _text(str(payload.get("id"))) if payload.get("id") is not None else None
    start_local = parse_local_datetime(payload.get("start_date_local"))
    start_utc = parse_utc_datetime(payload.get("start_date"))
    if source_id is None or start_local is None:
        raise MappingError("activity without id or start_date_local")
    if start_utc is None:
        raise MappingError(f"activity {source_id} without start_date")

    distance = _num(payload.get("distance"))
    if distance is None:
        distance = _num(payload.get("icu_distance"))
    intensity_pct = _num(payload.get("icu_intensity"))
    rpe = _num(payload.get("icu_rpe"))
    if rpe is None:
        rpe = _num(payload.get("perceived_exertion"))

    return {
        "source": SOURCE,
        "source_activity_id": source_id,
        "start_time": start_utc,
        "start_date_local": start_local.date(),
        "activity_type": _text(payload.get("type")),
        "name": _text(payload.get("name")),
        "is_indoor": _bool(payload.get("trainer")),
        "moving_time_s": _int(payload.get("moving_time")),
        "elapsed_time_s": _int(payload.get("elapsed_time")),
        "distance_m": distance,
        "elevation_gain_m": _num(payload.get("total_elevation_gain")),
        "avg_power_watts": _positive(_num(payload.get("icu_average_watts"))),
        # Intervals.icu "weighted average watts" is its Normalized-Power equivalent
        # (30 s rolling average, 4th-power mean).
        "normalized_power_watts": _positive(_num(payload.get("icu_weighted_avg_watts"))),
        "avg_hr_bpm": _positive(_num(payload.get("average_heartrate"))),
        "max_hr_bpm": _positive(_num(payload.get("max_heartrate"))),
        "ftp_at_activity_watts": _int(payload.get("icu_ftp")),
        "source_training_load": _num(payload.get("icu_training_load")),
        # icu_intensity is a percentage (e.g. 72.4); our column is a ratio (0.724).
        "source_intensity_factor": None if intensity_pct is None else intensity_pct / 100.0,
        "rpe": rpe,
        "feel": _int(payload.get("feel")),
        # Provenance: which platform delivered the activity to Intervals.icu.
        "origin": _text(payload.get("source")),
        "origin_client": _text(payload.get("oauth_client_name")),
        "device_name": _text(payload.get("device_name")),
        "origin_external_id": _text(payload.get("external_id")),
        "paired_event_id": _id_text(payload.get("paired_event_id")),
        "raw_payload": sanitize(payload),
    }


# --------------------------------------------------------------------------
# Wellness
# --------------------------------------------------------------------------


def wellness_values(payload: JSONObject) -> JSONObject:
    """Column values for ``coach.daily_recovery`` (without ``athlete_id``).

    Subjective fields (``fatigue``, ``soreness``, ``stress``) are stored on the
    source's own scale; interpretation is deterministic logic in Phase 3.
    ``injury`` is stored as-is and interpreted by the readiness rules
    (``app/training/readiness.py``). ``illness_reported``/``pain_reported``
    are explicit athlete reports and are not derived from Intervals.icu,
    which has no illness field.
    """
    day = parse_local_date(payload.get("id"))
    if day is None:
        raise MappingError("wellness record without a date id")
    return {
        "source": SOURCE,
        "recovery_date": day,
        "resting_hr_bpm": _positive(_num(payload.get("restingHR"))),
        # "hrv" is rMSSD; SDNN is reported separately as "hrvSDNN".
        "hrv_rmssd_ms": _positive(_num(payload.get("hrv"))),
        "sleep_duration_s": _int(_positive(_num(payload.get("sleepSecs")))),
        "sleep_score": _num(payload.get("sleepScore")),
        "subjective_fatigue": _int(payload.get("fatigue")),
        "subjective_soreness": _int(payload.get("soreness")),
        "subjective_stress": _int(payload.get("stress")),
        "subjective_injury": _int(payload.get("injury")),
        "notes": _text(payload.get("comments")),
        "raw_payload": sanitize(payload),
    }


def wellness_has_data(payload: JSONObject) -> bool:
    """True if the record carries a recorded value.

    Ignored: the id, computed load fields (``ctl``/``atl``/``rampRate``/…,
    present for every day) and boolean flags (``tempWeight``,
    ``tempRestingHR``, ``locked``), which real responses include with
    ``false`` on every record.
    """
    computed = {"id", "ctl", "atl", "rampRate", "ctlLoad", "atlLoad", "sportInfo", "updated"}
    return any(
        v not in (None, [], {}, "") and not isinstance(v, bool)
        for k, v in payload.items()
        if k not in computed
    )


# --------------------------------------------------------------------------
# Calendar events
# --------------------------------------------------------------------------


def event_values(payload: JSONObject) -> JSONObject:
    """Column values for ``coach.calendar_events`` (without ``athlete_id``)."""
    raw_id = payload.get("id")
    start = parse_local_datetime(payload.get("start_date_local"))
    category = _text(payload.get("category"))
    if raw_id is None or start is None or category is None:
        raise MappingError("event without id, start_date_local or category")
    return {
        "source": SOURCE,
        "source_event_id": str(raw_id),
        "category": category,
        "event_type": _text(payload.get("type")),
        "start_date_local": start.date(),
        "start_time_local": start,
        "end_date_local": parse_local_date(payload.get("end_date_local")),
        "name": _text(payload.get("name")),
        "description": _text(payload.get("description")),
        "external_id": _text(payload.get("external_id")),
        "is_indoor": _bool(payload.get("indoor")),
        "planned_duration_s": _int(payload.get("moving_time")),
        "planned_training_load": _num(payload.get("icu_training_load")),
        "source_updated_at": parse_utc_datetime(payload.get("updated")),
        "deleted_at": None,
        "raw_payload": sanitize(payload),
    }


# --------------------------------------------------------------------------
# Athlete
# --------------------------------------------------------------------------


# Personal data in the athlete object that the coach does not need.
_ATHLETE_DROP_KEYS = frozenset({"email"})


def athlete_snapshot(athlete: JSONObject) -> JSONObject:
    """Athlete object as stored: secrets redacted, unneeded personal data dropped."""
    return sanitize({k: v for k, v in athlete.items() if k not in _ATHLETE_DROP_KEYS})


def cycling_ftp(athlete: JSONObject) -> int | None:
    """FTP from the sport settings that cover ``Ride`` (None if absent)."""
    for settings in athlete.get("sportSettings") or []:
        if isinstance(settings, dict) and "Ride" in (settings.get("types") or []):
            return _int(settings.get("ftp"))
    return None


def athlete_summary(athlete: JSONObject) -> JSONObject:
    """Fields compared with our profile; reported, never auto-applied."""
    return {
        "intervals_athlete_id": _text(str(athlete.get("id"))) if athlete.get("id") else None,
        "name": _text(athlete.get("name")),
        "timezone": _text(athlete.get("timezone")),
        "weight_kg": _num(athlete.get("icu_weight")) or _num(athlete.get("weight")),
        "resting_hr_bpm": _num(athlete.get("icu_resting_hr")),
        "ride_ftp_watts": cycling_ftp(athlete),
    }
