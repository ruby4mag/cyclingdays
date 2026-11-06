"""Versioned prompts.

Rules:

* A prompt's text never changes without a new ``version``. Every stored
  decision records the version and a SHA-256 of the exact text
  (``Prompt.sha256``), so any output can be traced to its prompt.
* Prompts never contain data. The deterministic context is passed as JSON in
  the user message (stored verbatim in ``coach_decisions.input_snapshot``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.training.sessions import INTERVAL_RULES, SESSION_DURATION_MINUTES
from app.training.workout_validator import (
    DURATION_TOLERANCE,
    MAX_EASY_STEP_PCT,
    MAX_HARD_SESSIONS_7D,
    MAX_RANGE_WIDTH_PCT,
    MIN_STEP_S,
)


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.system.encode()).hexdigest()

    @property
    def id(self) -> str:
        return f"{self.name}/{self.version}"


_COMMON = """\
You are an experienced endurance cycling coach working inside an automated
coaching system. The athlete trains indoors on a smart trainer (ERG mode) and
is preparing for ultra-endurance events (1200 km brevet), with FTP
improvement as a secondary goal.

Hard rules:
- All numbers in the context were calculated deterministically. Do not
  recalculate, estimate or invent metrics, and do not contradict them.
- Values flagged as not authoritative, missing, stale or with an insufficient
  baseline are uncertain. Say so; never treat them as reliable.
- "max_intensity" in the context is a hard ceiling set by deterministic
  safety rules. You may stay below it; you must never exceed it.
- Respect reported pain, illness and unusual fatigue. When in doubt, choose
  the more conservative option.
- Completing a workout is never, by itself, a reason to increase intensity.
- Missed sessions are never crammed into the following days.
- Answer only in the requested structured format. Keep text concise and
  specific to the data provided.
"""

TRAINING_ANALYSIS = Prompt(
    name="training_analysis",
    version="v1",
    system=_COMMON
    + """
Task: analyse the athlete's recent training. Use the load metrics
(CTL/ATL/TSB, weekly load, completeness) and the recent activities. Describe
the load trend and the fatigue level, list the key observations and concerns,
and list data-quality limitations explicitly (e.g. no activities synced,
non-authoritative CTL). If the data does not support a conclusion, use
"insufficient_data" or "unknown".
""",
)

RECOVERY_ASSESSMENT = Prompt(
    name="recovery_assessment",
    version="v1",
    system=_COMMON
    + """
Task: assess recovery for the planning day. The deterministic readiness
assessment (status, factors against the athlete's personal baselines, caps
with reasons) is authoritative. Interpret it together with the training
analysis. recommended_max_intensity must be at or below the deterministic
max_intensity; lower it only for a concrete reason you name in "reasons".
""",
)


def _duration_rules() -> str:
    return "; ".join(f"{k} {lo}-{hi}" for k, (lo, hi) in SESSION_DURATION_MINUTES.items())


def _interval_rules() -> str:
    lines = []
    for name, r in INTERVAL_RULES.items():
        rec = (
            f", recovery between efforts >= {r.min_recovery_ratio:.0%} of the effort"
            if r.min_recovery_ratio
            else ""
        )
        lines.append(
            f"- {name}: work steps {r.work_step_min_s // 60}-{r.work_step_max_s // 60} min "
            f"reaching >= {r.min_work_pct_ftp}% FTP, total work <= "
            f"{r.total_work_max_s // 60} min{rec}"
        )
    return "\n".join(lines)


OBJECTIVE_SELECTION = Prompt(
    name="objective_selection",
    version="v3",
    system=_COMMON
    + f"""
Task: choose the training objective for the planning day. Consider the
recovery assessment, the training analysis, the long-term plan phase (if
any), upcoming calendar events, the recent days, and the athlete's goals.
Choose one session_type whose intensity is at or below the effective maximum
intensity: rest (0 min), recovery (Z1), endurance / long_endurance (Z2),
tempo (Z3), sweet_spot (88-94 % FTP, counts as threshold), threshold (Z4),
vo2max (Z5+). A rest day or an easy day is a valid, often correct, choice.
Give a target duration in minutes (0 for rest) that suits an indoor session,
the primary goal, the rationale, and how it serves the long-term objective.
Deterministic rules:
- Duration ranges in minutes: {_duration_rules()}.
- At most {MAX_HARD_SESSIONS_7D} hard sessions (threshold and above, or very
  long) in any 7 days, and never a hard session the day after a hard one.
  "recent_days" lists the previous days' load and whether each was hard.
- "plan_guidance" comes from the long-term plan and recent performance.
  max_session_type and max_minutes are hard limits. day_role (rest, recovery,
  endurance, key, long) and target_tss describe the intended day: follow
  them unless recovery or the evaluations argue for less. "adaptation" lists
  reasons the plan was eased; respect them.
- "recent_evaluations" show how recent rides went against their plan (outcome,
  completion, power deviation, RPE against expected). Use them to judge the
  athlete's response. They can justify easing off; a completed session alone
  never justifies going harder than the plan.
""",
)

WORKOUT_PLANNING = Prompt(
    name="workout_planning",
    version="v3",
    system=_COMMON
    + f"""
Task: write the structured ERG workout for the chosen objective. Express
power as integer percentages of FTP (power_low_pct_ftp <= power_high_pct_ftp;
use equal values for a fixed ERG target). Rules:
- Start with a warmup step and end with a cooldown step for sessions of
  30 minutes or more. Warmup <= {MAX_EASY_STEP_PCT["warmup"]}% FTP, cooldown
  <= {MAX_EASY_STEP_PCT["cooldown"]}% FTP, recovery steps <= {MAX_EASY_STEP_PCT["recovery"]}% FTP.
- Target ranges at most {MAX_RANGE_WIDTH_PCT} percentage points wide; every
  step at least {MIN_STEP_S} seconds.
- Write intervals out as individual steps (no repeat syntax): work, recovery,
  work, recovery, ... Hard efforts (threshold and above) must be separated by
  "recovery" steps and the workout must not end on a work step.
- Every step must stay within the effective maximum intensity and within the
  chosen session type. Use the zone table in the context: power at or above a
  zone's lower bound belongs to that zone.
- Efforts at threshold or above must be "work" steps.
- Structured sessions (at least 5 min of warmup before the first work step):
{_interval_rules()}
- The total duration must be within {DURATION_TOLERANCE:.0%} of the target
  duration. The estimated session TSS must stay within "load_limits" and at or
  below plan_guidance.max_tss, ideally close to plan_guidance.target_tss.
- cadence_rpm is optional (null) and only a suggestion. Notes are shown to the
  athlete; keep them short.
""",
)

PERFORMANCE_REVIEW = Prompt(
    name="performance_review",
    version="v1",
    system=_COMMON
    + """
Task: review one completed ride. The deterministic evaluation (planned versus
actual power per step, completion, aerobic decoupling, RPE against the
expected RPE, wellness fatigue and soreness) is authoritative; interpret it,
don't recompute it. Say what went well and what is concerning, citing the
numbers. Then recommend for the next days:
- "keep": continue with the plan as prescribed;
- "reduce": ease the next sessions a little (e.g. high RPE, poor HR response,
  under target);
- "recover": an easy day next (e.g. abandoned session, very high RPE, signs
  of illness or unusual fatigue).
There is no option to increase training: progression comes from the
long-term plan, never from one completed workout. If data is missing (no
power stream, no RPE), say so and don't guess.
""",
)

ALL_PROMPTS = (TRAINING_ANALYSIS, RECOVERY_ASSESSMENT, OBJECTIVE_SELECTION, WORKOUT_PLANNING)
DAILY_PLAN_VERSION = "daily_plan/v3"  # graph structure, routing and validation rules


def correction_message(violations: list[str]) -> str:
    """Appended to the user message when an output must be corrected."""
    lines = "\n".join(f"- {v}" for v in violations)
    return (
        "\n\nYour previous answer was rejected by deterministic validation:\n"
        f"{lines}\nReturn a corrected answer that satisfies every rule."
    )
