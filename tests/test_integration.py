"""The whole pipeline over the reference fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from race_bot.analysis.heuristic import HeuristicAnalyser
from race_bot.models.race_state import RacePhase, RaceState
from race_bot.models.tactics import TacticalPattern
from race_bot.pipeline.orchestrator import Orchestrator
from race_bot.sources.replay import ReplaySource

FIXTURE = Path(__file__).parent.parent / "fixtures" / "races" / "kustklassieker_2026.jsonl"


@pytest.fixture
async def finished_run():
    orchestrator = Orchestrator(
        source=ReplaySource(FIXTURE, speed=0),
        analyser=HeuristicAnalyser(),
        state=RaceState(race_name="Kustklassieker"),
        tick_seconds=0,
        max_pending=1,
    )
    state = await orchestrator.run()
    return orchestrator, state


async def test_all_posts_consumed(finished_run):
    _, state = finished_run
    assert state.posts_seen == 49


async def test_reaches_the_sprint(finished_run):
    _, state = finished_run
    assert state.phase is RacePhase.SPRINT
    assert state.km_to_go == 1.0


async def test_recognises_the_races_defining_moves(finished_run):
    """The crosswind split and the sprint are what this race was about."""
    orchestrator, _ = finished_run
    patterns = {e.pattern for e in orchestrator.tracker.all_events}
    assert TacticalPattern.ECHELONS_FORMING in patterns
    assert TacticalPattern.SPLIT_CONFIRMED in patterns
    assert TacticalPattern.LEADOUT_FORMING in patterns
    assert TacticalPattern.SPRINT_LAUNCHED in patterns


async def test_output_is_not_spam(finished_run):
    """Dedup must keep the event count far below the post count."""
    orchestrator, state = finished_run
    assert len(orchestrator.tracker.all_events) < state.posts_seen / 3


async def test_every_event_is_traceable_to_commentary(finished_run):
    orchestrator, _ = finished_run
    assert all(e.evidence_post_ids for e in orchestrator.tracker.all_events)


async def test_no_cobble_events_in_a_race_without_cobbles(finished_run):
    orchestrator, _ = finished_run
    patterns = {e.pattern for e in orchestrator.tracker.all_events}
    assert TacticalPattern.COBBLE_SECTOR_APPROACH not in patterns


async def test_replay_is_deterministic():
    async def run() -> list[str]:
        orchestrator = Orchestrator(
            source=ReplaySource(FIXTURE, speed=0),
            analyser=HeuristicAnalyser(),
            tick_seconds=0,
            max_pending=1,
        )
        await orchestrator.run()
        return [e.pattern.value for e in orchestrator.tracker.all_events]

    assert await run() == await run()


async def _patterns_with_chunk_size(chunk_size: int) -> list[str]:
    from tests.conftest import ChunkedSource

    posts = await ReplaySource(FIXTURE, speed=0).poll()
    orchestrator = Orchestrator(
        source=ChunkedSource(posts, chunk_size),
        analyser=HeuristicAnalyser(),
        tick_seconds=0,
        max_pending=1,
    )
    await orchestrator.run()
    return [e.pattern.value for e in orchestrator.tracker.all_events]


@pytest.mark.parametrize("chunk_size", [1, 3, 10])
async def test_results_do_not_depend_on_batch_size(chunk_size):
    """Whether posts arrive one at a time or in a burst must not change the read."""
    assert await _patterns_with_chunk_size(chunk_size) == await _patterns_with_chunk_size(49)
