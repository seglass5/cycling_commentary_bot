"""Commentary posts as they arrive from a source, and what we extract from them."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class PostKind(StrEnum):
    """What a post is actually doing, decided by cheap deterministic rules."""

    RACE = "race"
    """Substantive description of the race situation."""

    RESULT = "result"
    """Standings, classifications, intermediate sprint results."""

    NOISE = "noise"
    """Sponsor plugs, greetings, rider trivia, social embeds."""


class CommentaryPost(BaseModel):
    """A single post from a live text feed."""

    id: str
    timestamp: datetime
    text: str
    source: str
    author: str | None = None

    @staticmethod
    def make_id(source: str, timestamp: datetime, text: str) -> str:
        """Stable id for feeds that do not supply one.

        Content-hashed so re-polling the same post is recognised as a duplicate
        even when the feed renumbers or reorders entries.
        """
        digest = hashlib.sha256(
            f"{source}|{timestamp.isoformat()}|{text.strip()}".encode()
        ).hexdigest()
        return f"{source}-{digest[:12]}"


class ExtractedFacts(BaseModel):
    """Facts pulled out by regex, before any model sees the post.

    Everything here is cheap and reliable. Handing these to the model as structured
    hints beats asking it to re-read numbers out of prose.
    """

    km_to_go: float | None = None
    gaps_seconds: list[float] = Field(default_factory=list)
    group_sizes: list[int] = Field(default_factory=list)
    rider_mentions: list[str] = Field(default_factory=list)
    team_mentions: list[str] = Field(default_factory=list)


class NormalisedPost(BaseModel):
    """A post plus everything the deterministic pre-filter worked out about it."""

    post: CommentaryPost
    kind: PostKind
    facts: ExtractedFacts
    salience: float = 0.0
    """0-1. Drives what makes it into the context window when it is full."""

    @property
    def id(self) -> str:
        return self.post.id

    @property
    def text(self) -> str:
        return self.post.text

    @property
    def timestamp(self) -> datetime:
        return self.post.timestamp

    @property
    def is_noise(self) -> bool:
        return self.kind is PostKind.NOISE
