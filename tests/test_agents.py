"""Phase 3 agents, exercised with stub models. No network, no credentials."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from race_bot.agents.provider import AzureModels, AzureNotConfigured
from race_bot.agents.situation import SituationAgent, build_prompt
from race_bot.analysis.composite import CompositeAnalyser
from race_bot.analysis.heuristic import HeuristicAnalyser
from race_bot.config import Settings
from race_bot.models.race_state import GroupKind, RacePhase, RaceState
from race_bot.models.tactics import AnalysisResult
from race_bot.pipeline.state import apply_delta
from race_bot.pipeline.window import ContextWindow

SALIENT = "A five-man move has gone clear with 120km to go, the gap is 45 seconds"


def delta_model(payload: dict, captured: list[ModelMessage] | None = None) -> FunctionModel:
    """A model that always returns `payload` as the StateDelta."""

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if captured is not None:
            captured.extend(messages)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    return FunctionModel(handler)


def failing_model(exc: Exception) -> FunctionModel:
    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise exc

    return FunctionModel(handler)


def slow_model(delay: float) -> FunctionModel:
    async def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(delay)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {})])

    return FunctionModel(handler)


@pytest.fixture
def window(make_normalised):
    window = ContextWindow()
    window.add(make_normalised(SALIENT, post_id="p1"))
    return window


async def run(agent, window, state=None):
    return await agent.analyse(
        window=window, state=state or RaceState(), new_posts=window.posts
    )


# --- prompt construction -------------------------------------------------


def test_prompt_names_the_new_posts(window):
    prompt = build_prompt(window, window.posts)
    assert "p1" in prompt
    assert SALIENT in prompt


def test_prompt_handles_empty_new_posts(window):
    assert "none" in build_prompt(window, [])


def test_instructions_carry_current_state(window):
    """The agent must see what is already tracked, or it cannot emit a delta."""
    captured: list[ModelMessage] = []
    state = RaceState(race_name="Kustklassieker", phase=RacePhase.FINALE, km_to_go=20)
    agent = SituationAgent(delta_model({}, captured))

    asyncio.run(run(agent, window, state))

    instructions = " ".join(
        m.instructions or "" for m in captured if hasattr(m, "instructions")
    )
    assert "Kustklassieker" in instructions
    assert "finale" in instructions


# --- happy path ----------------------------------------------------------


async def test_delta_is_returned(window):
    agent = SituationAgent(
        delta_model(
            {
                "phase": "breakaway_gone",
                "km_to_go": 120,
                "groups": [{"kind": "breakaway", "size": 5, "gap_to_leader_s": 0}],
                "reasoning": "five clear",
            }
        )
    )
    result = await run(agent, window)

    assert result.delta.phase is RacePhase.BREAKAWAY_GONE
    assert result.delta.km_to_go == 120
    assert result.delta.groups[0].kind is GroupKind.BREAKAWAY


async def test_situation_agent_emits_no_events(window):
    """Tactical events are the tactics agent's job in phase 4."""
    agent = SituationAgent(delta_model({"km_to_go": 100}))
    assert (await run(agent, window)).events == []


async def test_delta_merges_into_state(window):
    agent = SituationAgent(
        delta_model({"km_to_go": 120, "groups": [{"kind": "breakaway", "size": 5}]})
    )
    result = await run(agent, window)
    new_state, changes = apply_delta(
        RaceState(), result.delta, now=window.posts[0].timestamp
    )

    assert new_state.km_to_go == 120
    assert len(new_state.groups) == 1
    assert changes


async def test_hallucinated_group_ref_does_not_duplicate(window):
    """The merge layer's guard, reached through the agent."""
    agent = SituationAgent(
        delta_model(
            {"groups": [{"group_ref": "does-not-exist", "kind": "breakaway",
                         "riders_added": ["A Rider"]}]}
        )
    )
    state, _ = apply_delta(
        RaceState(),
        (await run(agent, window)).delta,
        now=window.posts[0].timestamp,
    )
    assert len(state.groups) == 1


async def test_usage_is_tracked(window):
    agent = SituationAgent(delta_model({"km_to_go": 100}))
    await run(agent, window)

    assert agent.stats.calls == 1
    assert agent.stats.failures == 0
    assert agent.stats.total_tokens > 0


# --- failure handling ----------------------------------------------------


async def test_api_failure_returns_empty_result_not_an_exception(window):
    """A live race must survive an API outage."""
    agent = SituationAgent(failing_model(RuntimeError("azure exploded")))
    result = await run(agent, window)

    assert result.delta.is_empty
    assert agent.stats.failures == 1
    assert "azure exploded" in agent.stats.errors[0]


async def test_timeout_is_caught(window):
    agent = SituationAgent(slow_model(5.0), timeout_seconds=0.05)
    result = await run(agent, window)

    assert result.delta.is_empty
    assert agent.stats.failures == 1
    assert "timed out" in agent.stats.errors[0]


async def test_empty_delta_leaves_state_untouched(window):
    """The failure mode that matters: a dropped call must not wipe the race."""
    from race_bot.models.race_state import Group

    state = RaceState(
        phase=RacePhase.FINALE,
        km_to_go=20,
        groups=[Group(kind=GroupKind.BREAKAWAY, riders=["A Rider"])],
    )
    agent = SituationAgent(failing_model(RuntimeError("down")))
    result = await run(agent, window, state)

    new_state, changes = apply_delta(state, result.delta, now=window.posts[0].timestamp)
    assert new_state.phase is RacePhase.FINALE
    assert new_state.km_to_go == 20
    assert len(new_state.groups) == 1
    assert changes == []


def test_error_list_is_bounded():
    """A sustained outage must not grow the error list without limit."""
    agent = SituationAgent(failing_model(RuntimeError("boom")))
    for i in range(50):
        agent._record_failure(f"failure {i}")

    assert agent.stats.failures == 50
    assert len(agent.stats.errors) == 20


# --- composite -----------------------------------------------------------


async def test_composite_takes_state_from_one_and_events_from_the_other(window):
    composite = CompositeAnalyser(
        state_from=SituationAgent(delta_model({"km_to_go": 120})),
        events_from=HeuristicAnalyser(),
    )
    result = await run(composite, window)

    assert result.delta.km_to_go == 120
    assert result.events, "keyword baseline should still supply callouts"


async def test_composite_name_reports_both_halves():
    composite = CompositeAnalyser(
        state_from=SituationAgent(delta_model({})), events_from=HeuristicAnalyser()
    )
    assert composite.name == "situation+heuristic"


async def test_composite_survives_a_failing_state_analyser(window):
    composite = CompositeAnalyser(
        state_from=SituationAgent(failing_model(RuntimeError("down"))),
        events_from=HeuristicAnalyser(),
    )
    result = await run(composite, window)

    assert result.delta.is_empty
    assert result.events, "callouts must keep working when the model is down"


async def test_composite_returns_analysis_result(window):
    composite = CompositeAnalyser(
        state_from=HeuristicAnalyser(), events_from=HeuristicAnalyser()
    )
    assert isinstance(await run(composite, window), AnalysisResult)


# --- provider ------------------------------------------------------------


def test_provider_rejects_missing_credentials():
    with pytest.raises(AzureNotConfigured, match="RACE_BOT_AZURE_ENDPOINT"):
        AzureModels(Settings(azure_endpoint=None, azure_api_key=None))


def test_provider_builds_both_deployments():
    settings = Settings(
        azure_endpoint="https://example.openai.azure.com/",
        azure_api_key=SecretStr("fake-key"),
        situation_model="cheap-deployment",
        tactics_model="strong-deployment",
    )
    models = AzureModels(settings)

    assert models.situation.model_name == "cheap-deployment"
    assert models.tactics.model_name == "strong-deployment"


def test_provider_is_shared_between_deployments():
    """One HTTP client, not one per agent."""
    settings = Settings(
        azure_endpoint="https://example.openai.azure.com/",
        azure_api_key=SecretStr("fake-key"),
    )
    models = AzureModels(settings)
    assert models.situation.client is models.tactics.client


# --- full pipeline with an agent in place --------------------------------


async def test_agent_drives_state_through_the_whole_pipeline():
    """The situation agent wired into the orchestrator over the real fixture."""
    from pathlib import Path

    from race_bot.pipeline.orchestrator import Orchestrator
    from race_bot.sources.replay import ReplaySource

    fixture = Path(__file__).parent.parent / "fixtures" / "races" / "kustklassieker_2026.jsonl"

    scripted = delta_model(
        {
            "phase": "controlled_chase",
            "km_to_go": 120,
            "groups": [{"kind": "breakaway", "size": 5, "gap_to_leader_s": 0}],
            "reasoning": "scripted",
        }
    )
    agent = SituationAgent(scripted)
    orchestrator = Orchestrator(
        source=ReplaySource(fixture, speed=0),
        analyser=CompositeAnalyser(state_from=agent, events_from=HeuristicAnalyser()),
        tick_seconds=0,
        max_pending=1,
    )

    state = await orchestrator.run()

    assert state.posts_seen == 49
    assert state.km_to_go == 120
    assert len(state.groups) == 1
    assert agent.stats.calls >= 1
    assert agent.stats.failures == 0
    assert orchestrator.tracker.all_events, "keyword callouts still fire alongside"


async def test_pipeline_completes_when_the_model_is_down():
    """An Azure outage mid-race degrades to the baseline rather than crashing."""
    from pathlib import Path

    from race_bot.pipeline.orchestrator import Orchestrator
    from race_bot.sources.replay import ReplaySource

    fixture = Path(__file__).parent.parent / "fixtures" / "races" / "kustklassieker_2026.jsonl"

    agent = SituationAgent(failing_model(RuntimeError("azure down")))
    orchestrator = Orchestrator(
        source=ReplaySource(fixture, speed=0),
        analyser=CompositeAnalyser(state_from=agent, events_from=HeuristicAnalyser()),
        tick_seconds=0,
        max_pending=1,
    )

    state = await orchestrator.run()

    assert state.posts_seen == 49
    assert agent.stats.failures >= 1
    assert orchestrator.tracker.all_events


# --- backoff -------------------------------------------------------------


async def test_repeated_failures_back_off(window):
    """A five-hour race must not hammer a dead endpoint on every tick."""
    agent = SituationAgent(failing_model(RuntimeError("down")))
    for _ in range(12):
        await run(agent, window)

    assert agent.stats.skipped > 0
    assert agent.stats.calls < 12


async def test_first_failure_does_not_delay_the_next_attempt(window):
    """Transient errors are common; one failure should not suppress a retry."""
    agent = SituationAgent(failing_model(RuntimeError("blip")))
    await run(agent, window)
    await run(agent, window)

    assert agent.stats.calls == 2
    assert agent.stats.skipped == 0


async def test_recovery_resets_the_backoff(window):
    """A model that comes back must be used again immediately."""
    calls = {"n": 0}

    def handler(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("down")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"km_to_go": 50})])

    agent = SituationAgent(FunctionModel(handler))
    for _ in range(8):
        await run(agent, window)

    assert agent.stats.failures == 2
    # Once recovered, every subsequent tick is a real call again.
    result = await run(agent, window)
    assert result.delta.km_to_go == 50
    assert agent.stats.skipped <= 1
