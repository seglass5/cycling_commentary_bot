"""The tick loop: poll, normalise, analyse, merge, emit."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from race_bot.analysis.base import Analyser
from race_bot.models.commentary import NormalisedPost
from race_bot.models.race_state import RaceState
from race_bot.models.tactics import TacticalEvent
from race_bot.pipeline.normalise import normalise
from race_bot.pipeline.state import apply_delta
from race_bot.pipeline.tracker import EventTracker
from race_bot.pipeline.window import ContextWindow
from race_bot.sources.base import LiveTextSource


@dataclass
class TickResult:
    """Everything one pass through the loop produced."""

    new_posts: list[NormalisedPost] = field(default_factory=list)
    """Posts accepted into the window this tick, in order."""

    filtered_posts: list[NormalisedPost] = field(default_factory=list)
    """Posts dropped as noise. Kept so the renderer can show them dimmed."""

    changes: list[str] = field(default_factory=list)
    events: list[TacticalEvent] = field(default_factory=list)
    """Only events worth surfacing — the tracker has already absorbed duplicates."""

    analysed: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.new_posts or self.filtered_posts or self.changes or self.events)


TickCallback = Callable[[TickResult, RaceState], None | Awaitable[None]]


class Orchestrator:
    """Drives a source through the pipeline and maintains race state."""

    def __init__(
        self,
        *,
        source: LiveTextSource,
        analyser: Analyser,
        state: RaceState | None = None,
        window: ContextWindow | None = None,
        tracker: EventTracker | None = None,
        tick_seconds: float = 1.0,
        salience_threshold: float = 0.25,
        max_pending: int = 3,
    ) -> None:
        self.source = source
        self.analyser = analyser
        self.state = state or RaceState()
        self.window = window or ContextWindow()
        self.tracker = tracker or EventTracker()
        self.tick_seconds = tick_seconds
        self.salience_threshold = salience_threshold
        self.max_pending = max_pending

        self._seen_ids: set[str] = set()
        self._pending: list[NormalisedPost] = []

    @property
    def seen_count(self) -> int:
        return len(self._seen_ids)

    def _should_analyse(self, *, force: bool = False) -> bool:
        """Whether this batch justifies the cost of an analysis pass.

        Gated on salience so a run of low-content posts does not each trigger a
        model call, but backstopped by `max_pending` so a slow accumulation of
        quiet posts still gets looked at eventually. `force` drains whatever is
        left when the source ends, so the last few posts are never stranded.
        """
        if not self._pending:
            return False
        if force or len(self._pending) >= self.max_pending:
            return True
        return any(p.salience >= self.salience_threshold for p in self._pending)

    async def tick(self) -> TickResult:
        """One pass. Safe to call directly in tests without running the loop."""
        result = TickResult()
        now = datetime.now(UTC)

        for post in await self.source.poll():
            if post.id in self._seen_ids:
                continue
            self._seen_ids.add(post.id)
            self.state.posts_seen += 1

            normalised = normalise(post)
            if self.window.add(normalised):
                result.new_posts.append(normalised)
                self._pending.append(normalised)
            else:
                result.filtered_posts.append(normalised)

        if not self._should_analyse(force=self.source.exhausted):
            return result

        batch = self._pending
        self._pending = []
        result.analysed = True

        analysis = await self.analyser.analyse(
            window=self.window, state=self.state, new_posts=batch
        )

        if not analysis.delta.is_empty:
            self.state, result.changes = apply_delta(self.state, analysis.delta, now=now)

        for event in analysis.events:
            surfaced = self.tracker.ingest(event, now=now)
            if surfaced is not None:
                result.events.append(surfaced)

        return result

    async def run(self, on_tick: TickCallback | None = None) -> RaceState:
        """Run until the source is exhausted and nothing is left pending."""
        while True:
            result = await self.tick()

            if on_tick is not None and not result.is_empty:
                outcome = on_tick(result, self.state)
                if isinstance(outcome, Awaitable):
                    await outcome

            if self.source.exhausted and not self._pending:
                break
            if self.tick_seconds > 0:
                await asyncio.sleep(self.tick_seconds)

        await self.source.close()
        return self.state
