from __future__ import annotations

from datetime import UTC, datetime

import pytest

from race_bot.models.race_state import (
    GapTrend,
    Group,
    GroupDelta,
    GroupKind,
    RacePhase,
    RaceState,
    StateDelta,
)
from race_bot.pipeline.state import apply_delta, find_matching_group, format_gap

NOW = datetime(2026, 4, 12, 14, 0, tzinfo=UTC)


def test_apply_delta_does_not_mutate_input():
    state = RaceState(phase=RacePhase.EARLY_ATTACKS, km_to_go=100)
    new_state, _ = apply_delta(state, StateDelta(km_to_go=90), now=NOW)
    assert state.km_to_go == 100
    assert new_state.km_to_go == 90


def test_unset_fields_leave_state_alone():
    """The core guard: a delta that mentions nothing destroys nothing."""
    state = RaceState(
        phase=RacePhase.FINALE,
        km_to_go=20,
        groups=[Group(kind=GroupKind.BREAKAWAY, riders=["A Rider"], gap_to_leader_s=0)],
    )
    new_state, changes = apply_delta(state, StateDelta(), now=NOW)
    assert new_state.phase is RacePhase.FINALE
    assert new_state.km_to_go == 20
    assert len(new_state.groups) == 1
    assert changes == []


def test_km_to_go_never_increases():
    """Commentary refers back to earlier points in the race constantly."""
    state = RaceState(km_to_go=30)
    new_state, changes = apply_delta(state, StateDelta(km_to_go=120), now=NOW)
    assert new_state.km_to_go == 30
    assert changes == []


def test_km_to_go_tolerates_rounding_noise():
    state = RaceState(km_to_go=30)
    new_state, _ = apply_delta(state, StateDelta(km_to_go=30.4), now=NOW)
    assert new_state.km_to_go == 30


def test_km_to_go_accepted_when_unknown():
    new_state, _ = apply_delta(RaceState(), StateDelta(km_to_go=150), now=NOW)
    assert new_state.km_to_go == 150


def test_group_matched_by_rider_overlap():
    state = RaceState(
        groups=[Group(kind=GroupKind.BREAKAWAY, riders=["A Rider", "B Rider", "C Rider"])]
    )
    delta = StateDelta(
        groups=[GroupDelta(kind=GroupKind.BREAKAWAY, riders_added=["A Rider"], gap_to_leader_s=90)]
    )
    new_state, _ = apply_delta(state, delta, now=NOW)
    assert len(new_state.groups) == 1
    assert new_state.groups[0].gap_to_leader_s == 90


def test_distinct_rider_sets_create_separate_groups():
    state = RaceState(groups=[Group(kind=GroupKind.BREAKAWAY, riders=["A Rider", "B Rider"])])
    delta = StateDelta(
        groups=[GroupDelta(kind=GroupKind.BREAKAWAY, riders_added=["Y Rider", "Z Rider"])]
    )
    new_state, _ = apply_delta(state, delta, now=NOW)
    assert len(new_state.groups) == 2


def test_unnamed_delta_matches_sole_group_of_kind():
    """"The break" is unambiguous when there is only one."""
    state = RaceState(groups=[Group(kind=GroupKind.BREAKAWAY, riders=["A Rider"], size=1)])
    delta = StateDelta(groups=[GroupDelta(kind=GroupKind.BREAKAWAY, gap_to_leader_s=45)])
    new_state, _ = apply_delta(state, delta, now=NOW)
    assert len(new_state.groups) == 1
    assert new_state.groups[0].gap_to_leader_s == 45


def test_unnamed_delta_is_ambiguous_with_two_groups_of_kind():
    state = RaceState(
        groups=[
            Group(kind=GroupKind.CHASE, riders=["A Rider"]),
            Group(kind=GroupKind.CHASE, riders=["B Rider"]),
        ]
    )
    delta = GroupDelta(kind=GroupKind.CHASE, gap_to_leader_s=30)
    assert find_matching_group(state, delta) is None


def test_stale_group_ref_falls_back_to_heuristics():
    state = RaceState(groups=[Group(kind=GroupKind.BREAKAWAY, riders=["A Rider"])])
    delta = GroupDelta(
        group_ref="nonexistent", kind=GroupKind.BREAKAWAY, riders_added=["A Rider"]
    )
    assert find_matching_group(state, delta) is state.groups[0]


def test_riders_removed():
    state = RaceState(
        groups=[Group(kind=GroupKind.BREAKAWAY, riders=["A Rider", "B Rider"], size=2)]
    )
    delta = StateDelta(
        groups=[
            GroupDelta(kind=GroupKind.BREAKAWAY, riders_removed=["B Rider"], size=1)
        ]
    )
    new_state, _ = apply_delta(state, delta, now=NOW)
    assert new_state.groups[0].riders == ["A Rider"]
    assert new_state.groups[0].size == 1


def test_dissolved_group_removed():
    state = RaceState(groups=[Group(kind=GroupKind.BREAKAWAY, riders=["A Rider"])])
    delta = StateDelta(
        groups=[GroupDelta(kind=GroupKind.BREAKAWAY, riders_added=["A Rider"], dissolved=True)]
    )
    new_state, changes = apply_delta(state, delta, now=NOW)
    assert new_state.groups == []
    assert any("no longer on the road" in c for c in changes)


def test_new_group_created_with_timestamps():
    delta = StateDelta(
        groups=[GroupDelta(kind=GroupKind.BREAKAWAY, riders_added=["A Rider", "B Rider"])]
    )
    new_state, changes = apply_delta(RaceState(), delta, now=NOW)
    group = new_state.groups[0]
    assert group.first_seen == NOW
    assert group.size == 2
    assert len(changes) == 1


def test_phase_change_reported():
    state = RaceState(phase=RacePhase.CONTROLLED_CHASE)
    _, changes = apply_delta(state, StateDelta(phase=RacePhase.FINALE), now=NOW)
    assert changes == ["phase: controlled_chase -> finale"]


def test_weather_notes_deduplicated():
    state = RaceState(weather_notes=["crosswind from the north"])
    new_state, changes = apply_delta(
        state, StateDelta(weather_notes=["crosswind from the north"]), now=NOW
    )
    assert new_state.weather_notes == ["crosswind from the north"]
    assert changes == []


def test_trend_updated():
    state = RaceState(groups=[Group(kind=GroupKind.PELOTON, gap_to_leader_s=100)])
    delta = StateDelta(
        groups=[GroupDelta(kind=GroupKind.PELOTON, trend=GapTrend.SHRINKING)]
    )
    new_state, _ = apply_delta(state, delta, now=NOW)
    assert new_state.groups[0].trend is GapTrend.SHRINKING


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, "unknown"), (0, "0s"), (45, "45s"), (105, "1'45\""), (200, "3'20\"")],
)
def test_format_gap(seconds, expected):
    assert format_gap(seconds) == expected


def test_leaders_is_group_with_smallest_gap():
    state = RaceState(
        groups=[
            Group(kind=GroupKind.PELOTON, gap_to_leader_s=90),
            Group(kind=GroupKind.BREAKAWAY, gap_to_leader_s=0),
        ]
    )
    assert state.leaders.kind is GroupKind.BREAKAWAY
