"""Deterministic, conservative plan used when the LLM is unavailable or its
output cannot be made valid. Always within the cap by construction (tested).
"""

from __future__ import annotations

from app.agents.schemas import TrainingObjective, WorkoutDraft, WorkoutStep
from app.training.intensity import Intensity


def _step(kind: str, minutes: int, low: int, high: int, note: str) -> WorkoutStep:
    return WorkoutStep.model_validate(
        {
            "kind": kind,
            "duration_seconds": minutes * 60,
            "power_low_pct_ftp": low,
            "power_high_pct_ftp": high,
            "cadence_rpm": None,
            "note": note,
        }
    )


def fallback_plan(
    max_intensity: Intensity, reason: str, max_minutes: int | None = None
) -> tuple[TrainingObjective, WorkoutDraft | None]:
    """``max_minutes`` (the plan's limit for the day) shortens the main block."""
    if max_intensity <= Intensity.REST:
        objective = TrainingObjective(
            session_type="rest",
            target_duration_minutes=0,
            primary_goal="Rest",
            rationale=f"Deterministic fallback: rest. {reason}",
            long_term_link="Recovery protects long-term consistency.",
        )
        return objective, None
    if max_intensity == Intensity.RECOVERY:
        main = max(10, min(30, (max_minutes or 40) - 10))
        objective = TrainingObjective(
            session_type="recovery",
            target_duration_minutes=main + 10,
            primary_goal="Easy active recovery spin",
            rationale=f"Deterministic fallback within a recovery cap. {reason}",
            long_term_link="Promotes recovery without adding fatigue.",
        )
        steps = [
            _step("warmup", 5, 40, 50, "very easy"),
            _step("steady", main, 45, 54, "Z1, relaxed cadence"),
            _step("cooldown", 5, 40, 45, "easy"),
        ]
        return objective, WorkoutDraft(
            name=f"Recovery spin {main + 10}'", description=objective.rationale, steps=steps
        )
    main = max(10, min(40, (max_minutes or 60) - 20))
    objective = TrainingObjective(
        session_type="endurance",
        target_duration_minutes=main + 20,
        primary_goal="Steady aerobic endurance",
        rationale=f"Deterministic conservative fallback (endurance). {reason}",
        long_term_link="Aerobic base for ultra-endurance events.",
    )
    steps = [
        _step("warmup", 10, 50, 60, "progressive"),
        _step("steady", main, 62, 70, "Z2"),
        _step("cooldown", 10, 45, 55, "easy"),
    ]
    return objective, WorkoutDraft(
        name=f"Endurance {main + 20}'", description=objective.rationale, steps=steps
    )
