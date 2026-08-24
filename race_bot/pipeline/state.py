"""Applying proposed deltas to race state.

The model proposes, this module disposes. Every rule here exists to stop one
specific failure mode of letting a language model hold state directly:

- Unset fields mean "no change", never "unknown, so wipe it". A response that
  forgets to mention the breakaway does not delete the breakaway.
- Groups are matched to existing groups by rider overlap, so a group keeps its
  identity across the race instead of being recreated every tick.
- km-to-go never increases, because commentary routinely refers back to earlier
  points in the race ("he attacked at 40km to go") long after passing them.
"""

from __future__ import annotations

from datetime import datetime

from race_bot.models.race_state import (
    GapTrend,
    Group,
    GroupDelta,
    RaceState,
    StateDelta,
)

RIDER_OVERLAP_THRESHOLD = 0.3
"""Jaccard similarity above which two rider sets are taken to be the same group."""

KM_REGRESSION_TOLERANCE_KM = 0.5
"""Small increases are rounding noise between sources; larger ones are rejected."""


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _normalise_riders(riders: list[str]) -> set[str]:
    return {r.strip().lower() for r in riders if r.strip()}


def find_matching_group(state: RaceState, delta: GroupDelta) -> Group | None:
    """Which existing group, if any, this delta is talking about."""
    if delta.group_ref:
        matched = state.group_by_id(delta.group_ref)
        if matched is not None:
            return matched
        # A stale or invented ref falls through to the heuristics rather than
        # silently creating a duplicate group.

    same_kind = state.groups_of_kind(delta.kind)
    if not same_kind:
        return None

    proposed = _normalise_riders(delta.riders_added + delta.riders_removed)
    if proposed:
        scored = [(g, _jaccard(_normalise_riders(g.riders), proposed)) for g in same_kind]
        best, score = max(scored, key=lambda pair: pair[1])
        if score >= RIDER_OVERLAP_THRESHOLD:
            return best

    # No names to go on. Safe only when the reference is unambiguous — there is
    # exactly one group of this kind, so "the break" can only mean that one.
    if len(same_kind) == 1 and not proposed:
        return same_kind[0]

    return None


def _apply_group_delta(
    state: RaceState, delta: GroupDelta, now: datetime, changes: list[str]
) -> None:
    existing = find_matching_group(state, delta)

    if delta.dissolved:
        if existing is not None:
            state.groups.remove(existing)
            changes.append(f"{existing.label} no longer on the road")
        return

    if existing is None:
        group = Group(
            kind=delta.kind,
            riders=list(dict.fromkeys(delta.riders_added)),
            teams_present=list(dict.fromkeys(delta.teams_present)),
            size=delta.size if delta.size is not None else (len(delta.riders_added) or None),
            gap_to_leader_s=delta.gap_to_leader_s,
            trend=delta.trend or GapTrend.UNKNOWN,
            first_seen=now,
            last_updated=now,
        )
        state.groups.append(group)
        gap = f" at {format_gap(group.gap_to_leader_s)}" if group.gap_to_leader_s else ""
        changes.append(f"new {group.label}{gap}")
        return

    removed = _normalise_riders(delta.riders_removed)
    if removed:
        existing.riders = [r for r in existing.riders if r.strip().lower() not in removed]

    for rider in delta.riders_added:
        if rider.strip().lower() not in _normalise_riders(existing.riders):
            existing.riders.append(rider)

    for team in delta.teams_present:
        if team not in existing.teams_present:
            existing.teams_present.append(team)

    if delta.size is not None and delta.size != existing.size:
        changes.append(f"{existing.label}: size {existing.size} -> {delta.size}")
        existing.size = delta.size
    elif existing.size is None and existing.riders:
        existing.size = len(existing.riders)

    if delta.gap_to_leader_s is not None and delta.gap_to_leader_s != existing.gap_to_leader_s:
        before = format_gap(existing.gap_to_leader_s)
        changes.append(
            f"{existing.label}: gap {before} -> {format_gap(delta.gap_to_leader_s)}"
        )
        existing.gap_to_leader_s = delta.gap_to_leader_s

    if delta.trend is not None and delta.trend is not existing.trend:
        existing.trend = delta.trend

    existing.last_updated = now


def apply_delta(
    state: RaceState, delta: StateDelta, *, now: datetime
) -> tuple[RaceState, list[str]]:
    """Apply a delta to a copy of `state`.

    Returns the new state and a list of human-readable changes. The input state
    is never mutated, so a rejected delta leaves nothing behind.
    """
    new_state = state.model_copy(deep=True)
    changes: list[str] = []

    if delta.phase is not None and delta.phase is not new_state.phase:
        changes.append(f"phase: {new_state.phase.value} -> {delta.phase.value}")
        new_state.phase = delta.phase

    if delta.km_to_go is not None:
        current = new_state.km_to_go
        if current is None or delta.km_to_go <= current + KM_REGRESSION_TOLERANCE_KM:
            if current != delta.km_to_go:
                changes.append(f"km to go: {_format_km(current)} -> {delta.km_to_go:g}")
            new_state.km_to_go = (
                min(delta.km_to_go, current) if current is not None else delta.km_to_go
            )
        # Else: a reference back to an earlier point in the race. Ignored.

    for group_delta in delta.groups:
        _apply_group_delta(new_state, group_delta, now, changes)

    for note in delta.weather_notes:
        if note not in new_state.weather_notes:
            new_state.weather_notes.append(note)
            changes.append(f"conditions: {note}")

    new_state.updated_at = now
    return new_state, changes


def format_gap(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}'{remainder:02d}\""


def _format_km(km: float | None) -> str:
    return "unknown" if km is None else f"{km:g}"
