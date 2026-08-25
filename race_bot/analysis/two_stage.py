"""Situation agent every tick; tactics agent only when the race has moved.

The tactics agent uses the stronger deployment and reasons over the full pattern
knowledge base, so calling it on every quiet tick would be expensive and would
mostly produce empty readings. This module decides when it is worth asking.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from race_bot.agents.tactics import TacticsAgent
from race_bot.analysis.base import Analyser
from race_bot.models.commentary import NormalisedPost
from race_bot.models.race_state import RaceState, StateDelta
from race_bot.models.tactics import AnalysisResult, TacticalEvent
from race_bot.pipeline.state import apply_delta, find_matching_group
from race_bot.pipeline.window import ContextWindow

GAP_SHIFT_SECONDS = 30.0
"""A gap moving by this much is a change in the race, not measurement noise."""

HIGH_SALIENCE = 0.55
"""A post this salient is worth a look even if the state barely moved."""


def meaningful_change(
    delta: StateDelta,
    state: RaceState,
    new_posts: list[NormalisedPost],
    *,
    gap_shift_seconds: float = GAP_SHIFT_SECONDS,
    salience_threshold: float = HIGH_SALIENCE,
) -> tuple[bool, str]:
    """Whether the picture moved enough to justify a tactics call.

    Returns the decision and a short reason, which is recorded for diagnostics —
    this gate is a cost/coverage tradeoff, and until it has been calibrated
    against real commentary rather than a fixture, both the decision and the
    thresholds behind it need to be visible and tunable.
    """
    if delta.phase is not None and delta.phase is not state.phase:
        return True, f"phase -> {delta.phase.value}"

    for group_delta in delta.groups:
        existing = find_matching_group(state, group_delta)

        if group_delta.dissolved:
            return True, f"{group_delta.kind.value} dissolved"

        if existing is None:
            return True, f"new {group_delta.kind.value}"

        if group_delta.riders_added or group_delta.riders_removed:
            return True, f"{group_delta.kind.value} composition changed"

        if (
            group_delta.gap_to_leader_s is not None
            and existing.gap_to_leader_s is not None
            and abs(group_delta.gap_to_leader_s - existing.gap_to_leader_s)
            >= gap_shift_seconds
        ):
            return True, f"{group_delta.kind.value} gap moved"

    salient = max((p.salience for p in new_posts), default=0.0)
    if salient >= salience_threshold:
        return True, f"salient commentary ({salient:.2f})"

    return False, "no material change"


class TwoStageAnalyser:
    """Runs the situation agent every tick and the tactics agent on change."""

    name = "two-stage"

    def __init__(
        self,
        *,
        situation: Analyser,
        tactics: TacticsAgent,
        open_events: Callable[[], list[TacticalEvent]] | None = None,
        gap_shift_seconds: float = GAP_SHIFT_SECONDS,
        salience_threshold: float = HIGH_SALIENCE,
    ) -> None:
        self.situation = situation
        self.tactics = tactics
        self.open_events = open_events or (lambda: [])
        self.gap_shift_seconds = gap_shift_seconds
        self.salience_threshold = salience_threshold
        self.tactics_calls = 0
        self.tactics_skipped = 0

    async def analyse(
        self,
        *,
        window: ContextWindow,
        state: RaceState,
        new_posts: list[NormalisedPost],
    ) -> AnalysisResult:
        situation_result = await self.situation.analyse(
            window=window, state=state, new_posts=new_posts
        )

        should_run, reason = meaningful_change(
            situation_result.delta,
            state,
            new_posts,
            gap_shift_seconds=self.gap_shift_seconds,
            salience_threshold=self.salience_threshold,
        )
        if not should_run:
            self.tactics_skipped += 1
            return situation_result

        self.tactics_calls += 1
        self.tactics.stats.record_note(f"triggered by {reason}")

        # The tactics agent must reason about the race as it is now, not as it
        # was before this tick's delta. Project the delta forward rather than
        # handing it a stale picture.
        projected, _ = apply_delta(state, situation_result.delta, now=datetime.now(UTC))
        self.tactics.open_events = self.open_events()

        tactics_result = await self.tactics.analyse(
            window=window, state=projected, new_posts=new_posts
        )
        return AnalysisResult(delta=situation_result.delta, events=tactics_result.events)
