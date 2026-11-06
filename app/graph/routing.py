"""Conditional routing. Each node writes ``state["route"]``; these map it to
the next node. Unknown values raise, so a routing bug fails loudly rather
than silently skipping safety steps.
"""

from __future__ import annotations

from collections.abc import Hashable

from app.graph.state import CoachState

RouteMap = dict[Hashable, str]

AFTER_CONTEXT: RouteMap = {"analyse": "training_analysis", "rest": "rest_day", "abort": "abort"}
AFTER_ANALYSIS: RouteMap = {"next": "recovery_assessment", "fallback": "fallback"}
AFTER_RECOVERY: RouteMap = {
    "next": "objective_selection",
    "rest": "rest_day",
    "fallback": "fallback",
}
AFTER_OBJECTIVE: RouteMap = {
    "workout": "workout_planning",
    "rest": "finalize",
    "fallback": "fallback",
}
AFTER_WORKOUT: RouteMap = {"finalize": "finalize", "fallback": "fallback"}


def _route(state: CoachState, allowed: RouteMap) -> str:
    route = state.get("route")
    if route is None or route not in allowed:
        raise ValueError(f"invalid route {route!r}; expected one of {sorted(map(str, allowed))}")
    return route


def after_context(state: CoachState) -> str:
    return _route(state, AFTER_CONTEXT)


def after_analysis(state: CoachState) -> str:
    return _route(state, AFTER_ANALYSIS)


def after_recovery(state: CoachState) -> str:
    return _route(state, AFTER_RECOVERY)


def after_objective(state: CoachState) -> str:
    return _route(state, AFTER_OBJECTIVE)


def after_workout(state: CoachState) -> str:
    return _route(state, AFTER_WORKOUT)
