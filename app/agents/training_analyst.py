"""Training analyst: interprets deterministic load metrics and recent training."""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentOutcome, run_agent
from app.agents.llm import StructuredLLM
from app.agents.schemas import TrainingAnalysis
from app.graph.prompts import TRAINING_ANALYSIS


async def analyse_training(
    llm: StructuredLLM, context: dict[str, Any], *, max_corrections: int
) -> AgentOutcome[TrainingAnalysis]:
    payload = {
        "plan_date": context["plan_date"],
        "athlete": context["athlete"],
        "load": context["load"],
        "recent_activities": context["recent_activities"],
        "data_notes": context["data_notes"],
    }
    return await run_agent(
        llm, TRAINING_ANALYSIS, TrainingAnalysis, payload, max_corrections=max_corrections
    )
