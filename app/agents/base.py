"""Run one agent step: LLM call -> schema -> deterministic validation -> correction loop.

Every LLM call (accepted or rejected) produces a ``CallRecord``. The graph
stores these as ``coach_decisions`` rows, so rejected outputs are audited too.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.agents.llm import LLMOutputError, LLMUnavailableError, StructuredLLM
from app.graph.prompts import Prompt, correction_message

logger = logging.getLogger(__name__)


def render_payload(payload: dict[str, Any]) -> str:
    """Stable JSON rendering (sorted keys), so identical inputs give identical prompts."""
    return json.dumps(payload, indent=1, sort_keys=True, default=str, ensure_ascii=False)


@dataclass
class CallRecord:
    node: str
    prompt_id: str
    prompt_sha256: str
    configured_model: str
    reported_model: str | None
    accepted: bool
    attempt: int  # correction round, 0 = first answer
    input_payload: dict[str, Any]
    correction: str | None
    output: dict[str, Any] | None
    violations: list[str]
    error: str | None
    usage: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class AgentOutcome[T: BaseModel]:
    parsed: T | None
    calls: list[CallRecord] = field(default_factory=list)
    unavailable: str | None = None  # API failure after retries
    rejected: list[str] = field(default_factory=list)  # last violations if never valid

    @property
    def ok(self) -> bool:
        return self.parsed is not None


async def run_agent[T: BaseModel](
    llm: StructuredLLM,
    prompt: Prompt,
    schema: type[T],
    payload: dict[str, Any],
    *,
    validate: Callable[[T], list[str]] | None = None,
    max_corrections: int = 2,
) -> AgentOutcome[T]:
    outcome: AgentOutcome[T] = AgentOutcome(parsed=None)
    base_user = render_payload(payload)
    correction: str | None = None
    for attempt in range(max_corrections + 1):
        user = base_user + (correction or "")
        record = CallRecord(
            node=prompt.name,
            prompt_id=prompt.id,
            prompt_sha256=prompt.sha256,
            configured_model=llm.configured_model,
            reported_model=None,
            accepted=False,
            attempt=attempt,
            input_payload=payload,
            correction=correction,
            output=None,
            violations=[],
            error=None,
            usage=None,
        )
        outcome.calls.append(record)
        try:
            result = await llm.generate(schema, system=prompt.system, user=user, name=prompt.name)
        except LLMUnavailableError as exc:
            record.error = str(exc)
            outcome.unavailable = str(exc)
            return outcome
        except LLMOutputError as exc:
            record.error = str(exc)
            violations = [f"output was not valid for the schema: {str(exc)[:500]}"]
            record.violations = violations
            outcome.rejected = violations
            correction = correction_message(violations)
            continue

        record.reported_model = result.model
        record.usage = result.usage()
        record.output = result.parsed.model_dump(mode="json")
        violations = validate(result.parsed) if validate else []
        record.violations = violations
        if not violations:
            record.accepted = True
            outcome.parsed = result.parsed
            outcome.rejected = []
            return outcome
        logger.warning(
            "llm_output_rejected",
            extra={"node": prompt.name, "attempt": attempt, "violations": violations},
        )
        outcome.rejected = violations
        correction = correction_message(violations)
    return outcome
