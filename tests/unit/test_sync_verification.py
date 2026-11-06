"""Phase 6: duplicate-recording detection, activity provenance, sync checks."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from app.integrations.intervals_mappers import activity_values
from app.services.sync_verification import (
    CHECKS,
    ActivityRef,
    Confirmation,
    PublishedRef,
    WellnessRef,
    evaluate,
)
from app.training.activity_dedup import Recording, find_duplicates, same_session, sport_family

T0 = dt.datetime(2026, 11, 6, 1, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 11, 6)


def rec(i: int, start_min: int, minutes: int, **kw: Any) -> Recording:
    values: dict[str, Any] = {
        "id": i,
        "start": T0 + dt.timedelta(minutes=start_min),
        "duration_s": minutes * 60,
        "activity_type": "VirtualRide",
        "has_power": True,
        "paired": False,
        "moving_s": minutes * 60,
    }
    values.update(kw)
    return Recording(**values)


# --------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------


def test_sport_family() -> None:
    assert sport_family("VirtualRide") == sport_family("Ride") == "cycling"
    assert sport_family("GravelRide") == "cycling"
    assert sport_family("Run") == "Run"
    assert sport_family(None) == "unknown"


def test_trainer_app_and_watch_recording_of_one_ride_are_duplicates() -> None:
    trainer = rec(1, 0, 30, activity_type="VirtualRide", has_power=True, paired=True)
    watch = rec(2, -1, 32, activity_type="Ride", has_power=False)
    assert same_session(trainer, watch)
    assert find_duplicates([watch, trainer]) == {2: 1}  # the power file is kept


def test_short_overlap_and_other_sports_are_not_duplicates() -> None:
    ride = rec(1, 0, 60)
    later = rec(2, 50, 60)  # 10 min of 60 overlap
    run = rec(3, 0, 60, activity_type="Run")
    assert find_duplicates([ride, later, run]) == {}


def test_back_to_back_rides_are_not_duplicates() -> None:
    assert find_duplicates([rec(1, 0, 30), rec(2, 30, 30)]) == {}


def test_primary_selection_order() -> None:
    # Both have power: the one paired with a planned workout wins.
    assert find_duplicates([rec(1, 0, 30), rec(2, 0, 30, paired=True)]) == {1: 2}
    # Same power and pairing: longer moving time wins; then lowest id.
    assert find_duplicates([rec(1, 0, 30), rec(2, 0, 31)]) == {1: 2}
    assert find_duplicates([rec(5, 0, 30), rec(4, 0, 30)]) == {5: 4}


def test_groups_are_transitive() -> None:
    result = find_duplicates([rec(1, 0, 60), rec(2, 10, 60), rec(3, 20, 60, has_power=False)])
    assert result == {2: 1, 3: 1}


def test_zero_duration_is_ignored() -> None:
    assert find_duplicates([rec(1, 0, 0), rec(2, 0, 30)]) == {}


# --------------------------------------------------------------------------
# Provenance mapping
# --------------------------------------------------------------------------


def test_activity_provenance_fields_are_mapped() -> None:
    values = activity_values(
        {
            "id": "i77",
            "start_date_local": "2026-11-06T06:30:00",
            "start_date": "2026-11-06T01:00:00Z",
            "source": "OAUTH_CLIENT",
            "oauth_client_name": "icTrainer",
            "device_name": " icTrainer ",
            "external_id": "abc",
            "paired_event_id": 152154504,
        }
    )
    assert values["origin"] == "OAUTH_CLIENT"
    assert values["origin_client"] == "icTrainer"
    assert values["device_name"] == "icTrainer"
    assert values["origin_external_id"] == "abc"
    assert values["paired_event_id"] == "152154504"
    bare = activity_values(
        {"id": 1, "start_date_local": "2026-11-06T06:30:00", "start_date": "2026-11-06T01:00:00Z"}
    )
    assert bare["origin"] is None and bare["paired_event_id"] is None


# --------------------------------------------------------------------------
# Sync checks
# --------------------------------------------------------------------------

LINKED = {
    "coros_user_id": "abc",
    "coros_sync_activities": True,
    "coros_download_wellness": True,
    "coros_upload_workouts": True,
    "coros_last_upload": "2026-11-05T07:47:31Z",
}
PLAN = PublishedRef("500", TODAY, "Gentle Z1 Recovery Spin", 1800, 74.4)


def act(i: int, **kw: Any) -> ActivityRef:
    values: dict[str, Any] = {
        "id": i,
        "source_activity_id": f"i{i}",
        "start_date_local": TODAY,
        "name": "Gentle Z1 Recovery Spin",
        "activity_type": "VirtualRide",
        "origin": "OAUTH_CLIENT",
        "origin_client": "icTrainer",
        "device_name": None,
        "paired_event_id": "500",
        "is_indoor": True,
        "avg_power_watts": 73.0,
        "moving_time_s": 1790,
        "duplicate_of_activity_id": None,
    }
    values.update(kw)
    return ActivityRef(**values)


def wellness(days_ago: int, *fields: str) -> WellnessRef:
    return WellnessRef(TODAY - dt.timedelta(days=days_ago), frozenset(fields))


def run(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "snapshot": LINKED,
        "activities": [],
        "wellness": [],
        "published": [PLAN],
        "today": TODAY,
    }
    args.update(kw)
    return {c.check_name: c for c in evaluate(**args)}


def test_every_check_is_reported_in_order() -> None:
    assert list(run()) == list(CHECKS)


def test_nothing_is_verified_without_data() -> None:
    r = run()
    assert r["coros.link"].status == "configured"
    assert r["intervals_to_ictrainer.planned"].status == "not_observed"
    assert r["ictrainer.erg_execution"].status == "not_observed"
    assert r["ictrainer_to_intervals.activities"].status == "not_observed"
    assert r["coros_to_intervals.activities"].status == "not_observed"
    assert r["coros_to_intervals.wellness"].status == "not_observed"
    assert r["intervals_to_coros.workouts"].status == "configured"
    assert r["activities.duplicates"].status == "verified"


def test_unlinked_coros_is_not_configured() -> None:
    r = run(snapshot={})
    assert r["coros.link"].status == "not_configured"
    assert r["coros_to_intervals.activities"].status == "not_configured"
    assert r["coros_to_intervals.wellness"].status == "not_configured"
    assert r["intervals_to_coros.workouts"].status == "not_configured"


def test_ictrainer_ride_of_published_workout_verifies_all_ictrainer_directions() -> None:
    r = run(activities=[act(1)])
    assert r["intervals_to_ictrainer.planned"].status == "verified"
    erg = r["ictrainer.erg_execution"]
    assert erg.status == "verified", erg.summary
    assert erg.evidence["planned_avg_power_watts"] == 74.4
    assert r["ictrainer_to_intervals.activities"].status == "verified"


def test_ictrainer_detected_by_device_name_too() -> None:
    r = run(activities=[act(1, origin="UPLOAD", origin_client=None, device_name="icTrainer 3")])
    assert r["ictrainer_to_intervals.activities"].status == "verified"


def test_paired_ride_with_other_name_is_not_proof_of_calendar_sync() -> None:
    r = run(activities=[act(1, name="Afternoon ride")])
    assert r["intervals_to_ictrainer.planned"].status == "not_observed"
    assert r["intervals_to_ictrainer.planned"].evidence["planned_name"] == PLAN.name


def test_erg_execution_flags_a_ride_that_did_not_follow_the_plan() -> None:
    r = run(activities=[act(1, avg_power_watts=120.0, moving_time_s=900, is_indoor=False)])
    erg = r["ictrainer.erg_execution"]
    assert erg.status == "attention"
    assert len(erg.evidence["problems"]) == 3


def test_erg_execution_without_power_is_attention() -> None:
    r = run(activities=[act(1, avg_power_watts=None)])
    assert r["ictrainer.erg_execution"].status == "attention"
    assert "no power data" in r["ictrainer.erg_execution"].summary


def test_ride_paired_with_a_foreign_event_is_ignored() -> None:
    r = run(activities=[act(1, paired_event_id="999")])
    assert r["ictrainer.erg_execution"].status == "not_observed"
    assert r["ictrainer_to_intervals.activities"].status == "verified"


def test_coros_activity_is_verified() -> None:
    r = run(activities=[act(2, origin="COROS", origin_client=None, paired_event_id=None)])
    assert r["coros_to_intervals.activities"].status == "verified"
    assert r["coros_to_intervals.activities"].evidence["sources"] == {"COROS": 1}


def test_wellness_verified_stale_and_attribution() -> None:
    r = run(wellness=[wellness(1, "restingHR", "hrv", "sleepSecs"), wellness(2, "steps")])
    w = r["coros_to_intervals.wellness"]
    assert w.status == "verified"
    assert w.evidence["fields"] == {"hrv": 1, "restingHR": 1, "sleepSecs": 1, "steps": 1}
    assert "sleepScore" in w.evidence["fields_never_seen"]
    assert w.evidence["attribution"].startswith("COROS is the only")

    stale = run(wellness=[wellness(5, "restingHR")])["coros_to_intervals.wellness"]
    assert stale.status == "attention"

    both = run(snapshot={**LINKED, "polar_download_wellness": True}, wellness=[wellness(0, "hrv")])
    assert both["coros_to_intervals.wellness"].evidence["attribution"].startswith("ambiguous")


def test_duplicates_need_attention() -> None:
    kept = act(1)
    dup = act(2, origin="COROS", origin_client=None, duplicate_of_activity_id=1)
    d = run(activities=[kept, dup])["activities.duplicates"]
    assert d.status == "attention"
    assert d.evidence["pairs"][0]["kept"]["activity_id"] == "i1"


def test_manual_confirmation_upgrades_only_unobservable_results() -> None:
    conf = [
        Confirmation("intervals_to_ictrainer.planned", "seen in icTrainer", T0),
        Confirmation("intervals_to_coros.workouts", "on the watch", T0),
    ]
    r = run(confirmations=conf)
    assert r["intervals_to_ictrainer.planned"].status == "confirmed"
    assert "seen in icTrainer" in r["intervals_to_ictrainer.planned"].summary
    assert r["intervals_to_coros.workouts"].status == "confirmed"
    # Data evidence wins over a confirmation.
    bad = run(
        activities=[act(1, avg_power_watts=None)],
        confirmations=[Confirmation("ictrainer.erg_execution", "rode it", T0)],
    )
    assert bad["ictrainer.erg_execution"].status == "attention"
    # A confirmation can't turn on an integration that is off.
    off = run(snapshot={}, confirmations=conf)
    assert off["intervals_to_coros.workouts"].status == "not_configured"


def test_result_dict_has_direction() -> None:
    c = run()["coros.link"]
    d = c.as_dict()
    assert d["direction"] == CHECKS["coros.link"]
    assert dataclasses.asdict(c)["status"] == "configured"
