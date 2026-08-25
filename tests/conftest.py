from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from race_bot.models.commentary import CommentaryPost, NormalisedPost
from race_bot.pipeline.normalise import normalise

BASE_TIME = datetime(2026, 4, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def make_post():
    """Build a CommentaryPost with sensible defaults."""

    def _make(text: str, *, minute: int = 0, post_id: str | None = None) -> CommentaryPost:
        timestamp = BASE_TIME + timedelta(minutes=minute)
        return CommentaryPost(
            id=post_id or f"p{minute}",
            timestamp=timestamp,
            text=text,
            source="test",
        )

    return _make


@pytest.fixture
def make_normalised(make_post):
    def _make(text: str, *, minute: int = 0, post_id: str | None = None) -> NormalisedPost:
        return normalise(make_post(text, minute=minute, post_id=post_id))

    return _make


class ChunkedSource:
    """Feeds pre-loaded posts in fixed-size batches, to vary tick granularity.

    Replay at speed=0 hands over the whole transcript in a single poll, which
    collapses a race into one tick. Anything that depends on there being many
    ticks — gating, dedup across updates — needs this instead.
    """

    name = "chunked"

    def __init__(self, posts, chunk_size: int) -> None:
        self._chunks = [
            posts[i : i + chunk_size] for i in range(0, len(posts), chunk_size)
        ]

    async def poll(self):
        return self._chunks.pop(0) if self._chunks else []

    @property
    def exhausted(self) -> bool:
        return not self._chunks

    async def close(self) -> None:
        return None


FIXTURE_RACE = (
    Path(__file__).parent.parent / "fixtures" / "races" / "kustklassieker_2026.jsonl"
)


async def load_fixture_posts():
    """The reference transcript as a plain list of posts."""
    from race_bot.sources.replay import ReplaySource

    return await ReplaySource(FIXTURE_RACE, speed=0).poll()
