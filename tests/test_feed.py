"""RSS and Atom polling."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from race_bot.sources.feed import FeedSource, parse_feed
from race_bot.sources.http import PoliteFetcher

FETCHED_AT = datetime(2026, 4, 12, 16, 0, tzinfo=UTC)
ALLOW_ALL = "User-agent: *\nAllow: /\n"

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Kustklassieker live</title>
  <item>
    <title>Wright attacks</title>
    <description>He has gone alone with 24km to go</description>
    <pubDate>Sun, 12 Apr 2026 15:35:00 GMT</pubDate>
    <guid>post-2</guid>
  </item>
  <item>
    <title>Five clear</title>
    <description>A five-man move has gone clear</description>
    <pubDate>Sun, 12 Apr 2026 12:53:00 GMT</pubDate>
    <guid>post-1</guid>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Echelons forming</title>
    <content>The bunch is fanning across the road</content>
    <published>2026-04-12T14:15:00Z</published>
    <id>atom-1</id>
  </entry>
</feed>
"""


def test_rss_is_parsed():
    posts = parse_feed(RSS, source_name="t", fetched_at=FETCHED_AT)
    assert len(posts) == 2
    assert posts[0].id == "t-post-1"


def test_entries_come_back_chronological():
    posts = parse_feed(RSS, source_name="t", fetched_at=FETCHED_AT)
    assert [p.timestamp for p in posts] == sorted(p.timestamp for p in posts)


def test_title_and_body_are_combined():
    posts = parse_feed(RSS, source_name="t", fetched_at=FETCHED_AT)
    latest = posts[-1]
    assert "Wright attacks" in latest.text
    assert "24km to go" in latest.text


def test_title_is_not_duplicated_when_the_body_repeats_it():
    posts = parse_feed(RSS, source_name="t", fetched_at=FETCHED_AT)
    assert posts[0].text.count("Five clear") == 1


def test_rfc822_dates_are_parsed():
    posts = parse_feed(RSS, source_name="t", fetched_at=FETCHED_AT)
    assert posts[0].timestamp.hour == 12
    assert posts[0].timestamp.tzinfo is not None


def test_atom_is_parsed():
    posts = parse_feed(ATOM, source_name="t", fetched_at=FETCHED_AT)
    assert len(posts) == 1
    assert posts[0].id == "t-atom-1"
    assert posts[0].timestamp.hour == 14


def test_undated_entry_falls_back_to_fetch_time():
    feed = '<?xml version="1.0"?><rss><channel><item><title>No date</title>' \
           "<description>Something happened</description></item></channel></rss>"
    posts = parse_feed(feed, source_name="t", fetched_at=FETCHED_AT)
    assert posts[0].timestamp == FETCHED_AT


def test_empty_entries_are_skipped():
    feed = '<?xml version="1.0"?><rss><channel><item><title> </title>' \
           "<description> </description></item></channel></rss>"
    assert parse_feed(feed, source_name="t", fetched_at=FETCHED_AT) == []


def test_empty_feed_is_fine():
    feed = '<?xml version="1.0"?><rss><channel><title>Nothing</title></channel></rss>'
    assert parse_feed(feed, source_name="t", fetched_at=FETCHED_AT) == []


def make_source(body: str, **kwargs) -> FeedSource:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = PoliteFetcher(
        client=client, min_interval_seconds=0.0, retry_base_seconds=0.0
    )
    return FeedSource("https://example.com/feed.xml", fetcher=fetcher, **kwargs)


async def test_source_emits_entries():
    assert len(await make_source(RSS).poll()) == 2


async def test_source_deduplicates_across_polls():
    source = make_source(RSS)
    await source.poll()
    assert await source.poll() == []


async def test_malformed_feed_does_not_crash_the_race():
    source = make_source("<<< not xml")
    assert await source.poll() == []
    assert source.poll_errors


async def test_feed_source_is_never_exhausted():
    source = make_source(RSS)
    await source.poll()
    assert not source.exhausted
    source.finish()
    assert source.exhausted
