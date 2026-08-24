from __future__ import annotations

from datetime import UTC, datetime, timedelta

from race_bot.models.commentary import CommentaryPost, NormalisedPost
from race_bot.models.race_state import GroupDelta, GroupKind, RacePhase, StateDelta
from race_bot.models.tactics import AnalysisResult, TacticalEvent, TacticalPattern
from race_bot.pipeline.orchestrator import Orchestrator

BASE = datetime(2026, 4, 12, 12, 0, tzinfo=UTC)


class FakeSource:
    """Hands out pre-built batches, one per poll."""

    name = "fake"

    def __init__(self, batches: list[list[CommentaryPost]]) -> None:
        self._batches = list(batches)
        self.closed = False

    async def poll(self) -> list[CommentaryPost]:
        return self._batches.pop(0) if self._batches else []

    @property
    def exhausted(self) -> bool:
        return not self._batches

    async def close(self) -> None:
        self.closed = True


class RecordingAnalyser:
    """Returns a canned result and records what it was asked to analyse."""

    name = "recording"

    def __init__(self, result: AnalysisResult | None = None) -> None:
        self.result = result or AnalysisResult()
        self.calls: list[list[NormalisedPost]] = []

    async def analyse(self, *, window, state, new_posts) -> AnalysisResult:
        self.calls.append(list(new_posts))
        return self.result.model_copy(deep=True)


def post(text: str, minute: int, post_id: str | None = None) -> CommentaryPost:
    return CommentaryPost(
        id=post_id or f"p{minute}",
        timestamp=BASE + timedelta(minutes=minute),
        text=text,
        source="fake",
    )


SALIENT = "Echelons are forming and the bunch has split with 40km to go"
QUIET = "The riders roll along the coast road here"


async def test_tick_normalises_and_windows_posts():
    source = FakeSource([[post(SALIENT, 0)]])
    analyser = RecordingAnalyser()
    orchestrator = Orchestrator(source=source, analyser=analyser, tick_seconds=0)

    result = await orchestrator.tick()

    assert len(result.new_posts) == 1
    assert result.analysed
    assert len(orchestrator.window) == 1


async def test_noise_is_filtered_not_dropped_silently():
    source = FakeSource([[post("Subscribe to our newsletter", 0), post(SALIENT, 1)]])
    orchestrator = Orchestrator(source=source, analyser=RecordingAnalyser(), tick_seconds=0)

    result = await orchestrator.tick()

    assert len(result.filtered_posts) == 1
    assert len(result.new_posts) == 1


async def test_duplicate_posts_are_ignored():
    duplicate = post(SALIENT, 0, post_id="same")
    source = FakeSource([[duplicate], [duplicate]])
    orchestrator = Orchestrator(source=source, analyser=RecordingAnalyser(), tick_seconds=0)

    await orchestrator.tick()
    second = await orchestrator.tick()

    assert second.new_posts == []
    assert orchestrator.seen_count == 1


async def test_quiet_posts_do_not_trigger_analysis():
    source = FakeSource([[post(QUIET, 0)], [post(QUIET, 1)]])
    analyser = RecordingAnalyser()
    orchestrator = Orchestrator(
        source=source, analyser=analyser, tick_seconds=0, max_pending=5
    )

    result = await orchestrator.tick()

    assert not result.analysed
    assert analyser.calls == []


async def test_pending_backlog_forces_analysis():
    """Quiet posts still get looked at once enough accumulate."""
    source = FakeSource([[post(QUIET, i) for i in range(3)], []])
    analyser = RecordingAnalyser()
    orchestrator = Orchestrator(
        source=source, analyser=analyser, tick_seconds=0, max_pending=3
    )

    result = await orchestrator.tick()

    assert result.analysed
    assert len(analyser.calls[0]) == 3


async def test_delta_is_applied_to_state():
    result = AnalysisResult(
        delta=StateDelta(
            phase=RacePhase.FINALE,
            km_to_go=25,
            groups=[GroupDelta(kind=GroupKind.BREAKAWAY, riders_added=["A Rider"])],
        )
    )
    source = FakeSource([[post(SALIENT, 0)]])
    orchestrator = Orchestrator(
        source=source, analyser=RecordingAnalyser(result), tick_seconds=0
    )

    tick = await orchestrator.tick()

    assert orchestrator.state.phase is RacePhase.FINALE
    assert orchestrator.state.km_to_go == 25
    assert len(orchestrator.state.groups) == 1
    assert tick.changes


async def test_duplicate_events_are_absorbed_across_ticks():
    result = AnalysisResult(
        events=[
            TacticalEvent(
                pattern=TacticalPattern.SOLO_BID,
                headline="A Rider goes alone",
                participants=["A Rider"],
            )
        ]
    )
    source = FakeSource([[post(SALIENT, 0)], [post(SALIENT, 1, post_id="p1")]])
    orchestrator = Orchestrator(
        source=source, analyser=RecordingAnalyser(result), tick_seconds=0
    )

    first = await orchestrator.tick()
    second = await orchestrator.tick()

    assert len(first.events) == 1
    assert second.events == []
    assert len(orchestrator.tracker.all_events) == 1


async def test_run_drains_pending_posts_before_finishing():
    """A source that ends on quiet posts must not strand them."""
    source = FakeSource([[post(QUIET, 0)]])
    analyser = RecordingAnalyser()
    orchestrator = Orchestrator(
        source=source, analyser=analyser, tick_seconds=0, max_pending=99
    )

    await orchestrator.run()

    assert len(analyser.calls) == 1
    assert source.closed


async def test_run_invokes_callback_only_for_non_empty_ticks():
    source = FakeSource([[post(SALIENT, 0)], []])
    seen = []

    orchestrator = Orchestrator(
        source=source, analyser=RecordingAnalyser(), tick_seconds=0
    )
    await orchestrator.run(on_tick=lambda result, state: seen.append(result))

    assert len(seen) == 1


async def test_run_accepts_async_callback():
    source = FakeSource([[post(SALIENT, 0)]])
    seen = []

    async def on_tick(result, state):
        seen.append(result)

    orchestrator = Orchestrator(source=source, analyser=RecordingAnalyser(), tick_seconds=0)
    await orchestrator.run(on_tick=on_tick)

    assert len(seen) == 1


async def test_posts_seen_counts_everything_including_noise():
    source = FakeSource([[post("Subscribe now", 0), post(SALIENT, 1)]])
    orchestrator = Orchestrator(source=source, analyser=RecordingAnalyser(), tick_seconds=0)

    await orchestrator.tick()

    assert orchestrator.state.posts_seen == 2
