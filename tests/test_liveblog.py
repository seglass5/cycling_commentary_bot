"""Config-driven live blog extraction."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from race_bot.sources.http import PoliteFetcher
from race_bot.sources.liveblog import (
    LiveBlogSource,
    SiteConfig,
    SiteConfigError,
    available_sites,
    extract_posts,
    load_site_config,
    parse_timestamp,
)

FETCHED_AT = datetime(2026, 4, 12, 16, 0, tzinfo=UTC)
ALLOW_ALL = "User-agent: *\nAllow: /\n"

PAGE = """
<html><body>
  <nav>Home | Live | Results</nav>
  <div class="liveblog">
    <article class="live-post" data-post-id="p2">
      <time datetime="2026-04-12T13:05:00Z">13:05</time>
      <span class="byline">Jan Reporter</span>
      <div class="post-body">The gap is 45 seconds with 20km to go</div>
    </article>
    <article class="live-post" data-post-id="p1">
      <time datetime="2026-04-12T13:01:00Z">13:01</time>
      <div class="post-body">A five-man move has gone clear</div>
    </article>
  </div>
</body></html>
"""


@pytest.fixture
def config():
    return SiteConfig.from_mapping(
        "demo",
        {
            "post_selector": "article.live-post",
            "text_selector": ".post-body",
            "timestamp_selector": "time",
            "timestamp_attribute": "datetime",
            "author_selector": ".byline",
            "id_attribute": "data-post-id",
        },
    )


# --- extraction ----------------------------------------------------------


def test_posts_are_extracted(config):
    posts = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    assert len(posts) == 2
    assert posts[0].text == "A five-man move has gone clear"


def test_posts_come_back_chronological(config):
    """Live blogs render newest first; the pipeline needs oldest first."""
    posts = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    assert [p.timestamp for p in posts] == sorted(p.timestamp for p in posts)


def test_navigation_chrome_is_not_picked_up(config):
    posts = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    assert not any("Results" in p.text for p in posts)


def test_stable_ids_come_from_the_configured_attribute(config):
    posts = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    assert {p.id for p in posts} == {"demo-p1", "demo-p2"}


def test_author_is_extracted_when_present(config):
    posts = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    by_id = {p.id: p for p in posts}
    assert by_id["demo-p2"].author == "Jan Reporter"
    assert by_id["demo-p1"].author is None


def test_missing_id_attribute_falls_back_to_content_hash():
    config = SiteConfig.from_mapping("demo", {"post_selector": "article.live-post"})
    posts = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    assert all(p.id.startswith("demo-") for p in posts)
    assert len({p.id for p in posts}) == 2


def test_content_hash_ids_are_stable_across_polls():
    config = SiteConfig.from_mapping("demo", {"post_selector": "article.live-post"})
    first = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    second = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    assert [p.id for p in first] == [p.id for p in second]


def test_no_timestamp_selector_stamps_at_fetch_time():
    config = SiteConfig.from_mapping("demo", {"post_selector": "article.live-post"})
    posts = extract_posts(PAGE, config, source_name="demo", fetched_at=FETCHED_AT)
    assert all(p.timestamp == FETCHED_AT for p in posts)


def test_empty_posts_are_skipped(config):
    html = '<article class="live-post"><div class="post-body">   </div></article>'
    assert extract_posts(html, config, source_name="d", fetched_at=FETCHED_AT) == []


def test_max_posts_per_poll_caps_a_runaway_selector():
    config = SiteConfig.from_mapping(
        "demo", {"post_selector": "p", "max_posts_per_poll": 3}
    )
    html = "<div>" + "".join(f"<p>Post number {i}</p>" for i in range(50)) + "</div>"
    assert len(extract_posts(html, config, source_name="d", fetched_at=FETCHED_AT)) == 3


def test_whitespace_is_normalised(config):
    html = (
        '<article class="live-post"><div class="post-body">The\n  gap   is\t45'
        "</div></article>"
    )
    posts = extract_posts(html, config, source_name="d", fetched_at=FETCHED_AT)
    assert posts[0].text == "The gap is 45"


# --- timestamps ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "fmt", "expected_hour"),
    [
        ("2026-04-12T13:05:00Z", "iso", 13),
        ("2026-04-12T13:05:00+00:00", "iso", 13),
        ("1776000000", "epoch", None),
        ("12/04/2026 13:05", "%d/%m/%Y %H:%M", 13),
    ],
)
def test_timestamp_formats(raw, fmt, expected_hour):
    parsed = parse_timestamp(raw, fmt, fallback=FETCHED_AT)
    assert parsed.tzinfo is not None
    if expected_hour is not None:
        assert parsed.hour == expected_hour


def test_unparseable_timestamp_falls_back():
    """A post with a wrong timestamp beats a post that was dropped."""
    assert parse_timestamp("half past three", "iso", fallback=FETCHED_AT) == FETCHED_AT


def test_empty_timestamp_falls_back():
    assert parse_timestamp("", "iso", fallback=FETCHED_AT) == FETCHED_AT


def test_naive_timestamp_is_treated_as_utc():
    assert parse_timestamp("2026-04-12T13:05:00", "iso", fallback=FETCHED_AT).tzinfo is not None


# --- configuration -------------------------------------------------------


def test_post_selector_is_required():
    with pytest.raises(SiteConfigError, match="post_selector"):
        SiteConfig.from_mapping("demo", {"text_selector": ".body"})


def test_unknown_settings_are_rejected():
    """A typo in a config should fail loudly, not silently do nothing."""
    with pytest.raises(SiteConfigError, match="unknown settings: post_selecter"):
        SiteConfig.from_mapping(
            "demo", {"post_selector": "article", "post_selecter": "typo"}
        )


def test_notes_field_is_allowed():
    config = SiteConfig.from_mapping(
        "demo", {"post_selector": "article", "notes": "markup changed in March"}
    )
    assert config.post_selector == "article"


def test_no_sites_ship_configured():
    """Which sites to poll is the operator's decision, not a shipped default."""
    assert available_sites() == []


def test_missing_site_config_names_the_readme(tmp_path):
    with pytest.raises(SiteConfigError, match="README"):
        load_site_config("nope", sites_dir=tmp_path)


def test_site_config_loads_from_disk(tmp_path):
    (tmp_path / "mysite.yaml").write_text(
        "post_selector: article.post\npoll_seconds: 45\n", encoding="utf-8"
    )
    config = load_site_config("mysite", sites_dir=tmp_path)
    assert config.post_selector == "article.post"
    assert config.poll_seconds == 45.0
    assert available_sites(sites_dir=tmp_path) == ["mysite"]


# --- the source ----------------------------------------------------------


def make_source(handler, config, **kwargs) -> LiveBlogSource:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = PoliteFetcher(
        client=client, min_interval_seconds=0.0, retry_base_seconds=0.0
    )
    return LiveBlogSource("https://example.com/live", config, fetcher=fetcher, **kwargs)


def page_handler(page: str = PAGE, robots: str = ALLOW_ALL):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(200, text=page)

    return handler


async def test_source_emits_posts(config):
    source = make_source(page_handler(), config)
    assert len(await source.poll()) == 2


async def test_source_does_not_re_emit_the_whole_page(config):
    """A live blog serves the entire race every poll."""
    source = make_source(page_handler(), config)
    await source.poll()
    assert await source.poll() == []


async def test_source_emits_only_new_posts(config):
    pages = [PAGE, PAGE.replace(
        '<div class="liveblog">',
        '<div class="liveblog"><article class="live-post" data-post-id="p3">'
        '<time datetime="2026-04-12T13:09:00Z">13:09</time>'
        '<div class="post-body">Echelons are forming</div></article>',
    )]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(200, text=pages.pop(0) if pages else PAGE)

    source = make_source(handler, config)
    await source.poll()
    second = await source.poll()

    assert [p.id for p in second] == ["demo-p3"]


async def test_source_survives_a_failed_fetch(config):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        raise httpx.ConnectError("network down", request=request)

    source = make_source(handler, config)
    assert await source.poll() == []
    assert source.poll_errors


async def test_source_refuses_a_disallowed_site(config):
    """robots.txt is enforced through the source, not just the fetcher."""
    source = make_source(
        page_handler(robots="User-agent: *\nDisallow: /\n"), config
    )
    assert await source.poll() == []
    assert any("robots" in e.lower() for e in source.poll_errors)


async def test_unchanged_page_yields_nothing(config):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text=PAGE, headers={"ETag": '"v1"'})

    source = make_source(handler, config)
    await source.poll()
    assert await source.poll() == []


async def test_a_live_source_is_never_exhausted(config):
    source = make_source(page_handler(), config)
    await source.poll()
    assert not source.exhausted
    source.finish()
    assert source.exhausted


async def test_malformed_markup_does_not_crash_the_race(config):
    source = make_source(page_handler(page="<<<not html>>>"), config)
    assert await source.poll() == []


async def test_unstable_site_ids_do_not_produce_duplicates(config):
    """Found by integration testing against a live blog whose ids shifted.

    Some sites number posts by position in a growing list, so the same post gets
    a new id on every poll. Trusting `id_attribute` alone filled the recording
    with duplicates.
    """
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        counter["n"] += 1
        # Same content, different ids each time.
        page = PAGE.replace("p1", f"shift{counter['n']}a").replace(
            "p2", f"shift{counter['n']}b"
        )
        return httpx.Response(200, text=page)

    source = make_source(handler, config)
    first = await source.poll()
    second = await source.poll()

    assert len(first) == 2
    assert second == [], "identical text under a new id is not a new post"


async def test_genuinely_new_text_still_gets_through_with_shifting_ids(config):
    pages = [
        PAGE,
        PAGE.replace("p1", "q1").replace("p2", "q2").replace(
            "A five-man move has gone clear", "Echelons are forming in the crosswind"
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(200, text=pages.pop(0) if pages else PAGE)

    source = make_source(handler, config)
    await source.poll()
    second = await source.poll()

    assert [p.text for p in second] == ["Echelons are forming in the crosswind"]
