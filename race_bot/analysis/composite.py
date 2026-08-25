"""Combining analysers that do different halves of the job."""

from __future__ import annotations

import asyncio

from race_bot.analysis.base import Analyser
from race_bot.models.commentary import NormalisedPost
from race_bot.models.race_state import RaceState
from race_bot.models.tactics import AnalysisResult
from race_bot.pipeline.window import ContextWindow


class CompositeAnalyser:
    """Takes state from one analyser and tactical events from another.

    Phase 3 ships the situation agent but not the tactics agent, so the model
    tracks race state while the keyword baseline still supplies callouts. In
    phase 4 `events_from` becomes the tactics agent and this stays as it is.
    """

    def __init__(self, *, state_from: Analyser, events_from: Analyser) -> None:
        self.state_from = state_from
        self.events_from = events_from

    @property
    def name(self) -> str:
        return f"{self.state_from.name}+{self.events_from.name}"

    async def analyse(
        self,
        *,
        window: ContextWindow,
        state: RaceState,
        new_posts: list[NormalisedPost],
    ) -> AnalysisResult:
        # Run together: one is a network call, the other is local, so there is no
        # reason for the pipeline to wait for them in sequence.
        delta_result, events_result = await asyncio.gather(
            self.state_from.analyse(window=window, state=state, new_posts=new_posts),
            self.events_from.analyse(window=window, state=state, new_posts=new_posts),
        )
        return AnalysisResult(delta=delta_result.delta, events=events_result.events)
