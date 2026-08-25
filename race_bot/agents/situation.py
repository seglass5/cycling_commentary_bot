"""The situation agent: commentary in, state delta out.

Runs every tick, so it is pointed at the cheap deployment. It produces no
tactical events — that is the tactics agent's job. Its only concern is keeping
the race picture current.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from race_bot.agents.prompts import render_state, situation_instructions
from race_bot.agents.runner import DEFAULT_TIMEOUT_SECONDS, AgentStats, CallGuard
from race_bot.models.commentary import NormalisedPost
from race_bot.models.race_state import RaceState, StateDelta
from race_bot.models.tactics import AnalysisResult
from race_bot.pipeline.window import ContextWindow

__all__ = ["AgentStats", "SituationAgent", "SituationDeps", "build_prompt", "build_situation_agent"]


@dataclass
class SituationDeps:
    """What the agent's dynamic instructions need."""

    state: RaceState


def build_situation_agent(model: Model) -> Agent[SituationDeps, StateDelta]:
    agent: Agent[SituationDeps, StateDelta] = Agent(
        model,
        output_type=StateDelta,
        deps_type=SituationDeps,
        instructions=situation_instructions(),
        retries=2,
        name="situation",
    )

    @agent.instructions
    def current_state(ctx: RunContext[SituationDeps]) -> str:
        return "## Race state as currently understood\n\n" + render_state(ctx.deps.state)

    return agent


def build_prompt(window: ContextWindow, new_posts: list[NormalisedPost]) -> str:
    """The user turn: recent commentary, with the unseen posts called out."""
    new_ids = ", ".join(p.id for p in new_posts) or "none"
    return (
        "## Recent commentary, oldest first\n\n"
        f"{window.render()}\n\n"
        f"New since your last update: {new_ids}\n\n"
        "Return a StateDelta with only what has changed."
    )


class SituationAgent:
    """`Analyser` implementation backed by the situation agent.

    A failed or slow call returns an empty result rather than raising. Because an
    unset delta field means "no change", a dropped update costs one tick — the
    next one sees the same commentary in its window — while a crash would end the
    race coverage entirely.
    """

    name = "situation"

    def __init__(
        self, model: Model, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self.agent = build_situation_agent(model)
        self.guard = CallGuard(timeout_seconds=timeout_seconds)

    @property
    def stats(self) -> AgentStats:
        return self.guard.stats

    async def analyse(
        self,
        *,
        window: ContextWindow,
        state: RaceState,
        new_posts: list[NormalisedPost],
    ) -> AnalysisResult:
        result = await self.guard.run(
            lambda: self.agent.run(
                build_prompt(window, new_posts), deps=SituationDeps(state=state)
            )
        )
        if result is None:
            return AnalysisResult()

        self.guard.record_usage(result.usage)
        return AnalysisResult(delta=result.output)
