"""Deterministic evaluation of one completed ride (Phase 7).

Combines the following:

* **Power**, planned versus actual. If a power stream exists, each step is
  compared with ``performance.evaluate_workout``. Otherwise a cruder summary
  comparison is used and labelled ``method = "summary"``.
* **Heart rate:** average HR in each work step, and **aerobic decoupling**
  (Pa:HR) over the main set. Decoupling = (P/HR in the first half - P/HR in
  the second half) ÷ P/HR in the first half x 100. It needs at least
  ``MIN_DECOUPLING_S`` of paired power and HR, and is the usual Friel
  definition.
* **Power zones** (v2): seconds in each Coggan zone from the power stream, at
  the FTP the ride was done with (``ftp_watts``). Rides without a stream or
  FTP get none; the dashboard aggregates these.
* **Subjective input:** session RPE against the expected RPE for the session
  type, Intervals.icu "feel", and the athlete's wellness fatigue and
  soreness that day.

The **outcome** classification (``OUTCOMES``) is what the planner and the
adaptation rules read. No LLM is involved; an optional LLM narrative is added
on top by the activity processor.

Pure functions; no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.training.adaptation import EXPECTED_RPE, MIN_COMPLETION_PCT, RPE_MARGIN
from app.training.metrics import time_in_zones
from app.training.performance import PlannedStep, evaluate_workout

EVALUATION_VERSION = "evaluation/v2"
MIN_DECOUPLING_S = 20 * 60
AS_PLANNED_COMPLETION_PCT = 95.0
AS_PLANNED_DEVIATION_PCT = 5.0
PARTIAL_COMPLETION_PCT = 50.0

OUTCOMES = (
    "as_planned",  # done fully, on target, felt as expected
    "completed_with_deviation",  # done, but off target or harder than expected
    "partial",  # 50-85 % of the planned time
    "abandoned",  # under 50 %
    "unplanned",  # no planned workout for the ride
)


def to_seconds_series(
    values: Sequence[float | None] | None, time: Sequence[int] | None = None
) -> list[float | None] | None:
    """Stream → 1 Hz series. With a time stream, pauses become ``None`` gaps."""
    if not values:
        return None
    if not time or len(time) != len(values):
        return [None if v is None else float(v) for v in values]
    size = int(max(time)) + 1
    if size > 24 * 3600:
        return [None if v is None else float(v) for v in values]
    out: list[float | None] = [None] * size
    for t, v in zip(time, values, strict=True):
        if t is not None and 0 <= int(t) < size:
            out[int(t)] = None if v is None else float(v)
    return out


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def decoupling_pct(
    power: Sequence[float | None], hr: Sequence[float | None], start: int, end: int
) -> float | None:
    pairs = [
        (p, h)
        for p, h in zip(power[start:end], hr[start:end], strict=False)
        if p is not None and h is not None and p > 0 and h > 0
    ]
    if len(pairs) < MIN_DECOUPLING_S:
        return None
    half = len(pairs) // 2
    first, second = pairs[:half], pairs[half:]
    r1 = sum(p for p, _ in first) / sum(h for _, h in first)
    r2 = sum(p for p, _ in second) / sum(h for _, h in second)
    return round((r1 - r2) / r1 * 100, 2)


def _main_span(steps: Sequence[dict[str, Any]]) -> tuple[int, int]:
    """Seconds from the end of the warmup to the start of the cooldown."""
    t = 0
    start: int | None = None
    end: int | None = None
    for s in steps:
        if s["kind"] != "warmup" and start is None:
            start = t
        if s["kind"] == "cooldown" and end is None:
            end = t
        t += s["duration_s"]
    first = start or 0
    return first, end if end is not None and end > first else t


def evaluate_ride(
    *,
    planned: dict[str, Any] | None,
    activity: dict[str, Any],
    power: Sequence[float | None] | None,
    heartrate: Sequence[float | None] | None,
    wellness: dict[str, Any] | None = None,
    ftp_watts: float | None = None,
) -> dict[str, Any]:
    """Evaluate one ride.

    ``planned`` is the stored ``StructuredWorkout`` (JSON) or None.
    ``activity`` holds ``moving_time_s``, ``avg_power_watts``,
    ``normalized_power_watts``, ``avg_hr_bpm``, ``rpe`` and ``feel``.
    """
    notes: list[str] = []
    session_type = None if planned is None else planned["objective"]["session_type"]
    result: dict[str, Any] = {
        "version": EVALUATION_VERSION,
        "session_type": session_type,
        "planned": None,
        "power": None,
        "heart_rate": None,
        "subjective": None,
        "zones": None,
        "outcome": "unplanned",
        "notes": notes,
    }
    moving = activity.get("moving_time_s")
    avg_power = activity.get("avg_power_watts")

    # --- power ---------------------------------------------------------------
    if planned is not None:
        steps = planned["steps"]
        planned_s = int(planned["total_s"])
        planned_avg = planned["estimate"]["average_power_watts"]
        result["planned"] = {
            "name": planned["name"],
            "minutes": round(planned_s / 60, 1),
            "avg_power_watts": planned_avg,
            "tss": planned["estimate"]["tss"],
            "steps": len(steps),
        }
        if power and any(v is not None for v in power):
            ev = evaluate_workout(
                [
                    PlannedStep(
                        duration_s=s["duration_s"],
                        target_low_watts=s["low_watts"] or None,
                        target_high_watts=s["high_watts"] or None,
                        label=f"{s['index']}:{s['kind']}",
                    )
                    for s in steps
                ],
                power,
            )
            result["power"] = {
                "method": "stream",
                "completion_pct": round(ev.completion_pct, 1),
                "work_completion_pct": None
                if ev.work_completion_pct is None
                else round(ev.work_completion_pct, 1),
                "power_deviation_pct": None
                if ev.power_deviation_pct is None
                else round(ev.power_deviation_pct, 1),
                "steps_completed": ev.completed_steps,
                "steps_total": len(ev.steps),
                "extra_recorded_s": ev.extra_recorded_s,
                "steps": [
                    {
                        "index": r.index,
                        "label": r.label,
                        "status": r.status,
                        "avg_watts": None if r.avg_watts is None else round(r.avg_watts, 1),
                        "target_watts": r.target_mid_watts,
                        "deviation_pct": None
                        if r.deviation_pct is None
                        else round(r.deviation_pct, 1),
                    }
                    for r in ev.steps
                ],
            }
            notes.extend(ev.notes)
        elif moving:
            completion = min(200.0, moving / planned_s * 100) if planned_s else None
            deviation = (
                round((avg_power - planned_avg) / planned_avg * 100, 1)
                if avg_power and planned_avg
                else None
            )
            result["power"] = {
                "method": "summary",
                "completion_pct": None if completion is None else round(completion, 1),
                "power_deviation_pct": deviation,
            }
            notes.append("no power stream: compared from activity totals (less precise)")

    # --- heart rate ------------------------------------------------------------
    if heartrate and any(v is not None for v in heartrate):
        hr_info: dict[str, Any] = {"avg_hr_bpm": activity.get("avg_hr_bpm")}
        if power:
            span = _main_span(planned["steps"]) if planned is not None else (0, len(power))
            hr_info["decoupling_pct"] = decoupling_pct(power, heartrate, *span)
        if planned is not None:
            t = 0
            work = []
            for s in planned["steps"]:
                window = [h for h in heartrate[t : t + s["duration_s"]] if h is not None]
                if s["kind"] == "work" and window:
                    work.append({"index": s["index"], "avg_hr_bpm": round(_mean(window) or 0, 1)})
                t += s["duration_s"]
            hr_info["work_steps"] = work
        result["heart_rate"] = hr_info
    elif activity.get("avg_hr_bpm") is not None:
        result["heart_rate"] = {"avg_hr_bpm": activity["avg_hr_bpm"], "decoupling_pct": None}

    # --- subjective --------------------------------------------------------------
    rpe = activity.get("rpe")
    expected = EXPECTED_RPE.get(session_type or "")
    result["subjective"] = {
        "rpe": rpe,
        "expected_rpe": expected,
        "rpe_delta": None if rpe is None or expected is None else round(rpe - expected, 1),
        "feel": activity.get("feel"),
        "wellness_fatigue": (wellness or {}).get("fatigue"),
        "wellness_soreness": (wellness or {}).get("soreness"),
    }
    if rpe is None:
        notes.append("no RPE recorded: enter it in Intervals.icu to improve adaptation")

    # --- power zones ---------------------------------------------------------------
    if power and ftp_watts:
        dist = time_in_zones(power, ftp_watts)
        result["zones"] = {
            "ftp_watts": ftp_watts,
            "seconds_by_zone": {str(z): s for z, s in dist.seconds_by_zone.items()},
            "missing_seconds": dist.missing_seconds,
        }

    # --- outcome ---------------------------------------------------------------
    if planned is not None:
        p = result["power"] or {}
        completion = p.get("completion_pct")
        deviation = p.get("power_deviation_pct")
        delta = result["subjective"]["rpe_delta"]
        if completion is None:
            result["outcome"] = "completed_with_deviation"
            notes.append("completion unknown")
        elif completion < PARTIAL_COMPLETION_PCT:
            result["outcome"] = "abandoned"
        elif completion < MIN_COMPLETION_PCT:
            result["outcome"] = "partial"
        elif (
            completion >= AS_PLANNED_COMPLETION_PCT
            and (deviation is None or abs(deviation) <= AS_PLANNED_DEVIATION_PCT)
            and (delta is None or delta < RPE_MARGIN)
        ):
            result["outcome"] = "as_planned"
        else:
            result["outcome"] = "completed_with_deviation"
    return result


def summary_numbers(evaluation: dict[str, Any]) -> dict[str, float | None]:
    """The numbers stored in ``workout_performance`` columns and used by adaptation."""
    p = evaluation.get("power") or {}
    hr = evaluation.get("heart_rate") or {}
    return {
        "completion_pct": p.get("completion_pct"),
        "power_deviation_pct": p.get("power_deviation_pct"),
        "decoupling_pct": hr.get("decoupling_pct"),
        "rpe": (evaluation.get("subjective") or {}).get("rpe"),
    }
