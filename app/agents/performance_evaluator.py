"""Performance evaluator: interprets the deterministic evaluation of one ride (Phase 7)."""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentOutcome, run_agent
from app.agents.llm import StructuredLLM
from app.agents.schemas import PerformanceReview
from app.graph.prompts import PERFORMANCE_REVIEW


def review_violations(review: PerformanceReview, evaluation: dict[str, Any]) -> list[str]:
    """Deterministic checks on the review. It may only hold or lower load."""
    problems: list[str] = []
    if evaluation.get("outcome") == "abandoned" and review.recommendation == "keep":
        problems.append(
            "the session was abandoned (under 50 % completed): recommend 'reduce' or 'recover'"
        )
    return problems


async def review_performance(
    llm: StructuredLLM, payload: dict[str, Any], *, max_corrections: int
) -> AgentOutcome[PerformanceReview]:
    return await run_agent(
        llm,
        PERFORMANCE_REVIEW,
        PerformanceReview,
        payload,
        validate=lambda r: review_violations(r, payload["evaluation"]),
        max_corrections=max_corrections,
    )
