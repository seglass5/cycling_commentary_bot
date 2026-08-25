# Site configurations

Each `<name>.yaml` here describes how to read posts out of one live blog's
markup. `race-bot follow --site <name> --url <url>` uses it.

**No site is configured here.** That is deliberate.

## Before you add one

Polling somebody else's live blog is a decision about their terms, not a
technical question, and it is yours to make. Before adding a config:

1. **Read the site's terms of service.** Some publishers prohibit automated
   access outright. A permissive robots.txt is not permission if the terms say
   otherwise.
2. **Check robots.txt.** The bot enforces it — there is no override flag — but
   check it yourself before investing in selectors.
3. **Set a realistic `poll_seconds`.** Live blogs update every minute or two at
   most. Polling faster gains nothing and costs the publisher.
4. **Consider asking.** Many publishers will say yes to a non-commercial project
   that identifies itself, and some offer a feed that removes the question
   entirely — see `FeedSource`.

The bot always identifies itself in its User-Agent, honours robots.txt including
`Crawl-delay`, spaces its requests, and uses conditional requests so an unchanged
page costs a 304 rather than a full fetch.

## Format

```yaml
# Required: selects one element per commentary post.
post_selector: "article.live-post"

# Optional: the post body within that element. Defaults to the whole element.
text_selector: ".post-body"

# Optional: where the timestamp lives.
timestamp_selector: "time"
timestamp_attribute: datetime     # read this attribute instead of the text
timestamp_format: iso             # iso | epoch | a strptime pattern

# Optional.
author_selector: ".byline"
id_attribute: "data-post-id"      # stable id; falls back to a content hash

# Politeness. Requests are spaced by at least this, or the site's Crawl-delay
# if longer.
poll_seconds: 30

# Guard against a selector that accidentally matches the whole page.
max_posts_per_poll: 200
```

Only `post_selector` is required. Everything else degrades sensibly: with no
timestamp selector, posts are stamped at fetch time, which is close enough for a
live race and keeps ordering correct.

## Working out the selectors

```bash
# Record a few polls, then look at what you actually got
race-bot record --site mysite --url https://... --out fixtures/races/test.jsonl --polls 3
race-bot inspect fixtures/races/test.jsonl
```

If `inspect` shows empty or duplicated text, the selectors need work. If it shows
navigation chrome and sponsor blocks, tighten `post_selector` — the noise
classifier will catch some of it, but it is cheaper to not fetch it.
