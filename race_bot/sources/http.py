"""Polite HTTP fetching.

Everything that touches a third-party site goes through here, so the rules are in
one place and cannot be bypassed by a site adapter:

- robots.txt is fetched, cached and honoured. There is no override flag.
- Requests to a host are spaced by at least `min_interval_seconds`, and by the
  host's own Crawl-delay when it declares a longer one.
- Conditional requests (ETag / If-Modified-Since) mean an unchanged live blog
  costs a 304 rather than a full page.
- The User-Agent identifies the bot and carries a contact URL.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

DEFAULT_USER_AGENT = (
    "race-bot/0.1 (+https://github.com/seglass5/cycling_commentary_bot)"
)
DEFAULT_MIN_INTERVAL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 3


class RobotsDisallowed(RuntimeError):
    """The site's robots.txt does not permit fetching this URL."""


@dataclass
class FetchResult:
    """One fetch. `text` is None when the server said nothing changed."""

    status_code: int
    text: str | None = None
    not_modified: bool = False

    @property
    def has_content(self) -> bool:
        return self.text is not None


@dataclass
class _HostState:
    """Per-host politeness bookkeeping."""

    robots: RobotFileParser | None = None
    robots_checked: bool = False
    robots_available: bool = True
    """False when robots.txt could not be read — we then refuse, not proceed."""

    crawl_delay: float | None = None
    last_request_at: float = 0.0


@dataclass
class _CacheEntry:
    etag: str | None = None
    last_modified: str | None = None


class PoliteFetcher:
    """An HTTP client that will not embarrass you on someone else's server."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retry_base_seconds: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.min_interval_seconds = min_interval_seconds
        self.retry_base_seconds = retry_base_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self._hosts: dict[str, _HostState] = {}
        self._cache: dict[str, _CacheEntry] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- robots ---------------------------------------------------------

    async def _host_state(self, url: str) -> _HostState:
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc}"
        state = self._hosts.setdefault(key, _HostState())
        if state.robots_checked:
            return state

        state.robots_checked = True
        robots_url = urljoin(key, "/robots.txt")
        try:
            response = await self._client.get(robots_url)
        except httpx.HTTPError:
            # Cannot read the rules, so we do not get to guess at them.
            state.robots_available = False
            return state

        if response.status_code >= 500:
            # RFC 9309: a server error means treat the site as disallowed.
            state.robots_available = False
            return state

        parser = RobotFileParser()
        if response.status_code >= 400:
            # No robots.txt is a grant of access, by long-standing convention.
            parser.parse([])
        else:
            parser.parse(response.text.splitlines())

        state.robots = parser
        state.crawl_delay = _crawl_delay(parser, self.user_agent)
        return state

    async def allowed(self, url: str) -> bool:
        state = await self._host_state(url)
        if not state.robots_available:
            return False
        if state.robots is None:
            return True
        return state.robots.can_fetch(self.user_agent, url)

    # --- fetching -------------------------------------------------------

    async def _wait_turn(self, state: _HostState) -> None:
        interval = max(self.min_interval_seconds, state.crawl_delay or 0.0)
        elapsed = time.monotonic() - state.last_request_at
        if state.last_request_at and elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        state.last_request_at = time.monotonic()

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a URL, honouring robots.txt, rate limits and caching.

        Raises `RobotsDisallowed` if the site says no. Returns a result with
        `not_modified` set when the server reports no change.
        """
        state = await self._host_state(url)
        if not await self.allowed(url):
            raise RobotsDisallowed(
                f"robots.txt does not permit fetching {url} as {self.user_agent!r}"
            )

        headers: dict[str, str] = {}
        cached = self._cache.get(url)
        if cached:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            await self._wait_turn(state)
            try:
                response = await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(self.retry_base_seconds * 2**attempt)
                continue

            if response.status_code == 304:
                return FetchResult(status_code=304, not_modified=True)

            if response.status_code == 429 or response.status_code >= 500:
                # Back off, and obey Retry-After when the server sets it.
                delay = _retry_after(response)
                if delay is None:
                    delay = self.retry_base_seconds * 2**attempt
                last_error = httpx.HTTPStatusError(
                    f"{response.status_code} from {url}",
                    request=response.request,
                    response=response,
                )
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(delay)
                continue

            response.raise_for_status()
            self._cache[url] = _CacheEntry(
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
            return FetchResult(status_code=response.status_code, text=response.text)

        assert last_error is not None
        raise last_error


def _crawl_delay(parser: RobotFileParser, user_agent: str) -> float | None:
    try:
        delay = parser.crawl_delay(user_agent)
    except (AttributeError, ValueError):
        return None
    return float(delay) if delay is not None else None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        # HTTP-date form. Not worth parsing precisely; a fixed pause is fine.
        return 10.0
