"""LangGraph orchestration of the daily coaching workflow (Phase 4).

``state.py`` (shared state), ``context.py`` (deterministic inputs),
``nodes.py``, ``routing.py``, ``workflow.py`` (graph + PostgreSQL
checkpointing), ``prompts.py`` (versioned prompts), ``guards.py``
(deterministic checks on LLM output) and ``fallback.py`` (safe plan).
"""
