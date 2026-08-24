"""The boundary between the bot and wherever commentary comes from."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from race_bot.models.commentary import CommentaryPost


@runtime_checkable
class LiveTextSource(Protocol):
    """A feed of race commentary.

    Everything downstream is written against this, so replay fixtures, a tailed
    file and a future HTTP live-blog adapter are interchangeable. Implementations
    are responsible only for *producing* posts; ordering, dedup and pacing of the
    consuming loop are handled by the pipeline.
    """

    name: str

    async def poll(self) -> list[CommentaryPost]:
        """Return posts available since the last call. May be empty."""
        ...

    @property
    def exhausted(self) -> bool:
        """True when no further posts will ever arrive.

        Replay sources reach this at the end of the transcript. A live feed
        returns False until the race finishes.
        """
        ...

    async def close(self) -> None:
        """Release any resources. Safe to call more than once."""
        ...
