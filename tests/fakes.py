"""Test doubles: a scripted LLM and an in-memory Intervals.icu calendar."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

import httpx2 as httpx
from pydantic import BaseModel, ValidationError

from app.agents.llm import LLMOutputError, LLMResult


class ScriptedLLM:
    """Returns scripted answers per node name, in order.

    An item may be a dict (validated into the requested schema), a model
    instance, or an exception to raise. The last item repeats when the script
    runs out, so "always fails" needs only one entry.
    """

    def __init__(self, script: dict[str, list[Any]], model: str = "test-model-1") -> None:
        self.script = script
        self.model = model
        self.calls: dict[str, int] = defaultdict(int)
        self.prompts: list[tuple[str, str]] = []

    @property
    def configured_model(self) -> str:
        return self.model

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    async def generate[T: BaseModel](
        self, schema: type[T], *, system: str, user: str, name: str
    ) -> LLMResult[T]:
        items = self.script[name]
        item = items[min(self.calls[name], len(items) - 1)]
        self.calls[name] += 1
        self.prompts.append((name, user))
        if isinstance(item, BaseException):
            raise item
        try:
            parsed = item if isinstance(item, schema) else schema.model_validate(item)
        except ValidationError as exc:
            raise LLMOutputError(str(exc)) from exc
        return LLMResult(
            parsed=parsed,
            model=self.model + "-2026-01-01",
            response_id="resp_x",
            input_tokens=100,
            output_tokens=50,
            attempts=1,
            duration_ms=1.0,
        )


ANALYSIS = {
    "summary": "Consistent endurance riding.",
    "load_trend": "maintaining",
    "fatigue_level": "low",
    "key_observations": ["regular rides"],
    "concerns": [],
    "data_quality_notes": [],
}


def recovery(max_intensity: str = "vo2max") -> dict[str, Any]:
    return {
        "summary": "Recovered.",
        "recovery_state": "well_recovered",
        "recommended_max_intensity": max_intensity,
        "reasons": [],
    }


def objective(session_type: str = "sweet_spot", minutes: int = 75) -> dict[str, Any]:
    return {
        "session_type": session_type,
        "target_duration_minutes": minutes,
        "primary_goal": "Raise FTP",
        "rationale": "Fresh and well recovered.",
        "long_term_link": "Sustainable power for the brevet.",
    }


def step(kind: str, minutes: float, low: int, high: int) -> dict[str, Any]:
    return {
        "kind": kind,
        "duration_seconds": int(minutes * 60),
        "power_low_pct_ftp": low,
        "power_high_pct_ftp": high,
        "cadence_rpm": None,
        "note": "",
    }


def sweet_spot_workout() -> dict[str, Any]:
    steps = [step("warmup", 15, 50, 60)]
    for _ in range(3):
        steps += [step("work", 12, 88, 92), step("recovery", 5, 50, 55)]
    steps.append(step("cooldown", 9, 45, 55))
    return {"name": "Sweet spot 3x12", "description": "3 x 12 min at 88-92 %", "steps": steps}


def endurance_workout(minutes: int = 60) -> dict[str, Any]:
    return {
        "name": f"Endurance {minutes}'",
        "description": "Z2",
        "steps": [
            step("warmup", 10, 50, 60),
            step("steady", minutes - 20, 62, 70),
            step("cooldown", 10, 45, 55),
        ],
    }


# --- Intervals.icu calendar (publishing) ---------------------------------------------

_STEP_LINE = re.compile(
    r"^- (?P<cue>[A-Za-z ]+?) (?P<dur>(?:\d+h)?(?:\d+m)?(?:\d+s)?) "
    r"(?P<low>\d+)(?:-(?P<high>\d+))?%(?: (?P<rpm>\d+)rpm)?$"
)


def _parse_duration(text: str) -> int:
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def parse_workout_text(description: str) -> dict[str, Any]:
    """The subset of the Intervals.icu syntax we emit, parsed the way the real API
    did on 2026-11-05 (see app/integrations/intervals_workouts.py)."""
    steps: list[dict[str, Any]] = []
    notes: list[str] = []
    for line in description.splitlines():
        match = _STEP_LINE.match(line.strip())
        if match is None:
            if line.strip():
                notes.append(line.strip())
            continue
        low, high = int(match["low"]), match["high"]
        power: dict[str, Any] = (
            {"units": "%ftp", "value": low}
            if high is None
            else {"units": "%ftp", "start": low, "end": int(high)}
        )
        step: dict[str, Any] = {
            "text": match["cue"],
            "duration": _parse_duration(match["dur"]),
            "power": power,
        }
        if match["rpm"]:
            step["cadence"] = {"units": "rpm", "value": int(match["rpm"])}
        steps.append(step)
    return {"steps": steps, "description": "\n".join(notes)}


class FakeCalendar:
    """In-memory Intervals.icu calendar behind an httpx MockTransport."""

    def __init__(self, athlete_id: str = "i1", ftp: int | None = 175) -> None:
        self.athlete_id = athlete_id
        self.ftp = ftp
        self.events: dict[int, dict[str, Any]] = {}
        self.next_id = 1000
        self.requests: list[tuple[str, str]] = []
        self.corrupt_parse = False  # simulate Intervals.icu misreading a workout

    def add(self, **event: Any) -> dict[str, Any]:
        self.next_id += 1
        stored = {"id": self.next_id, "category": "WORKOUT", "type": "Ride", **event}
        if "description" in stored:
            stored["workout_doc"] = parse_workout_text(stored["description"])
        self.events[self.next_id] = stored
        return stored

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/api/v1/athlete/{self.athlete_id}")
        self.requests.append((request.method, path))
        if request.method == "GET" and path == "":
            sport = [{"types": ["Ride", "VirtualRide"], "ftp": self.ftp}]
            return httpx.Response(200, json={"id": self.athlete_id, "sportSettings": sport})
        if request.method == "GET" and path == "/events":
            oldest = request.url.params["oldest"]
            newest = request.url.params["newest"]
            category = request.url.params.get("category")
            found = [
                {k: v for k, v in e.items() if k != "workout_doc"}
                for e in self.events.values()
                if oldest <= e["start_date_local"][:10] <= newest
                and (category is None or e["category"] in category.split(","))
            ]
            return httpx.Response(200, json=found)
        if request.method == "POST" and path == "/events/bulk":
            assert request.url.params.get("upsert") == "true"
            out = []
            for body in json.loads(request.content):
                match = [
                    e for e in self.events.values() if e.get("external_id") == body["external_id"]
                ]
                if match:
                    match[0].update(body)
                    match[0]["workout_doc"] = parse_workout_text(body["description"])
                    event = match[0]
                else:
                    event = self.add(**body)
                if self.corrupt_parse:
                    event["workout_doc"]["steps"][0]["duration"] += 60
                out.append(event)
            return httpx.Response(200, json=out)
        m = re.fullmatch(r"/events/(\d+)", path)
        if m:
            event_id = int(m[1])
            if event_id not in self.events:
                return httpx.Response(404, json={"error": "not found"})
            if request.method == "GET":
                return httpx.Response(200, json=self.events[event_id])
            if request.method == "DELETE":
                del self.events[event_id]
                return httpx.Response(200, json={})
        return httpx.Response(500, json={"error": f"unexpected {request.method} {path}"})
