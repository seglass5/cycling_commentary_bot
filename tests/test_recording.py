"""Recording a live source into a replayable transcript."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from race_bot.models.commentary import CommentaryPost
from race_bot.sources.recording import RecordingSource
from race_bot.sources.replay import ReplaySource

BASE = datetime(2026, 4, 12, 12, 0, tzinfo=UTC)


class FakeLive:
    """Yields one batch per poll, then stays quiet like a live source."""

    name = "fake-live"

    def __init__(self, batches):
        self._batches = list(batches)
        self.closed = False

    async def poll(self):
        return self._batches.pop(0) if self._batches else []

    @property
    def exhausted(self) -> bool:
        return not self._batches

    async def close(self) -> None:
        self.closed = True


def post(text: str, minute: int, author: str | None = None) -> CommentaryPost:
    return CommentaryPost(
        id=f"live-{minute}",
        timestamp=BASE + timedelta(minutes=minute),
        text=text,
        source="fake-live",
        author=author,
    )


@pytest.fixture
def out(tmp_path):
    return tmp_path / "recorded" / "race.jsonl"


async def test_posts_are_written(out):
    source = RecordingSource(FakeLive([[post("A five-man move has gone clear", 0)]]), out)
    await source.poll()

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "A five-man move has gone clear"


async def test_parent_directory_is_created(tmp_path):
    out = tmp_path / "deep" / "nested" / "race.jsonl"
    RecordingSource(FakeLive([]), out)
    assert out.parent.exists()


async def test_polls_append_rather_than_overwrite(out):
    source = RecordingSource(
        FakeLive([[post("first post here", 0)], [post("second post here", 1)]]), out
    )
    await source.poll()
    await source.poll()

    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert source.recorded == 2


async def test_written_after_every_poll_not_at_the_end(out):
    """An interrupted race must still leave behind everything it saw."""
    source = RecordingSource(
        FakeLive([[post("first post here", 0)], [post("second post here", 1)]]), out
    )
    await source.poll()
    # No close, no second poll — simulating a dropped connection.
    assert out.read_text(encoding="utf-8").strip()


async def test_recording_round_trips_through_replay(out):
    """A recorded race must be readable as a fixture with no conversion."""
    original = [
        post("A five-man move has gone clear", 0),
        post("The gap is 45 seconds with 20km to go", 5, author="Jan Reporter"),
    ]
    source = RecordingSource(FakeLive([original]), out)
    await source.poll()

    replayed = await ReplaySource(out, speed=0).poll()

    assert [p.text for p in replayed] == [p.text for p in original]
    assert [p.id for p in replayed] == [p.id for p in original]
    assert [p.timestamp for p in replayed] == [p.timestamp for p in original]
    assert replayed[1].author == "Jan Reporter"


async def test_empty_poll_writes_nothing(out):
    source = RecordingSource(FakeLive([[]]), out)
    await source.poll()
    assert not out.exists() or out.read_text(encoding="utf-8") == ""


async def test_wrapper_is_transparent(out):
    inner = FakeLive([[post("something happened here", 0)]])
    source = RecordingSource(inner, out)

    assert source.name == "fake-live"
    assert source.exhausted is False
    await source.poll()
    assert source.exhausted is True
    await source.close()
    assert inner.closed


async def test_non_ascii_text_survives_the_round_trip(out):
    source = RecordingSource(
        FakeLive([[post("Attaque sur les pavés — Küng répond", 0)]]), out
    )
    await source.poll()
    replayed = await ReplaySource(out, speed=0).poll()
    assert replayed[0].text == "Attaque sur les pavés — Küng répond"
