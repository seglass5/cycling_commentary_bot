"""Phase 4: the tactics agent, its guards, and the gate in front of it."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from race_bot.agents.tactics import TacticsAgent, build_prompt, validate_reading
from race_bot.analysis.heuristic import HeuristicAnalyser
from race_bot.analysis.two_stage import (
    GAP_SHIFT_SECONDS,
    TwoStageAnalyser,
    meaningful_change,
)
from race_bot.models.race_state import (
    Group,
    GroupDelta,
    GroupKind,
    RacePhase,
    RaceState,
    StateDelta,
)
from race_bot.models.tactics import (
    EventStatus,
    ProposedEvent,
    TacticalEvent,
    TacticalPattern,
    TacticalReading,
)
from race_bot.pipeline.window import ContextWindow

SALIENT = "Callum Wright attacks out of the breakaway with 24km to go"


def reading_model(payload: dict, captured: list[ModelMessage] | None = None) -> FunctionModel:
    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if captured is not None:
            captured.extend(messages)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    return FunctionModel(handler)


def failing_model(exc: Exception) -> FunctionModel:
    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise exc

    return FunctionModel(handler)


def proposed(pattern=TacticalPattern.SOLO_BID, *, evidence=("p1",), **kwargs) -> dict:
    return {
        "pattern": pattern.value,
        "headline": kwargs.pop("headline", "Wright goes alone"),
        "explanation": kwargs.pop("explanation", "A solo bid against organised teams."),
        "confidence": kwargs.pop("confidence", 0.7),
        "evidence_post_ids": list(evidence),
        **kwargs,
    }


def evidence_aware_model(pattern=TacticalPattern.SOLO_BID) -> FunctionModel:
    """Cites a post id actually present in the prompt.

    A stub citing a made-up id gets correctly rejected by validation, which is
    useless for testing anything downstream of it.
    """
    import re

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        text = " ".join(
            getattr(part, "content", "")
            for message in messages
            for part in getattr(message, "parts", [])
            if isinstance(getattr(part, "content", None), str)
        )
        ids = re.findall(r"\(([\w.-]+)\)", text)
        payload = {"events": [proposed(pattern, evidence=ids[-1:] or ["unknown"])]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    return FunctionModel(handler)


@pytest.fixture
def window(make_normalised):
    window = ContextWindow()
    window.add(make_normalised(SALIENT, post_id="p1"))
    return window


@pytest.fixture
def state():
    return RaceState(phase=RacePhase.FINALE, km_to_go=24)


async def run(agent, window, state):
    return await agent.analyse(window=window, state=state, new_posts=window.posts)


# --- evidence validation -------------------------------------------------


def test_valid_evidence_survives(window, state):
    reading = TacticalReading(events=[ProposedEvent(**proposed())])
    outcome = validate_reading(reading, window=window, state=state)

    assert len(outcome.events) == 1
    assert outcome.events[0].evidence_post_ids == ["p1"]
    assert outcome.rejected == []


def test_unevidenced_event_is_discarded(window, state):
    """A confident read citing nothing is exactly what must not reach the user."""
    reading = TacticalReading(events=[ProposedEvent(**proposed(evidence=()))])
    outcome = validate_reading(reading, window=window, state=state)

    assert outcome.events == []
    assert "no valid evidence" in outcome.rejected[0]


def test_invented_evidence_is_discarded(window, state):
    reading = TacticalReading(events=[ProposedEvent(**proposed(evidence=("p999",)))])
    outcome = validate_reading(reading, window=window, state=state)

    assert outcome.events == []
    assert "no valid evidence" in outcome.rejected[0]


def test_partially_invented_evidence_is_trimmed_not_dropped(window, state):
    reading = TacticalReading(events=[ProposedEvent(**proposed(evidence=("p1", "p999")))])
    outcome = validate_reading(reading, window=window, state=state)

    assert len(outcome.events) == 1
    assert outcome.events[0].evidence_post_ids == ["p1"]
    assert any("p999" in r for r in outcome.rejected)


def test_off_phase_pattern_is_kept_but_recorded(window):
    """Our own phase tracking may be wrong; suppressing silently would hide that."""
    early = RaceState(phase=RacePhase.EARLY_ATTACKS, km_to_go=180)
    reading = TacticalReading(
        events=[ProposedEvent(**proposed(TacticalPattern.LEADOUT_FORMING))]
    )
    outcome = validate_reading(reading, window=window, state=early)

    assert len(outcome.events) == 1
    assert "leadout_forming" in outcome.off_phase[0]


def test_km_to_go_is_stamped_from_state(window, state):
    reading = TacticalReading(events=[ProposedEvent(**proposed())])
    outcome = validate_reading(reading, window=window, state=state)
    assert outcome.events[0].km_to_go == 24


def test_confidence_outside_range_is_rejected_by_the_schema():
    with pytest.raises(ValueError):
        ProposedEvent(**proposed(confidence=1.5))


def test_empty_reading_is_fine(window, state):
    outcome = validate_reading(TacticalReading(), window=window, state=state)
    assert outcome.events == []
    assert outcome.rejected == []


# --- the agent -----------------------------------------------------------


async def test_agent_returns_validated_events(window, state):
    agent = TacticsAgent(reading_model({"events": [proposed()]}))
    result = await run(agent, window, state)

    assert len(result.events) == 1
    assert result.events[0].pattern is TacticalPattern.SOLO_BID
    assert agent.stats.calls == 1


async def test_agent_returns_no_state_delta(window, state):
    """Race state is the situation agent's concern."""
    agent = TacticsAgent(reading_model({"events": [proposed()]}))
    assert (await run(agent, window, state)).delta.is_empty


async def test_agent_records_rejections_as_notes(window, state):
    agent = TacticsAgent(reading_model({"events": [proposed(evidence=("ghost",))]}))
    result = await run(agent, window, state)

    assert result.events == []
    assert any("no valid evidence" in n for n in agent.stats.notes)


async def test_agent_survives_api_failure(window, state):
    agent = TacticsAgent(failing_model(RuntimeError("azure down")))
    result = await run(agent, window, state)

    assert result.events == []
    assert agent.stats.failures == 1


async def test_prompt_lists_open_events(window, state):
    """The agent must know what is tracked, or it will re-report it."""
    captured: list[ModelMessage] = []
    agent = TacticsAgent(reading_model({"events": []}, captured))
    agent.open_events = [
        TacticalEvent(
            pattern=TacticalPattern.BREAKAWAY_ESTABLISHED,
            headline="Five clear",
            participants=["Callum Wright"],
        )
    ]
    await run(agent, window, state)

    instructions = " ".join(
        m.instructions or "" for m in captured if hasattr(m, "instructions")
    )
    assert "breakaway_established" in instructions
    assert "Five clear" in instructions


async def test_prompt_offers_only_phase_plausible_patterns(window):
    captured: list[ModelMessage] = []
    agent = TacticsAgent(reading_model({"events": []}, captured))
    await run(agent, window, RaceState(phase=RacePhase.EARLY_ATTACKS, km_to_go=180))

    instructions = " ".join(
        m.instructions or "" for m in captured if hasattr(m, "instructions")
    )
    assert "breakaway_attempt" in instructions
    assert "leadout_forming" not in instructions


def test_build_prompt_names_new_posts(window):
    assert "p1" in build_prompt(window, window.posts)


# --- the gate ------------------------------------------------------------


@pytest.fixture
def chase_state():
    return RaceState(
        phase=RacePhase.CONTROLLED_CHASE,
        km_to_go=80,
        groups=[
            Group(kind=GroupKind.BREAKAWAY, riders=["A Rider"], gap_to_leader_s=0),
            Group(kind=GroupKind.PELOTON, gap_to_leader_s=100),
        ],
    )


def test_quiet_tick_does_not_trigger(chase_state):
    triggered, _ = meaningful_change(StateDelta(km_to_go=79), chase_state, [])
    assert triggered is False


def test_phase_change_triggers(chase_state):
    triggered, reason = meaningful_change(
        StateDelta(phase=RacePhase.FINALE), chase_state, []
    )
    assert triggered
    assert "finale" in reason


def test_new_group_triggers(chase_state):
    triggered, reason = meaningful_change(
        StateDelta(groups=[GroupDelta(kind=GroupKind.CHASE, riders_added=["Z Rider"])]),
        chase_state,
        [],
    )
    assert triggered
    assert "new chase" in reason


def test_dissolved_group_triggers(chase_state):
    triggered, _ = meaningful_change(
        StateDelta(
            groups=[
                GroupDelta(
                    kind=GroupKind.BREAKAWAY, riders_added=["A Rider"], dissolved=True
                )
            ]
        ),
        chase_state,
        [],
    )
    assert triggered


def test_large_gap_shift_triggers(chase_state):
    triggered, reason = meaningful_change(
        StateDelta(
            groups=[
                GroupDelta(
                    kind=GroupKind.PELOTON,
                    gap_to_leader_s=100 - GAP_SHIFT_SECONDS - 1,
                )
            ]
        ),
        chase_state,
        [],
    )
    assert triggered
    assert "gap moved" in reason


def test_small_gap_shift_does_not_trigger(chase_state):
    triggered, _ = meaningful_change(
        StateDelta(groups=[GroupDelta(kind=GroupKind.PELOTON, gap_to_leader_s=95)]),
        chase_state,
        [],
    )
    assert triggered is False


def test_composition_change_triggers(chase_state):
    triggered, reason = meaningful_change(
        StateDelta(
            groups=[
                GroupDelta(
                    kind=GroupKind.BREAKAWAY,
                    riders_added=["A Rider"],
                    riders_removed=["A Rider"],
                )
            ]
        ),
        chase_state,
        [],
    )
    assert triggered
    assert "composition" in reason


def test_salient_commentary_triggers_even_without_state_change(chase_state, make_normalised):
    loud = make_normalised("Echelons are forming and the bunch has split in the crosswind")
    triggered, reason = meaningful_change(StateDelta(), chase_state, [loud])
    assert triggered
    assert "salient" in reason


# --- two-stage -----------------------------------------------------------


class StubSituation:
    name = "stub-situation"

    def __init__(self, delta: StateDelta) -> None:
        self.delta = delta
        self.calls = 0

    async def analyse(self, *, window, state, new_posts):
        from race_bot.models.tactics import AnalysisResult

        self.calls += 1
        return AnalysisResult(delta=self.delta.model_copy(deep=True))


async def test_two_stage_skips_tactics_on_a_quiet_tick(window, chase_state):
    tactics = TacticsAgent(reading_model({"events": [proposed()]}))
    analyser = TwoStageAnalyser(
        situation=StubSituation(StateDelta(km_to_go=79)), tactics=tactics
    )
    result = await analyser.analyse(window=window, state=chase_state, new_posts=[])

    assert tactics.stats.calls == 0
    assert result.events == []
    assert analyser.tactics_skipped == 1


async def test_two_stage_runs_tactics_on_change(window, chase_state):
    tactics = TacticsAgent(reading_model({"events": [proposed()]}))
    analyser = TwoStageAnalyser(
        situation=StubSituation(StateDelta(phase=RacePhase.FINALE)), tactics=tactics
    )
    result = await analyser.analyse(window=window, state=chase_state, new_posts=[])

    assert tactics.stats.calls == 1
    assert len(result.events) == 1
    assert analyser.tactics_calls == 1


async def test_tactics_sees_state_after_the_delta_is_applied(window, chase_state):
    """A stale picture would make the tactics agent reason about the last tick."""
    captured: list[ModelMessage] = []
    tactics = TacticsAgent(reading_model({"events": []}, captured))
    analyser = TwoStageAnalyser(
        situation=StubSituation(StateDelta(phase=RacePhase.SPRINT, km_to_go=1)),
        tactics=tactics,
    )
    await analyser.analyse(window=window, state=chase_state, new_posts=[])

    instructions = " ".join(
        m.instructions or "" for m in captured if hasattr(m, "instructions")
    )
    assert "phase: sprint" in instructions
    assert "km_to_go: 1" in instructions


async def test_two_stage_passes_open_events_through(window, chase_state):
    tracked = [
        TacticalEvent(
            pattern=TacticalPattern.BREAKAWAY_ESTABLISHED, headline="Five clear"
        )
    ]
    tactics = TacticsAgent(reading_model({"events": []}))
    analyser = TwoStageAnalyser(
        situation=StubSituation(StateDelta(phase=RacePhase.FINALE)),
        tactics=tactics,
        open_events=lambda: tracked,
    )
    await analyser.analyse(window=window, state=chase_state, new_posts=[])

    assert tactics.open_events == tracked


async def test_two_stage_keeps_the_delta_when_tactics_fails(window, chase_state):
    """State tracking must survive the tactics agent being down."""
    tactics = TacticsAgent(failing_model(RuntimeError("down")))
    analyser = TwoStageAnalyser(
        situation=StubSituation(StateDelta(phase=RacePhase.FINALE)), tactics=tactics
    )
    result = await analyser.analyse(window=window, state=chase_state, new_posts=[])

    assert result.delta.phase is RacePhase.FINALE
    assert result.events == []


async def test_status_advance_flows_to_the_tracker(window, state):
    """suspected -> confirmed must reach the user, not be absorbed as a duplicate."""
    from datetime import UTC, datetime

    from race_bot.pipeline.tracker import EventTracker

    tracker = EventTracker()
    now = datetime(2026, 4, 12, 15, 0, tzinfo=UTC)

    first = TacticsAgent(reading_model({"events": [proposed(status="suspected")]}))
    for event in (await run(first, window, state)).events:
        assert tracker.ingest(event, now=now) is not None

    second = TacticsAgent(
        reading_model({"events": [proposed(status="confirmed", confidence=0.9)]})
    )
    surfaced = [
        tracker.ingest(e, now=now) for e in (await run(second, window, state)).events
    ]
    assert all(s is not None for s in surfaced)
    assert tracker.all_events[0].status is EventStatus.CONFIRMED
    assert len(tracker.all_events) == 1


async def test_heuristic_still_usable_as_the_event_source(window, chase_state):
    """azure-state mode: model for state, keyword rules for callouts."""
    from race_bot.analysis.composite import CompositeAnalyser

    analyser = CompositeAnalyser(
        state_from=StubSituation(StateDelta(km_to_go=24)),
        events_from=HeuristicAnalyser(),
    )
    result = await analyser.analyse(
        window=window, state=chase_state, new_posts=window.posts
    )
    assert result.delta.km_to_go == 24


# --- full pipeline -------------------------------------------------------


async def test_two_stage_over_the_whole_fixture():
    """Both agents wired through the orchestrator on the reference race."""
    from race_bot.pipeline.orchestrator import Orchestrator
    from tests.conftest import ChunkedSource, load_fixture_posts

    posts = await load_fixture_posts()

    # A model that reports the same solo bid every time it is asked. The tracker
    # should absorb the repeats into a single event.
    tactics = TacticsAgent(evidence_aware_model())
    situation = StubSituation(
        StateDelta(
            phase=RacePhase.FINALE,
            km_to_go=24,
            groups=[GroupDelta(kind=GroupKind.BREAKAWAY, size=4, gap_to_leader_s=0)],
        )
    )
    analyser = TwoStageAnalyser(situation=situation, tactics=tactics)

    orchestrator = Orchestrator(
        source=ChunkedSource(posts, 4),
        analyser=analyser,
        tick_seconds=0,
        max_pending=1,
    )
    analyser.open_events = lambda: orchestrator.tracker.open_events
    state = await orchestrator.run()

    assert state.posts_seen == 49
    assert tactics.stats.calls >= 1
    # Evidence in the fixture is real, so nothing should be rejected outright.
    assert not any("no valid evidence" in n for n in tactics.stats.notes)
    # One repeated read must not become many events.
    assert len(orchestrator.tracker.all_events) == 1


async def test_gate_reduces_tactics_calls_over_a_real_race():
    """The gate exists to keep the expensive agent off quiet ticks."""
    from race_bot.pipeline.orchestrator import Orchestrator
    from tests.conftest import ChunkedSource, load_fixture_posts

    posts = await load_fixture_posts()
    tactics = TacticsAgent(reading_model({"events": []}))
    # A situation agent reporting nothing: only salient commentary should trigger.
    analyser = TwoStageAnalyser(situation=StubSituation(StateDelta()), tactics=tactics)

    orchestrator = Orchestrator(
        source=ChunkedSource(posts, 2),
        analyser=analyser,
        tick_seconds=0,
        max_pending=1,
    )
    await orchestrator.run()

    total = analyser.tactics_calls + analyser.tactics_skipped
    assert total > 0
    assert analyser.tactics_calls < total, "gate should skip at least some ticks"


def test_gate_thresholds_are_configurable(chase_state, make_normalised):
    """Untuned defaults must not be baked in — this gate needs real-data calibration."""
    post = make_normalised("A five-man move has gone clear at the front")

    permissive, _ = meaningful_change(
        StateDelta(), chase_state, [post], salience_threshold=0.1
    )
    strict, _ = meaningful_change(
        StateDelta(), chase_state, [post], salience_threshold=0.99
    )
    assert permissive is True
    assert strict is False


def test_gap_shift_threshold_is_configurable(chase_state):
    delta = StateDelta(groups=[GroupDelta(kind=GroupKind.PELOTON, gap_to_leader_s=90)])
    tight, _ = meaningful_change(delta, chase_state, [], gap_shift_seconds=5)
    loose, _ = meaningful_change(delta, chase_state, [], gap_shift_seconds=60)
    assert tight is True
    assert loose is False
