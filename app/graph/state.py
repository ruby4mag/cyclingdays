"""Shared graph state.

Only JSON-compatible values (str, numbers, dicts, lists) are stored, so the
PostgreSQL checkpointer can persist every step without custom serializers,
and a checkpoint can be inspected with plain SQL.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Outcome = Literal["planned", "rest", "fallback", "aborted"]


class CoachState(TypedDict, total=False):
    # Input
    athlete_slug: str
    plan_date: str  # ISO date, athlete-local
    thread_id: str
    run_id: str
    # Deterministic context and caps
    context: dict[str, Any]
    deterministic_cap: str  # Intensity label from readiness
    effective_cap: str  # after the recovery assessment (never above deterministic)
    # Accepted agent outputs (model_dump(mode="json"))
    training_analysis: dict[str, Any] | None
    recovery_assessment: dict[str, Any] | None
    objective: dict[str, Any] | None
    workout: dict[str, Any] | None
    # Every LLM call (accepted or rejected), appended by each node
    calls: Annotated[list[dict[str, Any]], operator.add]
    # Routing and result
    route: str
    outcome: Outcome
    fallback_reason: str | None
    abort_reason: str | None
    decision_id: int | None
    planned_workout_id: int | None
    validation: dict[str, Any] | None  # final validator result for the stored workout
