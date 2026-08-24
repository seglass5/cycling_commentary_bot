from __future__ import annotations

from datetime import UTC, datetime

import pytest

from race_bot.models.tactics import EventStatus, TacticalEvent, TacticalPattern
from race_bot.pipeline.tracker import EventTracker

NOW = datetime(2026, 4, 12, 14, 0, tzinfo=UTC)
P = TacticalPattern


def event(pattern, *, status=EventStatus.SUSPECTED, confidence=0.5, riders=(), evidence=()):
    return TacticalEvent(
        pattern=pattern,
        status=status,
        confidence=confidence,
        headline=f"{pattern.value} headline",
        participants=list(riders),
        evidence_post_ids=list(evidence),
    )


@pytest.fixture
def tracker():
    return EventTracker()


def test_first_event_is_surfaced(tracker):
    assert tracker.ingest(event(P.SOLO_BID, riders=["A Rider"]), now=NOW) is not None


def test_repeat_of_same_read_is_absorbed(tracker):
    tracker.ingest(event(P.SOLO_BID, riders=["A Rider"]), now=NOW)
    repeat = tracker.ingest(event(P.SOLO_BID, riders=["A Rider"], confidence=0.55), now=NOW)
    assert repeat is None
    assert len(tracker.all_events) == 1


def test_confidence_jump_is_surfaced(tracker):
    tracker.ingest(event(P.SOLO_BID, riders=["A Rider"], confidence=0.4), now=NOW)
    assert tracker.ingest(
        event(P.SOLO_BID, riders=["A Rider"], confidence=0.8), now=NOW
    ) is not None


def test_status_advance_is_surfaced(tracker):
    tracker.ingest(event(P.SOLO_BID, riders=["A Rider"]), now=NOW)
    updated = tracker.ingest(
        event(P.SOLO_BID, riders=["A Rider"], status=EventStatus.CONFIRMED), now=NOW
    )
    assert updated is not None
    assert updated.status is EventStatus.CONFIRMED


def test_evidence_accumulates_even_when_absorbed(tracker):
    tracker.ingest(event(P.SOLO_BID, riders=["A Rider"], evidence=["p1"]), now=NOW)
    tracker.ingest(event(P.SOLO_BID, riders=["A Rider"], evidence=["p2"]), now=NOW)
    assert tracker.all_events[0].evidence_post_ids == ["p1", "p2"]


def test_km_to_go_records_where_the_move_started(tracker):
    first = event(P.SOLO_BID, riders=["A Rider"])
    first.km_to_go = 24
    tracker.ingest(first, now=NOW)

    later = event(P.SOLO_BID, riders=["A Rider"], status=EventStatus.CONFIRMED)
    later.km_to_go = 12
    tracker.ingest(later, now=NOW)

    assert tracker.all_events[0].km_to_go == 24


def test_successor_pattern_resolves_predecessor(tracker):
    tracker.ingest(event(P.BREAKAWAY_ATTEMPT, riders=["A Rider"]), now=NOW)
    tracker.ingest(event(P.BREAKAWAY_ESTABLISHED, riders=["A Rider"]), now=NOW)

    attempt = next(e for e in tracker.all_events if e.pattern is P.BREAKAWAY_ATTEMPT)
    assert attempt.status is EventStatus.RESOLVED
    assert len(tracker.open_events) == 1


def test_failed_move_is_kept_not_deleted(tracker):
    tracker.ingest(event(P.CHASE_ORGANISED, riders=["A Rider"]), now=NOW)
    tracker.ingest(event(P.CHASE_COLLAPSING, riders=["A Rider"]), now=NOW)

    chase = next(e for e in tracker.all_events if e.pattern is P.CHASE_ORGANISED)
    assert chase.status is EventStatus.FAILED
    assert chase in tracker.all_events


def test_supersession_requires_participant_overlap(tracker):
    tracker.ingest(event(P.BREAKAWAY_ATTEMPT, riders=["A Rider"]), now=NOW)
    tracker.ingest(event(P.BREAKAWAY_ESTABLISHED, riders=["Z Rider"]), now=NOW)

    attempt = next(e for e in tracker.all_events if e.pattern is P.BREAKAWAY_ATTEMPT)
    assert attempt.status is EventStatus.SUSPECTED


def test_situational_patterns_dedup_regardless_of_riders(tracker):
    """Echelons forming is one situation however many riders get named."""
    assert tracker.ingest(event(P.ECHELONS_FORMING, riders=["A Rider"]), now=NOW) is not None
    assert tracker.ingest(event(P.ECHELONS_FORMING, riders=["B Rider"]), now=NOW) is None
    assert len(tracker.all_events) == 1


def test_rider_keyed_patterns_stay_distinct(tracker):
    tracker.ingest(event(P.SOLO_BID, riders=["A Rider"]), now=NOW)
    tracker.ingest(event(P.SOLO_BID, riders=["Z Rider"]), now=NOW)
    assert len(tracker.all_events) == 2


def test_closed_event_reopens_rather_than_updating(tracker):
    tracker.ingest(event(P.BREAKAWAY_ATTEMPT, riders=["A Rider"]), now=NOW)
    tracker.ingest(event(P.BREAKAWAY_ESTABLISHED, riders=["A Rider"]), now=NOW)
    reopened = tracker.ingest(event(P.BREAKAWAY_ATTEMPT, riders=["A Rider"]), now=NOW)
    assert reopened is not None
    assert len(tracker.all_events) == 3


def test_history_records_each_surfaced_change(tracker):
    tracker.ingest(event(P.SOLO_BID, riders=["A Rider"], confidence=0.3), now=NOW)
    tracker.ingest(
        event(P.SOLO_BID, riders=["A Rider"], confidence=0.9, status=EventStatus.CONFIRMED),
        now=NOW,
    )
    assert len(tracker.all_events[0].history) == 2
