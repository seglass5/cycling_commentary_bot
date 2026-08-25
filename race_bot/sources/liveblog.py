"""Config-driven live blog polling.

Adding a site is a YAML file, not Python. That keeps the fragile part — CSS
selectors against markup somebody else controls — as data that can be fixed
without a release, and it keeps the decision about *which* sites to poll with
whoever is running the bot.

No site configuration ships enabled. See `sources/sites/README.md`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from bs4 import BeautifulSoup, Tag

from race_bot.models.commentary import CommentaryPost
from race_bot.sources.http import PoliteFetcher

SITES_DIR = Path(__file__).parent / "sites"


class SiteConfigError(ValueError):
    """The site configuration is missing something or malformed."""


@dataclass(frozen=True)
class SiteConfig:
    """How to read posts out of one site's live blog markup."""

    name: str
    post_selector: str
    text_selector: str | None = None
    timestamp_selector: str | None = None
    timestamp_attribute: str | None = None
    """Read the timestamp from this attribute (e.g. `datetime`) instead of the text."""

    timestamp_format: str = "iso"
    """`iso`, `epoch`, or a strptime pattern."""

    author_selector: str | None = None
    id_attribute: str | None = None
    """Attribute holding a stable post id. Falls back to a content hash."""

    poll_seconds: float = 30.0
    max_posts_per_poll: int = 200
    """Guard against a selector that accidentally matches the whole page."""

    @classmethod
    def from_mapping(cls, name: str, raw: dict[str, Any]) -> SiteConfig:
        if not isinstance(raw, dict):
            raise SiteConfigError(f"{name}: expected a mapping")
        if not raw.get("post_selector"):
            raise SiteConfigError(f"{name}: 'post_selector' is required")

        known = {f for f in cls.__dataclass_fields__ if f != "name"}
        unknown = set(raw) - known - {"url", "notes"}
        if unknown:
            raise SiteConfigError(
                f"{name}: unknown settings: {', '.join(sorted(unknown))}"
            )

        return cls(
            name=name,
            post_selector=str(raw["post_selector"]),
            text_selector=raw.get("text_selector"),
            timestamp_selector=raw.get("timestamp_selector"),
            timestamp_attribute=raw.get("timestamp_attribute"),
            timestamp_format=str(raw.get("timestamp_format", "iso")),
            author_selector=raw.get("author_selector"),
            id_attribute=raw.get("id_attribute"),
            poll_seconds=float(raw.get("poll_seconds", 30.0)),
            max_posts_per_poll=int(raw.get("max_posts_per_poll", 200)),
        )


def load_site_config(name: str, *, sites_dir: Path | None = None) -> SiteConfig:
    path = (sites_dir or SITES_DIR) / f"{name}.yaml"
    if not path.exists():
        available = available_sites(sites_dir=sites_dir)
        raise SiteConfigError(
            f"no site configuration named {name!r}. "
            + (f"Available: {', '.join(available)}" if available else
               "None are configured — see race_bot/sources/sites/README.md")
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SiteConfig.from_mapping(name, raw)


def load_site_config_file(path: str | Path) -> SiteConfig:
    """Load a site config from an arbitrary path.

    Configs do not have to live inside the package — keeping them in your own
    repo means selectors can be fixed without touching an installed dependency.
    """
    path = Path(path)
    if not path.exists():
        raise SiteConfigError(f"no site configuration at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SiteConfig.from_mapping(path.stem, raw)


def available_sites(*, sites_dir: Path | None = None) -> list[str]:
    directory = sites_dir or SITES_DIR
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def parse_timestamp(raw: str, fmt: str, *, fallback: datetime) -> datetime:
    """Best-effort timestamp parsing.

    Live blogs are inconsistent about this, and a post with a slightly wrong
    timestamp is far better than a post dropped — ordering only has to be good
    enough for the window to read chronologically.
    """
    raw = raw.strip()
    if not raw:
        return fallback

    try:
        if fmt == "epoch":
            return datetime.fromtimestamp(float(raw), tz=UTC)
        parsed = (
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if fmt == "iso"
            else datetime.strptime(raw, fmt)
        )
    except ValueError:
        return fallback

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def extract_posts(
    html: str, config: SiteConfig, *, source_name: str, fetched_at: datetime
) -> list[CommentaryPost]:
    """Pull commentary posts out of a live blog page."""
    soup = BeautifulSoup(html, "lxml")
    posts: list[CommentaryPost] = []

    for element in soup.select(config.post_selector)[: config.max_posts_per_poll]:
        text = _extract_text(element, config.text_selector)
        if not text:
            continue

        timestamp = _extract_timestamp(element, config, fallback=fetched_at)
        author = _extract_text(element, config.author_selector) if config.author_selector else None

        post_id = None
        if config.id_attribute:
            raw_id = element.get(config.id_attribute)
            if raw_id:
                post_id = f"{source_name}-{raw_id}"

        posts.append(
            CommentaryPost(
                id=post_id or CommentaryPost.make_id(source_name, timestamp, text),
                timestamp=timestamp,
                text=text,
                source=source_name,
                author=author or None,
            )
        )

    posts.sort(key=lambda p: p.timestamp)
    return posts


def _fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def _extract_text(element: Tag, selector: str | None) -> str:
    target: Tag | None = element
    if selector:
        found = element.select_one(selector)
        if found is None:
            return ""
        target = found
    return " ".join(target.get_text(" ", strip=True).split())


def _extract_timestamp(
    element: Tag, config: SiteConfig, *, fallback: datetime
) -> datetime:
    if not config.timestamp_selector:
        return fallback

    node = element.select_one(config.timestamp_selector)
    if node is None:
        return fallback

    raw = (
        str(node.get(config.timestamp_attribute) or "")
        if config.timestamp_attribute
        else node.get_text(" ", strip=True)
    )
    return parse_timestamp(raw, config.timestamp_format, fallback=fallback)


class LiveBlogSource:
    """Polls a live blog page and emits newly seen posts.

    Deduplication happens here as well as in the pipeline: a live blog serves the
    whole page every time, so without it every poll would re-emit the entire race.

    Posts are tracked by id *and* by a fingerprint of their text. A site's
    `id_attribute` is not always as stable as it looks — an index that shifts as
    the page grows will produce a fresh id for the same post on every poll — and
    trusting it alone fills a recording with duplicates. The cost is that a race
    posting the same sentence twice reports it once, which is the better trade.
    """

    def __init__(
        self,
        url: str,
        config: SiteConfig,
        *,
        fetcher: PoliteFetcher | None = None,
        name: str | None = None,
    ) -> None:
        self.url = url
        self.config = config
        self.name = name or config.name
        self.fetcher = fetcher or PoliteFetcher(min_interval_seconds=config.poll_seconds)
        self._owns_fetcher = fetcher is None
        self._seen: set[str] = set()
        self._seen_text: set[str] = set()
        self._finished = False
        self.poll_errors: list[str] = []

    @property
    def exhausted(self) -> bool:
        # A live blog has no end we can detect. The run stops when the operator
        # stops it, or when `finish()` is called.
        return self._finished

    def finish(self) -> None:
        self._finished = True

    async def poll(self) -> list[CommentaryPost]:
        try:
            result = await self.fetcher.fetch(self.url)
        except Exception as exc:  # noqa: BLE001 - a live race outlives one bad fetch
            self._record_error(f"{type(exc).__name__}: {exc}")
            return []

        if not result.has_content:
            return []

        assert result.text is not None
        try:
            posts = extract_posts(
                result.text,
                self.config,
                source_name=self.name,
                fetched_at=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001 - markup changes should not crash a race
            self._record_error(f"extraction failed: {type(exc).__name__}: {exc}")
            return []

        fresh = []
        for post in posts:
            fingerprint = _fingerprint(post.text)
            if post.id in self._seen or fingerprint in self._seen_text:
                continue
            self._seen.add(post.id)
            self._seen_text.add(fingerprint)
            fresh.append(post)
        return fresh

    def _record_error(self, message: str) -> None:
        if len(self.poll_errors) < 20:
            self.poll_errors.append(message)

    async def close(self) -> None:
        if self._owns_fetcher:
            await self.fetcher.close()
