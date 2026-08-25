"""The tactics agent: recognises tactical moves and explains why they matter.

Runs only when the race picture has actually moved, so it is pointed at the
stronger deployment. Its output is validated before anything reaches the user —
an analysis that cannot be traced back to the commentary is not worth showing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from race_bot.agents.prompts import (
    render_open_events,
    render_state,
    tactics_instructions,
)
from race_bot.agents.runner import DEFAULT_TIMEOUT_SECONDS, AgentStats, CallGuard
from race_bot.knowledge.patterns import load_patterns
from race_bot.models.commentary import NormalisedPost
from race_bot.models.race_state import RaceState
from race_bot.models.tactics import (
    AnalysisResult,
    TacticalEvent,
    TacticalReading,
)
from race_bot.pipeline.window import ContextWindow


@dataclass
class TacticsDeps:
    """What the agent's dynamic instructions need."""

    state: RaceState
    open_events: list[TacticalEvent] = field(default_factory=list)


def build_tactics_agent(model: Model) -> Agent[TacticsDeps, TacticalReading]:
    agent: Agent[TacticsDeps, TacticalReading] = Agent(
        model,
        output_type=TacticalReading,
        deps_type=TacticsDeps,
        retries=2,
        name="tactics",
    )

    @agent.instructions
    def patterns_for_this_phase(ctx: RunContext[TacticsDeps]) -> str:
        # Rebuilt per call: the plausible pattern set narrows as the race
        # progresses, which keeps the prompt small and suppresses moves that
        # cannot be happening yet.
        return tactics_instructions(ctx.deps.state.phase)

    @agent.instructions
    def current_state(ctx: RunContext[TacticsDeps]) -> str:
        return "## Race state\n\n" + render_state(ctx.deps.state)

    @agent.instructions
    def tracked_moves(ctx: RunContext[TacticsDeps]) -> str:
        return "## Moves already being tracked\n\n" + render_open_events(
            ctx.deps.open_events
        )

    return agent


def build_prompt(window: ContextWindow, new_posts: list[NormalisedPost]) -> str:
    new_ids = ", ".join(p.id for p in new_posts) or "none"
    return (
        "## Recent commentary, oldest first\n\n"
        f"{window.render()}\n\n"
        f"New since your last reading: {new_ids}\n\n"
        "Report any tactical moves happening now. An empty list is fine."
    )


@dataclass
class ValidationOutcome:
    """Events that survived validation, and why the others did not."""

    events: list[TacticalEvent] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    off_phase: list[str] = field(default_factory=list)


def validate_reading(
    reading: TacticalReading, *, window: ContextWindow, state: RaceState
) -> ValidationOutcome:
    """Check a reading against the commentary it claims to be based on.

    Evidence is enforced strictly: post ids that do not exist are dropped, and an
    event left with no evidence at all is discarded. A confident-sounding read
    citing a post that was never written is exactly the failure this project
    cannot ship.

    An off-phase pattern is kept but recorded. The prompt only offers patterns
    plausible in the current phase, so the model going outside that set is a
    signal that our own phase tracking may be wrong — which is worth seeing
    rather than silently suppressing.
    """
    outcome = ValidationOutcome()
    known_ids = {post.id for post in window.posts}
    knowledge = load_patterns()

    for proposed in reading.events:
        valid = [pid for pid in proposed.evidence_post_ids if pid in known_ids]
        invented = [pid for pid in proposed.evidence_post_ids if pid not in known_ids]

        if not valid:
            outcome.rejected.append(
                f"{proposed.pattern.value}: no valid evidence "
                f"(cited {', '.join(proposed.evidence_post_ids) or 'nothing'})"
            )
            continue

        if invented:
            outcome.rejected.append(
                f"{proposed.pattern.value}: dropped unknown evidence "
                f"{', '.join(invented)}"
            )

        if not knowledge[proposed.pattern].plausible_in(state.phase):
            outcome.off_phase.append(
                f"{proposed.pattern.value} reported during {state.phase.value}"
            )

        event = proposed.to_event(km_to_go=state.km_to_go)
        event.evidence_post_ids = valid
        outcome.events.append(event)

    return outcome


class TacticsAgent:
    """`Analyser` implementation backed by the tactics agent.

    Returns events only — race state is the situation agent's concern.
    """

    name = "tactics"

    def __init__(
        self, model: Model, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self.agent = build_tactics_agent(model)
        self.guard = CallGuard(timeout_seconds=timeout_seconds)
        self.open_events: list[TacticalEvent] = []
        """Set by the caller each tick, so the agent can advance moves it already reported."""

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
                build_prompt(window, new_posts),
                deps=TacticsDeps(state=state, open_events=list(self.open_events)),
            )
        )
        if result is None:
            return AnalysisResult()

        self.guard.record_usage(result.usage)
        outcome = validate_reading(result.output, window=window, state=state)

        for note in outcome.rejected + outcome.off_phase:
            self.stats.record_note(note)

        return AnalysisResult(events=outcome.events)
