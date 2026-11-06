# Coaching graph (LangGraph + OpenAI)

_Last updated: Phase 7 (2026-11-05). Graph version **`daily_plan/v3`**._

The daily workflow plans one day for one athlete. The LLM interprets and
chooses; it never calculates. Deterministic code decides what the LLM is
**allowed** to choose, and validates everything it returns.

## 1. Graph

```
START → gather_context ─┬─ analyse → training_analysis → recovery_assessment ─┬─ next → objective_selection ─┬─ workout → workout_planning → finalize → END
                        │                     │                                │                            ├─ rest ─────────────────────→ finalize
                        │                     │ fallback                       ├─ rest → rest_day → finalize│
                        ├─ rest → rest_day ───┼──────────────────→ finalize    │                            │ fallback
                        └─ abort → abort ─────┼──────────────────→ finalize    │ fallback                   │
                                              └──→ fallback ←──────────────────┴────────────────────────────┘ (and workout_planning)
                                                       └──→ finalize
```

| Node | Kind | Does |
|---|---|---|
| `gather_context` | deterministic | Runs the Phase 3 engine for the plan date (`TrainingMetricsService`) and builds the context (§3). Routes **abort** (no athlete or no FTP), **rest** (deterministic cap = rest, e.g. illness or pain: no LLM involved) or **analyse** |
| `training_analysis` | LLM | Interprets load and recent activities → `TrainingAnalysis` |
| `recovery_assessment` | LLM | Interprets readiness → `RecoveryAssessment`. Its `recommended_max_intensity` can only **lower** the cap; a higher value is clamped and logged (`llm_cap_clamped`) |
| `objective_selection` | LLM + guard | Picks the session type and duration → `TrainingObjective`. Guard: session intensity ≤ effective cap; rest = 0 min; duration within the type's range; no hard session after a hard day or beyond 3 per 7 days |
| `workout_planning` | LLM + guard | Writes the ERG steps in % FTP → `WorkoutDraft`. Guard: the full Phase 5 validator ([WORKOUTS.md](WORKOUTS.md)) |
| `rest_day` / `fallback` / `abort` | deterministic | Safe outcomes; see below |
| `finalize` | deterministic | Writes all decisions and the day's `planned_workouts` row (validated once more) in one transaction; idempotent per thread |

**`workout_planning` guard.** Since Phase 5 this is the full workout
validator, the same one the publisher runs: power, duration, interval
structure, recovery structure, readiness, weekly distribution and recent
load. So the LLM is corrected against exactly the rules that decide
publication. A fallback workout that fails a weekly or load rule becomes rest.

**The safe deterministic outcomes:**
- `rest_day`: a rest decision whose reason comes from the caps.
- `fallback`: a conservative plan within the cap (§5).
- `abort`: no workout; the reason is recorded.

Routing is explicit (`app/graph/routing.py`). An unknown route raises instead
of silently skipping a step.

## 2. Structured outputs and validation

Schemas live in `app/agents/schemas.py`. All fields are required (OpenAI
strict mode), extra fields are forbidden, and bounds are enforced: for
example, power is an integer % of FTP from 30 to 160, with low ≤ high.

Every answer passes three checks, in order:

1. **OpenAI strict JSON schema.** `responses.parse(text_format=…)`.
2. **Pydantic validation on receipt.** A failure is an `LLMOutputError`.
3. **Deterministic guards** (`app/graph/guards.py`).

A rejection sends the violations back to the model as a correction
(`LLM_MAX_CORRECTIONS`, default 2). If the output is still invalid, the run
uses the **fallback**. Invalid output is never accepted, so the LLM has no
path around the guards. Tests cover each guard, the correction loop, clamping
and fallback.

## 3. Context (deterministic input)

`app/graph/context.py` collects:

* the athlete profile;
* the Phase 3 readiness assessment, with factors and caps;
* load (latest CTL/ATL/TSB with authoritative flags, 8 ISO weeks, coverage);
* activities from the last 14 days, with our TSS and IF;
* the next 7 days of the calendar;
* the plan phase and the power-zone table;
* **`guidance`** (Phase 7): the day's limits, i.e. the strictest of readiness,
  the long-term plan's prescription, and the adaptation from recent rides.
  The graph's deterministic cap is `guidance.max_intensity`. See
  [ADAPTIVE_COACHING.md](ADAPTIVE_COACHING.md) §5;
* `recent_evaluations`: rides of the last 14 days compared with their plan.

It is stored verbatim as the `daily_plan` decision's `input_snapshot`. Each
LLM call stores its exact input payload. The JSON is rendered with sorted
keys, so identical inputs give identical prompts.

## 4. Persistence

**Checkpoints.** `AsyncPostgresSaver` (`langgraph-checkpoint-postgres`) keeps
a checkpoint after every node:

* The tables live in schema **`langgraph`**. The connection pins
  `search_path=langgraph`, `setup()` is idempotent, and Alembic never
  manages these tables.
* The thread id is `daily-plan:<athlete>:<date>`:
  * A completed thread returns its stored result (`already_planned`) with no
    LLM calls and no new rows.
  * An interrupted thread **resumes** from the last checkpoint, and completed
    LLM nodes are not repeated (tested by killing a run mid-graph).
  * `--force` starts a new thread (`…:run-<id>`).

**Decisions (`coach.coach_decisions`).** There's one row per LLM call,
accepted or rejected:

| Column | Value |
|---|---|
| `decision_type` | `llm:<node>` |
| `model_name` | the model the API **reported** |
| `prompt_version` | e.g. `objective_selection/v1` |
| `input_snapshot` | the payload, the correction text and the prompt SHA-256 |
| `output` | the output, accepted flag, violations, error and token usage |

There's also one `daily_plan` row per run:

| Column | Value |
|---|---|
| `model_name` | the models that were used |
| `prompt_version` | `daily_plan/v3` |
| `input_snapshot` | the full context |
| `output` | outcome, deterministic and effective caps, all accepted outputs, the workout, fallback or abort reason, and per-node prompt versions |

## 5. Failures and retries

| Failure | Handling |
|---|---|
| Timeouts, network errors, 429, 5xx, 408/409 | `OpenAIStructuredLLM` retries with exponential backoff and jitter, up to `OPENAI_MAX_ATTEMPTS` (3). The SDK's own retries are off, so there's one policy and one log line (`llm_request_retry`) |
| Retries exhausted, or auth/bad-request error (not retried) | `LLMUnavailableError` → `fallback` |
| Refusal, unparseable or schema-invalid output | `LLMOutputError` → correction loop → `fallback` |
| Guard violation | correction loop → `fallback` |
| `OPENAI_API_KEY` not set | `fallback`, with the reason recorded |
| Unexpected exception (bug, DB outage) | the run fails; the checkpoint allows resuming |

**The fallback plan** (`app/graph/fallback.py`) is deterministic and
conservative. A rest cap gives rest. A recovery cap gives a 40-min Z1 spin.
Anything higher gives 60 min of Z2 endurance. It never uses the full cap,
because the LLM's judgement is missing. Tests prove it passes the guards for
every cap and FTP.

## 6. Prompts

`app/graph/prompts.py` has four graph prompts (`training_analysis/v1`,
`recovery_assessment/v1`, `objective_selection/v3`, `workout_planning/v3`;
rule text is generated from the validator constants; v3 adds the plan
guidance and recent evaluations) and `performance_review/v1`, used by the
activity processor (Phase 7). They share a preamble with the hard rules:

* Never recalculate metrics.
* Treat non-authoritative data as uncertain.
* `max_intensity` is a ceiling.
* Respect pain, illness and fatigue.
* Completing a workout is never, by itself, a reason to progress.
* Never cram missed sessions into the following days.

Changing a prompt's text requires a new version. The SHA-256 of the exact
text is stored with every call.

## 7. Configuration

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | — | secret |
| `OPENAI_MODEL` | `gpt-6.1-sol` | Any model with structured outputs. Chosen from `/v1/models` on 2026-11-05. It has no dated snapshot, so the reported model is stored with each decision. Use a dated snapshot (e.g. `gpt-5.5-2026-04-23`) to pin behaviour |
| `OPENAI_BASE_URL` | — | optional |
| `OPENAI_TIMEOUT_SECONDS` | `300` | per request; reasoning models are slow |
| `OPENAI_MAX_ATTEMPTS` | `3` | transient-error attempts per call |
| `OPENAI_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | `2` / `60` | |
| `LLM_MAX_CORRECTIONS` | `2` | correction rounds before the fallback |

LangSmith is installed as a LangGraph dependency, but tracing is **off**
(no `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2` set). Nothing leaves the VM
except the OpenAI and Intervals.icu API calls.

## 8. Publishing

Publishing (Phase 5) runs after the graph, in `scripts.plan_day` or
`scripts.publish_workout`, not inside it. It's an external side effect with
its own idempotency (stable `external_id`, upsert, verification) and its own
audit, so a failed publish is repaired by re-running it, without touching the
LLM or the checkpoints. See [WORKOUTS.md](WORKOUTS.md).
