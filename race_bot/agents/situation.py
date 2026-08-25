"""The situation agent: commentary in, state delta out.

Runs every tick, so it is pointed at the cheap deployment. It does not produce
tactical events — that is the tactics agent in phase 4. Its only job is keeping
the race picture current.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from race_bot.agents.prompts import render_state, situation_instructions
from race_bot.models.commentary import NormalisedPost
from race_bot.models.race_state import RaceState, StateDelta
from race_bot.models.tactics import AnalysisResult
from race_bot.pipeline.window import ContextWindow

DEFAULT_TIMEOUT_SECONDS = 25.0
MAX_BACKOFF_TICKS = 32
"""Ceiling on how long a sustained outage suppresses calls."""


@dataclass
class SituationDeps:
    """What the agent's dynamic instructions need."""

    state: RaceState


@dataclass
class AgentStats:
    """Running totals, so cost and reliability are visible during a race."""

    calls: int = 0
    failures: int = 0
    skipped: int = 0
    """Ticks where the agent was not called because it was backing off."""

    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def summary(self) -> str:
        parts = [f"{self.calls} calls", f"{self.total_tokens} tokens"]
        if self.failures:
            parts.append(f"{self.failures} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped while backing off")
        return ", ".join(parts)


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

    A failed or slow call returns an empty result rather than raising. During a
    live race a dropped update is recoverable — the next tick sees the same
    commentary in its window — but a crash is not.

    Repeated failures back off exponentially. A race runs for five hours; without
    this, an outage means thousands of doomed calls against a dead endpoint.
    """

    name = "situation"

    def __init__(
        self,
        model: Model,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.agent = build_situation_agent(model)
        self.timeout_seconds = timeout_seconds
        self.stats = AgentStats()
        self._consecutive_failures = 0
        self._ticks_to_skip = 0

    async def analyse(
        self,
        *,
        window: ContextWindow,
        state: RaceState,
        new_posts: list[NormalisedPost],
    ) -> AnalysisResult:
        if self._ticks_to_skip > 0:
            self._ticks_to_skip -= 1
            self.stats.skipped += 1
            return AnalysisResult()

        self.stats.calls += 1
        try:
            result = await asyncio.wait_for(
                self.agent.run(
                    build_prompt(window, new_posts), deps=SituationDeps(state=state)
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            self._record_failure(f"situation agent timed out after {self.timeout_seconds:g}s")
            return AnalysisResult()
        except Exception as exc:  # noqa: BLE001 - a live race must survive any API failure
            self._record_failure(f"{type(exc).__name__}: {exc}")
            return AnalysisResult()

        usage = result.usage
        self.stats.input_tokens += usage.input_tokens or 0
        self.stats.output_tokens += usage.output_tokens or 0
        self._consecutive_failures = 0
        return AnalysisResult(delta=result.output)

    def _record_failure(self, message: str) -> None:
        self.stats.failures += 1
        self._consecutive_failures += 1
        # First failure is free — transient errors are common and the next tick
        # carries the same commentary anyway. Sustained failure backs off.
        self._ticks_to_skip = min(
            2 ** (self._consecutive_failures - 1) - 1, MAX_BACKOFF_TICKS
        )
        # Bounded: a sustained outage must not grow this without limit.
        if len(self.stats.errors) < 20:
            self.stats.errors.append(message)
