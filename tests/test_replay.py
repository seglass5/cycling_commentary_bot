from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from race_bot.sources.replay import ReplaySource

BASE = datetime(2026, 4, 12, 12, 0, tzinfo=UTC)


def write_transcript(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


@pytest.fixture
def transcript(tmp_path):
    entries = [
        {"timestamp": (BASE + timedelta(minutes=m)).isoformat(), "text": f"Post at minute {m}"}
        for m in (0, 5, 10)
    ]
    return write_transcript(tmp_path / "race.jsonl", entries)


async def test_speed_zero_returns_everything_at_once(transcript):
    source = ReplaySource(transcript, speed=0)
    posts = await source.poll()
    assert len(posts) == 3
    assert source.exhausted


async def test_posts_sorted_by_timestamp(tmp_path):
    entries = [
        {"timestamp": (BASE + timedelta(minutes=m)).isoformat(), "text": f"minute {m}"}
        for m in (10, 0, 5)
    ]
    source = ReplaySource(write_transcript(tmp_path / "r.jsonl", entries), speed=0)
    posts = await source.poll()
    assert [p.text for p in posts] == ["minute 0", "minute 5", "minute 10"]


async def test_paced_replay_releases_gradually(transcript):
    source = ReplaySource(transcript, speed=1)
    first = await source.poll()
    # Only the post at the race start is due immediately.
    assert len(first) == 1
    assert not source.exhausted


async def test_exhausted_source_returns_empty(transcript):
    source = ReplaySource(transcript, speed=0)
    await source.poll()
    assert await source.poll() == []


def test_generated_ids_are_stable(tmp_path, transcript):
    a = ReplaySource(transcript, speed=0)
    b = ReplaySource(transcript, speed=0)
    assert [p.id for p in a._posts] == [p.id for p in b._posts]


def test_explicit_ids_are_preserved(tmp_path):
    entries = [{"id": "custom-1", "timestamp": BASE.isoformat(), "text": "hello there"}]
    source = ReplaySource(write_transcript(tmp_path / "r.jsonl", entries), speed=0)
    assert source._posts[0].id == "custom-1"


def test_naive_timestamps_treated_as_utc(tmp_path):
    entries = [{"timestamp": "2026-04-12T12:00:00", "text": "naive timestamp here"}]
    source = ReplaySource(write_transcript(tmp_path / "r.jsonl", entries), speed=0)
    assert source._posts[0].timestamp.tzinfo is not None


def test_blank_lines_and_comments_ignored(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text(
        f'# a comment\n\n{{"timestamp": "{BASE.isoformat()}", "text": "real post here"}}\n',
        encoding="utf-8",
    )
    assert ReplaySource(path, speed=0).post_count == 1


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplaySource(tmp_path / "nope.jsonl")


def test_malformed_json_names_the_line(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text(
        f'{{"timestamp": "{BASE.isoformat()}", "text": "a valid post"}}\nnot json\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r":2:"):
        ReplaySource(path)


def test_missing_required_field_raises(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text('{"text": "no timestamp"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="timestamp"):
        ReplaySource(path)
