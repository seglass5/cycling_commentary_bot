"""Recording a live source to a replayable transcript.

This closes the loop the project needs: follow a real race, keep the transcript,
replay it deterministically, and evaluate against it. The output format is exactly
what `ReplaySource` reads and `race-bot inspect` analyses, so a recorded race
becomes a fixture with no conversion step.
"""

from __future__ import annotations

import json
from pathlib import Path

from race_bot.models.commentary import CommentaryPost
from race_bot.sources.base import LiveTextSource


class RecordingSource:
    """Wraps any source, appending every post it yields to a JSONL file.

    Writes are flushed per poll rather than buffered to the end: a race that is
    interrupted — a dropped connection, a stopped process — should still leave
    behind everything it saw.
    """

    def __init__(self, inner: LiveTextSource, path: str | Path) -> None:
        self.inner = inner
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.recorded = 0

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def exhausted(self) -> bool:
        return self.inner.exhausted

    async def poll(self) -> list[CommentaryPost]:
        posts = await self.inner.poll()
        if posts:
            self._append(posts)
        return posts

    def _append(self, posts: list[CommentaryPost]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            for post in posts:
                handle.write(
                    json.dumps(
                        {
                            "id": post.id,
                            "timestamp": post.timestamp.isoformat(),
                            "text": post.text,
                            **({"author": post.author} if post.author else {}),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        self.recorded += len(posts)

    async def close(self) -> None:
        await self.inner.close()
