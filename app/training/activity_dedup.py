"""Detect the same ride recorded twice (Phase 6).

With icTrainer on the trainer *and* a COROS watch recording, one ride can
reach Intervals.icu twice: once from icTrainer and once from COROS. Counting
both would double the training load. This module decides which recordings
are duplicates. It doesn't rely on Intervals.icu doing its own de-duplication,
because that isn't documented.

Rule (version ``DEDUP_VERSION``):

* Two activities are the **same session** when they are the same sport
  family (every ``*Ride`` type is cycling, other types must match exactly)
  and their time ranges overlap by at least ``MIN_OVERLAP_SHARE`` of the
  shorter one.
* Overlaps are transitive (A~B and B~C → one group).
* In each group one **primary** recording is kept:
  1. with power data;
  2. paired with a planned workout;
  3. the longest moving time;
  4. the lowest id (stable).

  Every other recording in the group is a duplicate of the primary.

Pure functions; no I/O.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

DEDUP_VERSION = "v1"
MIN_OVERLAP_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class Recording:
    id: int
    start: dt.datetime  # aware
    duration_s: int
    activity_type: str | None
    has_power: bool
    paired: bool
    moving_s: int

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(seconds=self.duration_s)


def sport_family(activity_type: str | None) -> str:
    t = (activity_type or "").strip()
    if "Ride" in t or t in {"Velomobile", "Handcycle"}:
        return "cycling"
    return t or "unknown"


def same_session(a: Recording, b: Recording) -> bool:
    if sport_family(a.activity_type) != sport_family(b.activity_type):
        return False
    shorter = min(a.duration_s, b.duration_s)
    if shorter <= 0:
        return False
    overlap = (min(a.end, b.end) - max(a.start, b.start)).total_seconds()
    return overlap >= MIN_OVERLAP_SHARE * shorter


def _rank(r: Recording) -> tuple[bool, bool, int, int]:
    return (r.has_power, r.paired, r.moving_s, -r.id)


def find_duplicates(recordings: Iterable[Recording]) -> dict[int, int]:
    """Map duplicate id → primary id. Activities without a duplicate are absent."""
    items: Sequence[Recording] = sorted(recordings, key=lambda r: (r.start, r.id))
    parent = {r.id: r.id for r in items}

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if b.start >= a.end:
                break  # sorted by start: no later recording can overlap a
            if same_session(a, b):
                parent[root(b.id)] = root(a.id)

    groups: dict[int, list[Recording]] = {}
    for r in items:
        groups.setdefault(root(r.id), []).append(r)
    result: dict[int, int] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        primary = max(members, key=_rank)
        for m in members:
            if m.id != primary.id:
                result[m.id] = primary.id
    return result
