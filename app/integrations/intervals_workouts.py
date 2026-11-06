"""Intervals.icu workout format: structured workout → calendar event, and back.

Format sources:

* ``POST /api/v1/athlete/{id}/events`` and ``/events/bulk`` accept workouts
  "in native Intervals.icu format ('description' field)" (OpenAPI, checked
  2026-11-05).
* The text syntax comes from the official forum guide "Workout Builder Syntax
  Quick Guide" (forum.intervals.icu/t/123701). A step is one line
  ``- <cue> <duration> <target> [cadence]``, e.g. ``- Work 12m 88-92% 90rpm``.
  Lines without a leading ``-`` are free text, except repeat headers ending in
  ``<n>x``.
* Verified against the real API (2026-11-05, probe event created and deleted).
  Intervals.icu parses the text into ``workout_doc.steps`` with ``duration``
  in seconds, ``power`` as ``{"value"|"start"/"end", "units": "%ftp"}``,
  ``cadence`` ``{"value", "units": "rpm"}`` and ``text`` (the cue). The free
  text becomes ``workout_doc.description``.

Safety: cues are fixed labels chosen by kind; LLM text never appears on a
step line, so it cannot alter targets or durations. Free text is sanitised
so no line can be read as a step or a repeat header.
"""

from __future__ import annotations

import re
from typing import Any

from app.training.workout_generator import StructuredWorkout, WorkoutStepSpec

EVENT_CATEGORY = "WORKOUT"
EVENT_TYPE = "Ride"
EXTERNAL_ID_PREFIX = "cycledays"
STEP_CUE = {
    "warmup": "Warmup",
    "steady": "Steady",
    "work": "Work",
    "recovery": "Recovery",
    "cooldown": "Cooldown",
}
_REPEAT_HEADER = re.compile(r"\d+\s*x\s*$", re.IGNORECASE)
_STEP_PREFIX = re.compile(r"^\s*[-*•]+\s*")
_SPECIAL = re.compile(r"[<>^]")  # timed-prompt syntax ("^", "<!>")
MAX_TEXT_LINES = 40


def external_id_for(athlete_slug: str, workout_date: str) -> str:
    """Stable idempotency key: one planned workout per athlete and day."""
    return f"{EXTERNAL_ID_PREFIX}:{athlete_slug}:{workout_date}"


def format_duration(seconds: int) -> str:
    """``3750`` → ``1h2m30s``; ``600`` → ``10m``."""
    if seconds <= 0:
        raise ValueError("step duration must be positive")
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    out = (f"{h}h" if h else "") + (f"{m}m" if m else "") + (f"{s}s" if s else "")
    return out


def format_step(step: WorkoutStepSpec) -> str:
    target = (
        f"{step.low_pct}%" if step.low_pct == step.high_pct else f"{step.low_pct}-{step.high_pct}%"
    )
    line = f"- {STEP_CUE[step.kind]} {format_duration(step.duration_s)} {target}"
    if step.cadence_rpm is not None:
        line += f" {step.cadence_rpm}rpm"
    return line


def _clean_text(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = _SPECIAL.sub("", _STEP_PREFIX.sub("", raw)).strip()
        if not line:
            continue
        if _REPEAT_HEADER.search(line):
            line += "."
        lines.append(line)
    return lines[:MAX_TEXT_LINES]


def to_description(workout: StructuredWorkout) -> str:
    """Native Intervals.icu workout text: notes, a blank line, then the steps."""
    notes = _clean_text(workout.description)
    est = workout.estimate
    notes.append(
        f"Objective: {workout.objective.session_type.replace('_', ' ')}. "
        f"{' '.join(_clean_text(workout.objective.primary_goal))}"
    )
    notes.append(
        f"FTP reference {workout.ftp_watts} W. "
        f"Estimated TSS {est.tss:.0f}, IF {est.intensity_factor:.2f}."
    )
    cues = [
        f"Step {s.index} ({STEP_CUE[s.kind]}): {' '.join(_clean_text(s.note))}"
        for s in workout.steps
        if _clean_text(s.note)
    ]
    steps = [format_step(s) for s in workout.steps]
    return "\n".join([*notes, *cues, "", *steps]) + "\n"


def to_event(workout: StructuredWorkout, *, indoor: bool = True) -> dict[str, Any]:
    """``EventEx`` body for ``POST /events/bulk?upsert=true``."""
    return {
        "category": EVENT_CATEGORY,
        "type": EVENT_TYPE,
        "start_date_local": f"{workout.workout_date.isoformat()}T00:00:00",
        "name": workout.name,
        "description": to_description(workout),
        "indoor": indoor,
        "external_id": external_id_for(workout.athlete_slug, workout.workout_date.isoformat()),
    }


def _power(step: dict[str, Any]) -> tuple[float, float] | None:
    power = step.get("power")
    if not isinstance(power, dict) or power.get("units") != "%ftp":
        return None
    if "value" in power:
        v = float(power["value"])
        return v, v
    if "start" in power and "end" in power:
        return float(power["start"]), float(power["end"])
    return None


def verify_event(event: dict[str, Any], workout: StructuredWorkout) -> list[str]:
    """Compare a retrieved event with the workout we published. [] = verified."""
    problems: list[str] = []
    expected = to_event(workout)
    for key in ("category", "type", "name", "external_id"):
        if event.get(key) != expected[key]:
            problems.append(f"{key}: expected {expected[key]!r}, got {event.get(key)!r}")
    if str(event.get("start_date_local", ""))[:10] != workout.workout_date.isoformat():
        problems.append(
            f"date: expected {workout.workout_date}, got {event.get('start_date_local')!r}"
        )
    doc = event.get("workout_doc")
    steps = doc.get("steps") if isinstance(doc, dict) else None
    if not isinstance(steps, list):
        return [*problems, "workout_doc.steps missing: Intervals.icu did not parse the workout"]
    if len(steps) != len(workout.steps):
        return [
            *problems,
            f"step count: expected {len(workout.steps)}, Intervals.icu parsed {len(steps)}",
        ]
    for ours, theirs in zip(workout.steps, steps, strict=True):
        if not isinstance(theirs, dict):
            problems.append(f"step {ours.index}: not an object")
            continue
        if theirs.get("reps") or theirs.get("steps"):
            problems.append(f"step {ours.index}: parsed as a repeat block")
        if theirs.get("duration") != ours.duration_s:
            problems.append(
                f"step {ours.index}: duration {theirs.get('duration')!r}s, "
                f"expected {ours.duration_s}s"
            )
        power = _power(theirs)
        if power != (float(ours.low_pct), float(ours.high_pct)):
            problems.append(
                f"step {ours.index}: power {theirs.get('power')!r}, expected "
                f"{ours.low_pct}-{ours.high_pct}% FTP"
            )
        if theirs.get("ramp"):
            problems.append(f"step {ours.index}: parsed as a ramp")
        cadence = theirs.get("cadence")
        got_cadence = cadence.get("value") if isinstance(cadence, dict) else None
        if got_cadence != ours.cadence_rpm:
            problems.append(
                f"step {ours.index}: cadence {got_cadence!r}, expected {ours.cadence_rpm!r}"
            )
    return problems


def event_matches(event: dict[str, Any], workout: StructuredWorkout) -> bool:
    """True when the remote event already carries exactly this workout."""
    expected = to_event(workout)
    return (
        all(event.get(k) == expected[k] for k in ("name", "description", "external_id"))
        and str(event.get("start_date_local", ""))[:10] == workout.workout_date.isoformat()
    )
