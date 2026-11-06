"""Guards, fallback, prompts, routing, agent correction loop and the OpenAI wrapper."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import openai
import pytest
from pydantic import ValidationError

from app.agents.base import render_payload, run_agent
from app.agents.llm import LLMOutputError, LLMUnavailableError, OpenAIStructuredLLM
from app.agents.schemas import (
    RecoveryAssessment,
    TrainingAnalysis,
    TrainingObjective,
    WorkoutDraft,
)
from app.graph import guards, routing
from app.graph.fallback import fallback_plan
from app.graph.prompts import ALL_PROMPTS, TRAINING_ANALYSIS, correction_message
from app.training.intensity import Intensity
from tests.conftest import make_unit_settings
from tests.fakes import (
    ANALYSIS,
    ScriptedLLM,
    endurance_workout,
    objective,
    recovery,
    step,
    sweet_spot_workout,
)

# --- schemas -----------------------------------------------------------------


def test_schemas_reject_out_of_range_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        WorkoutDraft.model_validate({**endurance_workout(), "steps": [step("work", 1, 90, 80)]})
    with pytest.raises(ValidationError):
        WorkoutDraft.model_validate({**endurance_workout(), "steps": [step("work", 1, 90, 200)]})
    with pytest.raises(ValidationError):
        TrainingAnalysis.model_validate({**ANALYSIS, "extra": 1})
    with pytest.raises(ValidationError):
        TrainingObjective.model_validate(objective("hiit"))


# --- guards ----------------------------------------------------------------------


def test_objective_guard() -> None:
    ss = TrainingObjective.model_validate(objective("sweet_spot"))
    assert guards.objective_violations(ss, Intensity.VO2MAX) == []
    assert "exceeds" in guards.objective_violations(ss, Intensity.TEMPO)[0]
    bad_rest = TrainingObjective.model_validate(objective("rest", 30))
    assert any("rest day" in v for v in guards.objective_violations(bad_rest, Intensity.REST))
    short = TrainingObjective.model_validate(objective("endurance", 10))
    assert any("at least" in v for v in guards.objective_violations(short, Intensity.VO2MAX))


def test_workout_guard_accepts_valid_and_flags_each_rule() -> None:
    ss = TrainingObjective.model_validate(objective("sweet_spot", 75))
    draft = WorkoutDraft.model_validate(sweet_spot_workout())
    assert guards.workout_violations(draft, ss, Intensity.VO2MAX, 175) == []
    # Cap lower than the session: every 88-92 % step (Z3/Z4 boundary at 90 %) breaks it.
    capped = guards.workout_violations(draft, ss, Intensity.TEMPO, 175)
    assert sum("above the allowed tempo" in v for v in capped) == 3

    endurance = TrainingObjective.model_validate(objective("endurance", 60))
    sneaky = endurance_workout()
    sneaky["steps"].insert(2, step("work", 5, 100, 105))  # threshold effort in an endurance ride
    v = guards.workout_violations(
        WorkoutDraft.model_validate(sneaky), endurance, Intensity.VO2MAX, 175
    )
    assert any("above the allowed endurance" in x for x in v)

    mislabeled = endurance_workout()
    mislabeled["steps"][1] = step("steady", 40, 95, 100)
    v = guards.workout_violations(
        WorkoutDraft.model_validate(mislabeled),
        TrainingObjective.model_validate(objective("threshold", 60)),
        Intensity.VO2MAX,
        175,
    )
    assert any("must be 'work'" in x for x in v)

    too_long = WorkoutDraft.model_validate(endurance_workout(120))
    v = guards.workout_violations(too_long, endurance, Intensity.VO2MAX, 175)
    assert any("differs from the objective" in x for x in v)

    no_warmup = endurance_workout()
    no_warmup["steps"][0]["kind"] = "steady"
    v = guards.workout_violations(
        WorkoutDraft.model_validate(no_warmup), endurance, Intensity.VO2MAX, 175
    )
    assert "the first step must be a warmup" in v


def test_workout_guard_requires_valid_ftp() -> None:
    draft = WorkoutDraft.model_validate(endurance_workout())
    with pytest.raises(ValueError):
        guards.workout_violations(
            draft,
            TrainingObjective.model_validate(objective("endurance", 60)),
            Intensity.VO2MAX,
            None,
        )


def test_effective_cap_never_raises_deterministic_cap() -> None:
    higher = RecoveryAssessment.model_validate(recovery("vo2max"))
    lower = RecoveryAssessment.model_validate(recovery("recovery"))
    assert guards.effective_cap(Intensity.ENDURANCE, higher) == Intensity.ENDURANCE
    assert guards.effective_cap(Intensity.THRESHOLD, lower) == Intensity.RECOVERY
    assert guards.effective_cap(Intensity.TEMPO, None) == Intensity.TEMPO


@pytest.mark.parametrize("cap", list(Intensity))
def test_fallback_always_passes_guards(cap: Intensity) -> None:
    obj, draft = fallback_plan(cap, "test")
    assert guards.objective_violations(obj, cap) == []
    if draft is None:
        assert obj.session_type == "rest"
    else:
        for ftp in (100, 175, 300):
            assert guards.workout_violations(draft, obj, cap, ftp) == []


# --- prompts / routing ------------------------------------------------------------


def test_prompts_are_versioned_and_hashed() -> None:
    ids = [p.id for p in ALL_PROMPTS]
    assert len(set(ids)) == len(ids) and all("/v" in i for i in ids)
    assert len(TRAINING_ANALYSIS.sha256) == 64
    assert "max_intensity" in TRAINING_ANALYSIS.system
    assert "- a\n- b" in correction_message(["a", "b"])
    assert render_payload({"b": 1, "a": 2}) == render_payload({"a": 2, "b": 1})


def test_invalid_route_fails_loudly() -> None:
    with pytest.raises(ValueError, match="invalid route"):
        routing.after_context({"route": "skip_safety"})
    with pytest.raises(ValueError):
        routing.after_workout({})
    assert routing.after_objective({"route": "rest"}) == "rest"


# --- run_agent ---------------------------------------------------------------------


async def test_agent_corrects_rejected_output() -> None:
    llm = ScriptedLLM({"objective_selection": [objective("vo2max"), objective("endurance", 60)]})
    from app.graph.prompts import OBJECTIVE_SELECTION

    out = await run_agent(
        llm,
        OBJECTIVE_SELECTION,
        TrainingObjective,
        {"x": 1},
        validate=lambda o: guards.objective_violations(o, Intensity.ENDURANCE),
        max_corrections=2,
    )
    assert out.ok and out.parsed is not None and out.parsed.session_type == "endurance"
    assert [c.accepted for c in out.calls] == [False, True]
    assert "exceeds" in out.calls[0].violations[0]
    assert out.calls[1].correction is not None and "rejected" in out.calls[1].correction
    assert "rejected by deterministic validation" in llm.prompts[1][1]


async def test_agent_gives_up_after_max_corrections() -> None:
    llm = ScriptedLLM({"objective_selection": [objective("vo2max")]})
    from app.graph.prompts import OBJECTIVE_SELECTION

    out = await run_agent(
        llm,
        OBJECTIVE_SELECTION,
        TrainingObjective,
        {},
        validate=lambda o: guards.objective_violations(o, Intensity.ENDURANCE),
        max_corrections=2,
    )
    assert not out.ok and len(out.calls) == 3 and out.rejected


async def test_agent_schema_error_then_unavailable() -> None:
    llm = ScriptedLLM({"training_analysis": [{"summary": "x"}, LLMUnavailableError("down")]})
    out = await run_agent(llm, TRAINING_ANALYSIS, TrainingAnalysis, {}, max_corrections=3)
    assert not out.ok and out.unavailable == "down"
    assert "schema" in out.calls[0].violations[0]
    assert len(out.calls) == 2


# --- OpenAI wrapper (mocked client, no network) ---------------------------------------


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


class FakeResponses:
    def __init__(self, items: list[Any]) -> None:
        self.items = items
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _llm(
    items: list[Any], **settings: Any
) -> tuple[OpenAIStructuredLLM, FakeResponses, list[float]]:
    responses = FakeResponses(items)
    client: Any = SimpleNamespace(responses=responses)
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    values = {"openai_model": "gpt-test", "openai_max_attempts": 3, **settings}
    return (
        OpenAIStructuredLLM(make_unit_settings(**values), client=client, sleep=sleep),
        responses,
        sleeps,
    )


def _ok(parsed: Any) -> Any:
    return SimpleNamespace(
        output_parsed=parsed,
        model="gpt-test-2026-01-01",
        id="resp_1",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        output=[],
    )


async def test_openai_wrapper_success_records_reported_model() -> None:
    parsed = TrainingAnalysis.model_validate(ANALYSIS)
    llm, responses, _ = _llm([_ok(parsed)])
    result = await llm.generate(TrainingAnalysis, system="s", user="u", name="n")
    assert result.parsed == parsed and result.model == "gpt-test-2026-01-01"
    call = responses.calls[0]
    assert call["model"] == "gpt-test" and call["text_format"] is TrainingAnalysis
    assert call["input"][0] == {"role": "system", "content": "s"}


async def test_openai_wrapper_retries_transient_errors() -> None:
    parsed = TrainingAnalysis.model_validate(ANALYSIS)
    llm, _responses, sleeps = _llm(
        [
            _status_error(openai.RateLimitError, 429),
            _status_error(openai.InternalServerError, 503),
            _ok(parsed),
        ]
    )
    result = await llm.generate(TrainingAnalysis, system="s", user="u", name="n")
    assert result.attempts == 3 and len(sleeps) == 2


async def test_openai_wrapper_gives_up_and_does_not_retry_auth() -> None:
    llm, _, _ = _llm([_status_error(openai.InternalServerError, 500)] * 3)
    with pytest.raises(LLMUnavailableError) as info:
        await llm.generate(TrainingAnalysis, system="s", user="u", name="n")
    assert info.value.retryable
    llm, responses, sleeps = _llm([_status_error(openai.AuthenticationError, 401)])
    with pytest.raises(LLMUnavailableError) as info:
        await llm.generate(TrainingAnalysis, system="s", user="u", name="n")
    assert not info.value.retryable and len(responses.calls) == 1 and sleeps == []


async def test_openai_wrapper_refusal_is_output_error() -> None:
    refusal = SimpleNamespace(
        output_parsed=None,
        model="m",
        id="r",
        usage=None,
        output=[SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="no")])],
    )
    llm, _, _ = _llm([refusal])
    with pytest.raises(LLMOutputError, match="refusal: no"):
        await llm.generate(TrainingAnalysis, system="s", user="u", name="n")


def test_openai_wrapper_requires_key() -> None:
    with pytest.raises(LLMUnavailableError):
        OpenAIStructuredLLM(make_unit_settings())


def test_langgraph_conninfo_pins_schema() -> None:
    url = make_unit_settings().langgraph_conninfo()
    assert url.startswith("postgresql://") and url.endswith("options=-c%20search_path%3Dlanggraph")
