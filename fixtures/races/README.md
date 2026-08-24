# Fixtures

JSONL transcripts, one post per line:

```json
{"timestamp": "2026-04-12T12:30:00+00:00", "text": "...", "author": "optional", "id": "optional"}
```

`timestamp` and `text` are required. Posts without an `id` get a stable content
hash, so re-polling the same post is recognised as a duplicate.

## kustklassieker_2026.jsonl

**Synthetic.** A fictional coastal classic, hand-written to exercise the
pipeline: early attacks that fail, a five-man breakaway, a crosswind section
producing echelons and a decisive split, a crash, a solo bid from the break, the
catch, lead-out trains, and a bunch sprint. Also contains sponsor filler and a
results post so the noise classifier has something to reject.

Useful for development, and as a smoke test. **Not** a substitute for evaluation:
it was written by the same process being tested, so it cannot tell us whether the
system works on real commentary. Phase 5 needs archived transcripts from actual
live blogs.
