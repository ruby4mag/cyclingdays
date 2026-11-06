"""Graph nodes.

Deterministic nodes: ``gather_context``, ``rest_day``, ``fallback``, ``abort``,
``finalize``. LLM nodes: ``training_analysis``, ``recovery_assessment``,
``objective_selection``, ``workout_planning``. An LLM node's output enters the
state only after schema validation and the deterministic guards. An LLM
failure (API down after retries, or output still invalid after corrections)
routes to ``fallback``.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.base import AgentOutcome
from app.agents.llm import StructuredLLM
from app.agents.recovery_analyst import assess_recovery
from app.agents.schemas import TrainingObjective, WorkoutDraft
from app.agents.training_analyst import analyse_training
from app.agents.workout_planner import plan_workout, select_objective
from app.database.models import AthleteProfile, CoachDecision, PlannedWorkout
from app.database.repositories import PlannedWorkoutRepository
from app.graph import guards
from app.graph.context import ContextError, build_context
from app.graph.fallback import fallback_plan
from app.graph.prompts import ALL_PROMPTS, DAILY_PLAN_VERSION
from app.graph.state import CoachState
from app.integrations.intervals_workouts import external_id_for, to_description
from app.services.training_metrics import TrainingMetricsError
from app.training.intensity import Intensity
from app.training.workout_validator import validate_workout

logger = logging.getLogger(__name__)

# Rows in these states record what happened; a new plan never overwrites them.
_FINAL_WORKOUT_STATUSES = ("completed", "missed", "skipped")


def _cap(label: str) -> Intensity:
    return Intensity[label.upper()]


def _failure_reason(node: str, outcome: AgentOutcome[Any]) -> str:
    if outcome.unavailable:
        return f"{node}: LLM unavailable ({outcome.unavailable[:300]})"
    return f"{node}: output rejected after corrections: {'; '.join(outcome.rejected)[:500]}"


@dataclass
class CoachNodes:
    sessions: async_sessionmaker[AsyncSession]
    llm: StructuredLLM | None
    max_corrections: int

    # --- deterministic -------------------------------------------------------

    async def gather_context(self, state: CoachState) -> dict[str, Any]:
        plan_date = dt.date.fromisoformat(state["plan_date"])
        try:
            context = await build_context(self.sessions, state["athlete_slug"], plan_date)
        except (ContextError, TrainingMetricsError) as exc:
            return {"route": "abort", "abort_reason": str(exc)}
        if context["athlete"]["ftp_watts"] is None:
            return {
                "context": context,
                "route": "abort",
                "abort_reason": "athlete FTP is unknown; workouts cannot be planned",
            }
        # Strictest of readiness and the long-term plan (guidance.py).
        cap = context["guidance"]["max_intensity"]
        update: dict[str, Any] = {
            "context": context,
            "deterministic_cap": cap,
            "effective_cap": cap,
        }
        # Safety stop: a rest cap needs no LLM. Otherwise analyse; without a
        # configured LLM the first LLM node routes to the fallback.
        update["route"] = "rest" if _cap(cap) <= Intensity.REST else "analyse"
        return update

    async def rest_day(self, state: CoachState) -> dict[str, Any]:
        context = state["context"]
        caps = (context.get("guidance") or {}).get("caps") or context["readiness"]["caps"]
        reasons = [c["reason"] for c in caps if c["max_intensity"] in ("rest", "recovery")]
        cap = state.get("effective_cap") or state.get("deterministic_cap", "rest")
        why = "; ".join(reasons) or f"maximum intensity is {cap}"
        objective = TrainingObjective(
            session_type="rest",
            target_duration_minutes=0,
            primary_goal="Rest",
            rationale=f"Rest day required by deterministic safety rules: {why}.",
            long_term_link="Respecting illness, pain and fatigue protects long-term progress.",
        )
        return {"objective": objective.model_dump(mode="json"), "workout": None, "outcome": "rest"}

    async def fallback(self, state: CoachState) -> dict[str, Any]:
        cap = _cap(state.get("effective_cap") or state["deterministic_cap"])
        reason = state.get("fallback_reason") or "LLM unavailable"
        max_minutes = (state["context"].get("guidance") or {}).get("max_minutes")
        objective, workout = fallback_plan(cap, reason, max_minutes)
        if workout is not None:
            ctx = guards.validation_context(state["context"], cap)
            problems = guards.workout_violations(
                workout,
                objective,
                cap,
                state["context"]["athlete"]["ftp_watts"],
                ctx,
                athlete_slug=state["athlete_slug"],
            )
            if problems:
                # Even the conservative plan breaks a weekly or load rule: rest.
                objective, workout = fallback_plan(
                    Intensity.REST, f"{reason}; fallback workout invalid: {'; '.join(problems)}"
                )
        logger.warning("coach_fallback_plan", extra={"reason": reason, "cap": cap.label})
        return {
            "objective": objective.model_dump(mode="json"),
            "workout": None if workout is None else workout.model_dump(mode="json"),
            "outcome": "fallback",
        }

    async def abort(self, state: CoachState) -> dict[str, Any]:
        logger.error("coach_plan_aborted", extra={"reason": state.get("abort_reason")})
        return {"outcome": "aborted", "objective": None, "workout": None}

    # --- LLM -----------------------------------------------------------------

    def _unavailable(self, node: str) -> dict[str, Any]:
        return {
            "route": "fallback",
            "fallback_reason": f"{node}: LLM not configured (OPENAI_API_KEY missing)",
        }

    async def training_analysis(self, state: CoachState) -> dict[str, Any]:
        if self.llm is None:
            return self._unavailable("training_analysis")
        out = await analyse_training(
            self.llm, state["context"], max_corrections=self.max_corrections
        )
        update: dict[str, Any] = {"calls": [c.as_dict() for c in out.calls]}
        if out.parsed is None:
            return {
                **update,
                "route": "fallback",
                "fallback_reason": _failure_reason("training_analysis", out),
            }
        return {**update, "route": "next", "training_analysis": out.parsed.model_dump(mode="json")}

    async def recovery_assessment(self, state: CoachState) -> dict[str, Any]:
        assert self.llm is not None
        out = await assess_recovery(
            self.llm,
            state["context"],
            state["training_analysis"] or {},
            max_corrections=self.max_corrections,
        )
        update: dict[str, Any] = {"calls": [c.as_dict() for c in out.calls]}
        if out.parsed is None:
            return {
                **update,
                "route": "fallback",
                "fallback_reason": _failure_reason("recovery_assessment", out),
            }
        deterministic = _cap(state["deterministic_cap"])
        effective = guards.effective_cap(deterministic, out.parsed)
        recommended = out.parsed.recommended_max_intensity
        if _cap(recommended) > deterministic:
            logger.warning(
                "llm_cap_clamped",
                extra={"recommended": recommended, "deterministic": deterministic.label},
            )
        update.update(
            recovery_assessment=out.parsed.model_dump(mode="json"),
            effective_cap=effective.label,
            route="rest" if effective <= Intensity.REST else "next",
        )
        return update

    async def objective_selection(self, state: CoachState) -> dict[str, Any]:
        assert self.llm is not None
        cap = _cap(state["effective_cap"])
        out = await select_objective(
            self.llm,
            state["context"],
            state["training_analysis"] or {},
            state["recovery_assessment"] or {},
            cap,
            max_corrections=self.max_corrections,
        )
        update: dict[str, Any] = {"calls": [c.as_dict() for c in out.calls]}
        if out.parsed is None:
            return {
                **update,
                "route": "fallback",
                "fallback_reason": _failure_reason("objective_selection", out),
            }
        update["objective"] = out.parsed.model_dump(mode="json")
        if out.parsed.session_type == "rest":
            return {**update, "route": "rest", "workout": None, "outcome": "rest"}
        return {**update, "route": "workout"}

    async def workout_planning(self, state: CoachState) -> dict[str, Any]:
        assert self.llm is not None and state["objective"] is not None
        cap = _cap(state["effective_cap"])
        objective = TrainingObjective.model_validate(state["objective"])
        out = await plan_workout(
            self.llm, state["context"], objective, cap, max_corrections=self.max_corrections
        )
        update: dict[str, Any] = {"calls": [c.as_dict() for c in out.calls]}
        if out.parsed is None:
            return {
                **update,
                "route": "fallback",
                "fallback_reason": _failure_reason("workout_planning", out),
            }
        return {
            **update,
            "route": "finalize",
            "workout": out.parsed.model_dump(mode="json"),
            "outcome": "planned",
        }

    # --- persistence -----------------------------------------------------------

    async def finalize(self, state: CoachState) -> dict[str, Any]:
        """Persist every LLM call and the daily decision in one transaction.

        Idempotent per graph thread: if the daily decision already exists
        (e.g. the run crashed after committing and was resumed), nothing is
        written twice.
        """
        thread_id = state["thread_id"]
        plan_date = dt.date.fromisoformat(state["plan_date"])
        async with self.sessions() as session, session.begin():
            existing = await session.scalar(
                select(CoachDecision.id).where(
                    CoachDecision.graph_thread_id == thread_id,
                    CoachDecision.decision_type == "daily_plan",
                )
            )
            if existing is not None:
                row = await PlannedWorkoutRepository(session).get_by_external_id(
                    external_id_for(state["athlete_slug"], state["plan_date"])
                )
                return {"decision_id": int(existing), "planned_workout_id": row and row.id}
            athlete_id = await session.scalar(
                select(AthleteProfile.id).where(AthleteProfile.slug == state["athlete_slug"])
            )
            if athlete_id is None:
                logger.error("coach_finalize_no_athlete", extra={"slug": state["athlete_slug"]})
                return {"decision_id": None}
            for call in state.get("calls", []):
                session.add(
                    CoachDecision(
                        athlete_id=athlete_id,
                        decision_date=plan_date,
                        decision_type=f"llm:{call['node']}",
                        graph_thread_id=thread_id,
                        graph_run_id=state["run_id"],
                        model_name=call["reported_model"] or call["configured_model"],
                        prompt_version=call["prompt_id"],
                        input_snapshot={
                            "payload": call["input_payload"],
                            "correction": call["correction"],
                            "prompt_sha256": call["prompt_sha256"],
                        },
                        output={
                            k: call[k]
                            for k in (
                                "output",
                                "accepted",
                                "attempt",
                                "violations",
                                "error",
                                "usage",
                            )
                        },
                        rationale=(call["output"] or {}).get("summary")
                        or (call["output"] or {}).get("rationale"),
                    )
                )
            accepted = [c for c in state.get("calls", []) if c["accepted"]]
            models = sorted({c["reported_model"] for c in accepted if c["reported_model"]})
            objective = state.get("objective") or {}
            decision = CoachDecision(
                athlete_id=athlete_id,
                decision_date=plan_date,
                decision_type="daily_plan",
                graph_thread_id=thread_id,
                graph_run_id=state["run_id"],
                model_name=", ".join(models) or None,
                prompt_version=DAILY_PLAN_VERSION,
                input_snapshot=state.get("context") or {},
                output={
                    "outcome": state.get("outcome"),
                    "deterministic_cap": state.get("deterministic_cap"),
                    "effective_cap": state.get("effective_cap"),
                    "training_analysis": state.get("training_analysis"),
                    "recovery_assessment": state.get("recovery_assessment"),
                    "objective": state.get("objective"),
                    "workout": state.get("workout"),
                    "fallback_reason": state.get("fallback_reason"),
                    "abort_reason": state.get("abort_reason"),
                    "prompt_versions": {p.name: p.id for p in ALL_PROMPTS},
                    "llm_calls": len(state.get("calls", [])),
                },
                rationale=objective.get("rationale")
                or state.get("abort_reason")
                or state.get("fallback_reason"),
            )
            session.add(decision)
            await session.flush()
            decision_id = decision.id
            planned_id, validation = await self._store_planned_workout(
                session, state, athlete_id, decision_id
            )
            decision.output = {**decision.output, "planned_workout_id": planned_id}
        logger.info(
            "coach_daily_plan",
            extra={
                "thread_id": thread_id,
                "outcome": state.get("outcome"),
                "session_type": objective.get("session_type"),
                "decision_id": decision_id,
            },
        )
        return {
            "decision_id": decision_id,
            "planned_workout_id": planned_id,
            "validation": validation,
        }

    async def _store_planned_workout(
        self,
        session: AsyncSession,
        state: CoachState,
        athlete_id: int,
        decision_id: int,
    ) -> tuple[int | None, dict[str, Any] | None]:
        """One ``planned_workouts`` row per athlete and day, updated on re-planning.

        The row is keyed by the stable external id used for publishing, so a
        re-planned day replaces the same Intervals.icu event. The final
        validator runs here once more on the exact workout that will be
        published; a failing workout is stored as ``rejected`` and is never
        published.
        """
        outcome = state.get("outcome")
        if outcome == "aborted" or not state.get("objective"):
            return None, None
        objective = TrainingObjective.model_validate(state["objective"])
        plan_date = dt.date.fromisoformat(state["plan_date"])
        external_id = external_id_for(state["athlete_slug"], state["plan_date"])
        repo = PlannedWorkoutRepository(session)
        row = await repo.get_by_external_id(external_id)
        if row is not None and row.status in _FINAL_WORKOUT_STATUSES:
            logger.warning(
                "planned_workout_final",
                extra={"external_id": external_id, "status": row.status},
            )
            return row.id, None

        cap = _cap(state.get("effective_cap") or state["deterministic_cap"])
        context = state["context"]
        values: dict[str, Any]
        validation: dict[str, Any] | None = None
        if state.get("workout") is None:
            values = {
                "status": "validated",
                "workout_type": "rest",
                "name": "Rest day",
                "planned_duration_s": 0,
                "planned_training_load": 0.0,
                "structure": {},
                "validation_result": {"valid": True, "rest": True},
            }
        else:
            draft = WorkoutDraft.model_validate(state["workout"])
            ctx = guards.validation_context(context, cap, objective.target_duration_minutes)
            workout = guards.to_structured(
                draft,
                objective,
                ftp_watts=context["athlete"]["ftp_watts"],
                athlete_slug=state["athlete_slug"],
                plan_date=plan_date,
            )
            result = validate_workout(workout, ctx)
            validation = {**result.as_dict(), "context": ctx.as_dict()}
            values = {
                "status": "validated" if result.valid else "rejected",
                "workout_type": objective.session_type,
                "name": workout.name,
                "planned_duration_s": workout.total_s,
                "planned_training_load": workout.estimate.tss,
                "ftp_reference_watts": workout.ftp_watts,
                "structure": {
                    "workout": workout.model_dump(mode="json"),
                    "intervals_description": to_description(workout),
                },
                "validation_result": validation,
            }
            if not result.valid:
                logger.error(
                    "planned_workout_rejected",
                    extra={"external_id": external_id, "errors": result.errors},
                )
        values.update(
            coach_decision_id=decision_id,
            objective=objective.primary_goal,
            workout_date=plan_date,
        )
        if values["workout_type"] == "rest":
            values["ftp_reference_watts"] = None
        if row is None:
            row = PlannedWorkout(athlete_id=athlete_id, external_id=external_id, **values)
            session.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await session.flush()
        return row.id, validation
