"""The polite fetcher. No real network — httpx.MockTransport throughout."""

from __future__ import annotations

import httpx
import pytest

from race_bot.sources.http import PoliteFetcher, RobotsDisallowed

ALLOW_ALL = "User-agent: *\nAllow: /\n"
DISALLOW_LIVE = "User-agent: *\nDisallow: /live/\n"


def make_fetcher(handler, **kwargs) -> PoliteFetcher:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": kwargs.get("user_agent", "race-bot/test")},
    )
    kwargs.setdefault("user_agent", "race-bot/test")
    kwargs.setdefault("min_interval_seconds", 0.0)
    kwargs.setdefault("retry_base_seconds", 0.0)
    return PoliteFetcher(client=client, **kwargs)


def routes(robots: str | None, *, robots_status: int = 200, page: str = "<html>hi</html>"):
    """A handler serving robots.txt and one page."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/robots.txt":
            if robots is None:
                raise httpx.ConnectError("robots unreachable", request=request)
            return httpx.Response(robots_status, text=robots or "")
        return httpx.Response(200, text=page)

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


# --- robots.txt ----------------------------------------------------------


async def test_allowed_path_is_fetched():
    fetcher = make_fetcher(routes(ALLOW_ALL))
    result = await fetcher.fetch("https://example.com/live/race")
    assert result.status_code == 200
    assert result.text == "<html>hi</html>"


async def test_disallowed_path_is_refused():
    fetcher = make_fetcher(routes(DISALLOW_LIVE))
    with pytest.raises(RobotsDisallowed):
        await fetcher.fetch("https://example.com/live/race")


async def test_missing_robots_allows_access():
    """No robots.txt is a grant of access by long-standing convention."""
    fetcher = make_fetcher(routes("", robots_status=404))
    assert await fetcher.allowed("https://example.com/live/race")


async def test_server_error_on_robots_fails_closed():
    """RFC 9309: a 5xx on robots.txt means treat the site as disallowed."""
    fetcher = make_fetcher(routes("", robots_status=503))
    assert not await fetcher.allowed("https://example.com/live/race")
    with pytest.raises(RobotsDisallowed):
        await fetcher.fetch("https://example.com/live/race")


async def test_unreachable_robots_fails_closed():
    """If we cannot read the rules, we do not get to guess at them."""
    fetcher = make_fetcher(routes(None))
    assert not await fetcher.allowed("https://example.com/live/race")


async def test_robots_is_fetched_once_per_host():
    handler = routes(ALLOW_ALL)
    fetcher = make_fetcher(handler)
    await fetcher.fetch("https://example.com/live/a")
    await fetcher.fetch("https://example.com/live/b")

    robots_calls = [r for r in handler.calls if r.url.path == "/robots.txt"]
    assert len(robots_calls) == 1


async def test_robots_is_checked_per_host():
    handler = routes(ALLOW_ALL)
    fetcher = make_fetcher(handler)
    await fetcher.fetch("https://a.example.com/live")
    await fetcher.fetch("https://b.example.com/live")

    robots_calls = [r for r in handler.calls if r.url.path == "/robots.txt"]
    assert len(robots_calls) == 2


async def test_crawl_delay_is_read_from_robots():
    robots = "User-agent: *\nAllow: /\nCrawl-delay: 42\n"
    fetcher = make_fetcher(routes(robots))
    await fetcher.allowed("https://example.com/live")

    state = fetcher._hosts["https://example.com"]
    assert state.crawl_delay == 42.0


# --- conditional requests -----------------------------------------------


async def test_etag_is_sent_on_the_second_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        if request.headers.get("If-None-Match") == '"abc"':
            return httpx.Response(304)
        return httpx.Response(200, text="page", headers={"ETag": '"abc"'})

    fetcher = make_fetcher(handler)
    first = await fetcher.fetch("https://example.com/live")
    second = await fetcher.fetch("https://example.com/live")

    assert first.has_content
    assert second.not_modified
    assert second.text is None


async def test_last_modified_is_sent_on_the_second_request():
    stamp = "Sun, 12 Apr 2026 12:00:00 GMT"
    sent: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        sent.append(request.headers.get("If-Modified-Since"))
        return httpx.Response(200, text="page", headers={"Last-Modified": stamp})

    fetcher = make_fetcher(handler)
    await fetcher.fetch("https://example.com/live")
    await fetcher.fetch("https://example.com/live")

    assert sent == [None, stamp]


# --- retries -------------------------------------------------------------


async def test_transient_server_error_is_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="recovered")

    fetcher = make_fetcher(handler)
    # Retry-After of 0 keeps the test fast.
    result = await fetcher.fetch("https://example.com/live")

    assert result.text == "recovered"
    assert attempts["n"] == 2


async def test_persistent_failure_eventually_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(500, headers={"Retry-After": "0"})

    fetcher = make_fetcher(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await fetcher.fetch("https://example.com/live")


async def test_rate_limit_response_is_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text="ok")

    fetcher = make_fetcher(handler)
    assert (await fetcher.fetch("https://example.com/live")).text == "ok"


async def test_user_agent_identifies_the_bot():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("User-Agent", ""))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(200, text="page")

    fetcher = make_fetcher(handler, user_agent="race-bot/9.9 (+https://example.org)")
    await fetcher.fetch("https://example.com/live")

    assert all("race-bot" in ua for ua in seen)
