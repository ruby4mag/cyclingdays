"""Recovery analyst: interprets the deterministic readiness assessment."""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentOutcome, run_agent
from app.agents.llm import StructuredLLM
from app.agents.schemas import RecoveryAssessment
from app.graph.prompts import RECOVERY_ASSESSMENT


async def assess_recovery(
    llm: StructuredLLM,
    context: dict[str, Any],
    training_analysis: dict[str, Any],
    *,
    max_corrections: int,
) -> AgentOutcome[RecoveryAssessment]:
    payload = {
        "plan_date": context["plan_date"],
        "readiness": context["readiness"],
        "max_intensity": context["readiness"]["max_intensity"],
        "training_analysis": training_analysis,
    }
    # No validation function: a recommendation above the deterministic cap is
    # harmless because guards.effective_cap() clamps it.
    return await run_agent(
        llm, RECOVERY_ASSESSMENT, RecoveryAssessment, payload, max_corrections=max_corrections
    )
