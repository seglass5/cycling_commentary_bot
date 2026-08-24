"""Replay a saved transcript as though the race were happening now."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from race_bot.models.commentary import CommentaryPost


class ReplaySource:
    """Re-emits a fixture transcript on its original timing, scaled by `speed`.

    A five-hour race at speed=3600 replays in five seconds; at speed=0 the whole
    transcript arrives on the first poll. That makes the entire pipeline
    deterministically testable without waiting on a real race.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        speed: float = 60.0,
        name: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.speed = speed
        self.name = name or self.path.stem
        self._posts = _load_transcript(self.path, self.name)
        self._cursor = 0
        self._wall_start: float | None = None
        self._race_start: datetime | None = self._posts[0].timestamp if self._posts else None

    @property
    def post_count(self) -> int:
        return len(self._posts)

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._posts)

    async def poll(self) -> list[CommentaryPost]:
        if self.exhausted:
            return []

        if self.speed <= 0:
            batch = self._posts[self._cursor :]
            self._cursor = len(self._posts)
            return batch

        now = time.monotonic()
        if self._wall_start is None:
            # Anchor on first poll so construction-to-first-poll delay is not
            # counted as race time.
            self._wall_start = now

        assert self._race_start is not None
        elapsed_race_seconds = (now - self._wall_start) * self.speed

        batch: list[CommentaryPost] = []
        while self._cursor < len(self._posts):
            post = self._posts[self._cursor]
            if (post.timestamp - self._race_start).total_seconds() > elapsed_race_seconds:
                break
            batch.append(post)
            self._cursor += 1
        return batch

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None


def _load_transcript(path: Path, source_name: str) -> list[CommentaryPost]:
    """Read a JSONL transcript into posts, sorted by timestamp.

    Each line needs `timestamp` and `text`; `id` and `author` are optional.
    Posts without an id get a stable content hash.
    """
    if not path.exists():
        raise FileNotFoundError(f"transcript not found: {path}")

    posts: list[CommentaryPost] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON — {exc}") from exc

        if "text" not in raw or "timestamp" not in raw:
            raise ValueError(f"{path}:{lineno}: entry needs both 'timestamp' and 'text'")

        timestamp = _parse_timestamp(raw["timestamp"], path, lineno)
        text = str(raw["text"]).strip()
        posts.append(
            CommentaryPost(
                id=raw.get("id") or CommentaryPost.make_id(source_name, timestamp, text),
                timestamp=timestamp,
                text=text,
                source=source_name,
                author=raw.get("author"),
            )
        )

    posts.sort(key=lambda p: p.timestamp)
    return posts


def _parse_timestamp(value: object, path: Path, lineno: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{path}:{lineno}: bad timestamp {value!r} — {exc}") from exc
    # Naive timestamps are common in scraped transcripts; treat them as UTC so
    # arithmetic against other posts never raises.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
