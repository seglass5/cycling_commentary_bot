from __future__ import annotations

import pytest

from race_bot.analysis.heuristic import MAX_CONFIDENCE, HeuristicAnalyser, infer_phase
from race_bot.models.race_state import GroupKind, RacePhase, RaceState
from race_bot.models.tactics import TacticalPattern
from race_bot.pipeline.window import ContextWindow


@pytest.fixture
def analyser():
    return HeuristicAnalyser()


async def analyse(analyser, posts, state=None):
    window = ContextWindow()
    for post in posts:
        window.add(post)
    return await analyser.analyse(
        window=window, state=state or RaceState(), new_posts=posts
    )


@pytest.mark.parametrize(
    ("km_to_go", "expected"),
    [
        (None, None),
        (1.0, RacePhase.SPRINT),
        (20, RacePhase.FINALE),
        (100, RacePhase.EARLY_ATTACKS),
        (180, RacePhase.EARLY_ATTACKS),
    ],
)
def test_infer_phase(km_to_go, expected):
    assert infer_phase(km_to_go, RacePhase.UNKNOWN) == expected


def test_phase_stays_in_controlled_chase_once_break_is_gone():
    assert infer_phase(100, RacePhase.BREAKAWAY_GONE) is RacePhase.CONTROLLED_CHASE


async def test_extracts_km_and_phase(analyser, make_normalised):
    result = await analyse(analyser, [make_normalised("With 20km to go the pace is high")])
    assert result.delta.km_to_go == 20
    assert result.delta.phase is RacePhase.FINALE


async def test_breakaway_and_peloton_proposed_together(analyser, make_normalised):
    result = await analyse(
        analyser,
        [make_normalised("A five-man move has gone clear, the gap is 45 seconds")],
    )
    kinds = {g.kind for g in result.delta.groups}
    assert kinds == {GroupKind.BREAKAWAY, GroupKind.PELOTON}


async def test_large_group_is_not_treated_as_a_breakaway(analyser, make_normalised):
    result = await analyse(
        analyser,
        [make_normalised("The leaders are chased by a bunch of 120 riders at 45 seconds")],
    )
    assert GroupKind.BREAKAWAY not in {g.kind for g in result.delta.groups}


async def test_detects_echelons(analyser, make_normalised):
    result = await analyse(analyser, [make_normalised("Echelons are forming across the road")])
    assert TacticalPattern.ECHELONS_FORMING in {e.pattern for e in result.events}


async def test_detects_crash(analyser, make_normalised):
    result = await analyse(analyser, [make_normalised("Crash in the bunch on the roundabout")])
    assert TacticalPattern.CRASH_DISRUPTION in {e.pattern for e in result.events}


async def test_leadout_only_fires_near_the_finish(analyser, make_normalised):
    far = await analyse(
        analyser, [make_normalised("The lead-out is taking shape with 80km to go")]
    )
    near = await analyse(
        analyser, [make_normalised("The lead-out is taking shape with 8km to go")]
    )
    assert TacticalPattern.LEADOUT_FORMING not in {e.pattern for e in far.events}
    assert TacticalPattern.LEADOUT_FORMING in {e.pattern for e in near.events}


async def test_distance_gated_rule_uses_state_when_post_has_no_distance(
    analyser, make_normalised
):
    state = RaceState(km_to_go=6)
    result = await analyse(
        analyser, [make_normalised("The lead-out train is lining up")], state=state
    )
    assert TacticalPattern.LEADOUT_FORMING in {e.pattern for e in result.events}


async def test_confidence_is_capped(analyser, make_normalised):
    result = await analyse(
        analyser,
        [make_normalised("Echelons forming, crash, the sprint is on with 1km to go")],
    )
    assert all(e.confidence <= MAX_CONFIDENCE for e in result.events)


async def test_events_carry_evidence(analyser, make_normalised):
    post = make_normalised("Echelons are forming across the road", post_id="evidence-1")
    result = await analyse(analyser, [post])
    assert all(e.evidence_post_ids == ["evidence-1"] for e in result.events)


async def test_rider_names_do_not_trigger_cobble_rule(analyser, make_normalised):
    """"Bergstrom" is a name, not a berg."""
    result = await analyse(
        analyser, [make_normalised("Tomas Bergstrom leads Mathieu Vanden Berghe")]
    )
    assert TacticalPattern.COBBLE_SECTOR_APPROACH not in {e.pattern for e in result.events}
