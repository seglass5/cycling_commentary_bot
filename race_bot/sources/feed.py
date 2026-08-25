"""RSS and Atom polling.

Where a publisher offers a feed of their live posts, this is the option with no
terms-of-service ambiguity: a feed exists to be syndicated. The tradeoff is
cadence — feeds update on the publisher's schedule, which for some is per-post
and for others is per-article.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from race_bot.models.commentary import CommentaryPost
from race_bot.sources.http import PoliteFetcher

_ATOM = "{http://www.w3.org/2005/Atom}"


def _text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _parse_date(raw: str, fallback: datetime) -> datetime:
    raw = raw.strip()
    if not raw:
        return fallback
    for parse in (_parse_rfc822, _parse_iso):
        parsed = parse(raw)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


def _parse_rfc822(raw: str) -> datetime | None:
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _parse_iso(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_feed(xml: str, *, source_name: str, fetched_at: datetime) -> list[CommentaryPost]:
    """Parse RSS 2.0 or Atom into commentary posts, oldest first."""
    root = ElementTree.fromstring(xml)
    posts: list[CommentaryPost] = []

    entries = root.findall(f".//{_ATOM}entry") or root.findall(".//item")
    for entry in entries:
        is_atom = entry.tag.startswith(_ATOM)

        if is_atom:
            body = _text(entry.find(f"{_ATOM}content")) or _text(
                entry.find(f"{_ATOM}summary")
            )
            title = _text(entry.find(f"{_ATOM}title"))
            raw_date = _text(entry.find(f"{_ATOM}published")) or _text(
                entry.find(f"{_ATOM}updated")
            )
            raw_id = _text(entry.find(f"{_ATOM}id"))
        else:
            body = _text(entry.find("description"))
            title = _text(entry.find("title"))
            raw_date = _text(entry.find("pubDate"))
            raw_id = _text(entry.find("guid"))

        # A feed item's title often carries the actual update ("Wright attacks"),
        # with the body repeating or expanding it. Keep both, without duplication.
        text = title if not body else (body if title in body else f"{title}. {body}")
        text = text.strip()
        if not text:
            continue

        timestamp = _parse_date(raw_date, fetched_at)
        posts.append(
            CommentaryPost(
                id=f"{source_name}-{raw_id}" if raw_id
                else CommentaryPost.make_id(source_name, timestamp, text),
                timestamp=timestamp,
                text=text,
                source=source_name,
            )
        )

    posts.sort(key=lambda p: p.timestamp)
    return posts


class FeedSource:
    """Polls an RSS or Atom feed and emits newly seen entries."""

    def __init__(
        self,
        url: str,
        *,
        fetcher: PoliteFetcher | None = None,
        name: str = "feed",
        poll_seconds: float = 60.0,
    ) -> None:
        self.url = url
        self.name = name
        self.fetcher = fetcher or PoliteFetcher(min_interval_seconds=poll_seconds)
        self._owns_fetcher = fetcher is None
        self._seen: set[str] = set()
        self._finished = False
        self.poll_errors: list[str] = []

    @property
    def exhausted(self) -> bool:
        return self._finished

    def finish(self) -> None:
        self._finished = True

    async def poll(self) -> list[CommentaryPost]:
        try:
            result = await self.fetcher.fetch(self.url)
        except Exception as exc:  # noqa: BLE001 - a race outlives one bad fetch
            self._record_error(f"{type(exc).__name__}: {exc}")
            return []

        if not result.has_content:
            return []

        assert result.text is not None
        try:
            posts = parse_feed(
                result.text, source_name=self.name, fetched_at=datetime.now(UTC)
            )
        except ElementTree.ParseError as exc:
            self._record_error(f"malformed feed: {exc}")
            return []

        fresh = [p for p in posts if p.id not in self._seen]
        self._seen.update(p.id for p in fresh)
        return fresh

    def _record_error(self, message: str) -> None:
        if len(self.poll_errors) < 20:
            self.poll_errors.append(message)

    async def close(self) -> None:
        if self._owns_fetcher:
            await self.fetcher.close()
