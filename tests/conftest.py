from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
