"""The boundary between the pipeline and whatever does the reasoning."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from race_bot.models.commentary import NormalisedPost
from race_bot.models.race_state import RaceState
from race_bot.models.tactics import AnalysisResult
from race_bot.pipeline.window import ContextWindow


@runtime_checkable
class Analyser(Protocol):
    """Turns recent commentary into a state delta and any tactical events.

    The Pydantic AI agents implement this from phase 3. Keeping the pipeline
    written against a protocol means the whole loop is testable with a
    deterministic analyser and no network.
    """

    name: str

    async def analyse(
        self,
        *,
        window: ContextWindow,
        state: RaceState,
        new_posts: list[NormalisedPost],
    ) -> AnalysisResult:
        ...
