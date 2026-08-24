"""Event lifecycle and dedup.

This is what separates a useful bot from a spammy one. A breakaway forming
produces the same tactical read on twenty consecutive ticks; without this, the
user gets twenty identical callouts. Here, the first one opens an event and the
rest quietly update it.
"""

from __future__ import annotations

from datetime import datetime

from race_bot.models.tactics import (
    EventStatus,
    EventUpdate,
    TacticalEvent,
    TacticalPattern,
)

P = TacticalPattern

SUPERSEDES: dict[TacticalPattern, tuple[TacticalPattern, ...]] = {
    # A recognised move resolves the earlier, more tentative read of it.
    P.BREAKAWAY_ESTABLISHED: (P.BREAKAWAY_ATTEMPT,),
    P.BREAKAWAY_CAUGHT: (P.BREAKAWAY_ATTEMPT, P.BREAKAWAY_ESTABLISHED, P.CHASE_ORGANISED),
    P.ECHELONS_FORMING: (P.ECHELON_RISK,),
    P.SPLIT_CONFIRMED: (P.ECHELON_RISK, P.ECHELONS_FORMING),
    P.LEADOUT_ESTABLISHED: (P.LEADOUT_FORMING,),
    P.SPRINT_LAUNCHED: (P.LEADOUT_FORMING, P.LEADOUT_ESTABLISHED, P.CLASSICS_POSITIONING),
}
"""Which earlier patterns a new one resolves, when they share participants."""

FAILS: dict[TacticalPattern, tuple[TacticalPattern, ...]] = {
    # A move that visibly did not come off. Kept and shown, not deleted: a chase
    # that collapses is tactical information in its own right.
    P.CHASE_COLLAPSING: (P.CHASE_ORGANISED,),
    P.BREAKAWAY_CAUGHT: (P.SOLO_BID,),
}

CONFIDENCE_STEP = 0.15
"""How much confidence must move before a re-report is worth showing again."""


class EventTracker:
    """Holds open events and decides what is worth surfacing."""

    def __init__(self) -> None:
        self._events: list[TacticalEvent] = []
        self._by_key: dict[tuple[str, frozenset[str]], TacticalEvent] = {}

    @property
    def all_events(self) -> list[TacticalEvent]:
        return list(self._events)

    @property
    def open_events(self) -> list[TacticalEvent]:
        return [e for e in self._events if e.is_open]

    def ingest(self, incoming: TacticalEvent, *, now: datetime) -> TacticalEvent | None:
        """Record an event.

        Returns the event if it is worth showing the user — either new, or a
        meaningful change to one already open — and None if it is a duplicate.
        """
        self._resolve_superseded(incoming, now)

        existing = self._by_key.get(incoming.key())
        if existing is None or not existing.is_open:
            return self._open(incoming, now)

        return self._update(existing, incoming, now)

    def _open(self, event: TacticalEvent, now: datetime) -> TacticalEvent:
        event.created_at = now
        event.updated_at = now
        event.history = [
            EventUpdate(
                at=now,
                status=event.status,
                note=event.headline,
                evidence_post_ids=list(event.evidence_post_ids),
            )
        ]
        self._events.append(event)
        self._by_key[event.key()] = event
        return event

    def _update(
        self, existing: TacticalEvent, incoming: TacticalEvent, now: datetime
    ) -> TacticalEvent | None:
        status_advanced = _status_rank(incoming.status) > _status_rank(existing.status)
        confidence_moved = abs(incoming.confidence - existing.confidence) >= CONFIDENCE_STEP
        new_evidence = [
            pid for pid in incoming.evidence_post_ids if pid not in existing.evidence_post_ids
        ]

        existing.evidence_post_ids.extend(new_evidence)
        for rider in incoming.participants:
            if rider not in existing.participants:
                existing.participants.append(rider)
        for team in incoming.teams:
            if team not in existing.teams:
                existing.teams.append(team)
        existing.confidence = incoming.confidence
        existing.updated_at = now
        # km_to_go is deliberately not updated: it records where the move
        # started, which is what a race timeline needs.

        if not (status_advanced or confidence_moved):
            # Same read as before. Quietly absorbed.
            return None

        existing.status = incoming.status if status_advanced else existing.status
        existing.headline = incoming.headline
        if incoming.explanation:
            existing.explanation = incoming.explanation
        existing.history.append(
            EventUpdate(
                at=now,
                status=existing.status,
                note=incoming.headline,
                evidence_post_ids=new_evidence,
            )
        )
        return existing

    def _resolve_superseded(self, incoming: TacticalEvent, now: datetime) -> None:
        """Close out earlier events that this one answers."""
        resolving = SUPERSEDES.get(incoming.pattern, ())
        failing = FAILS.get(incoming.pattern, ())
        if not resolving and not failing:
            return

        who = incoming.key()[1]
        for event in self._events:
            if not event.is_open:
                continue
            if event.pattern not in resolving and event.pattern not in failing:
                continue
            # Participant-free events (team-level reads) are matched on pattern
            # alone; named ones must actually overlap.
            if who and event.key()[1] and not (who & event.key()[1]):
                continue

            event.status = (
                EventStatus.FAILED if event.pattern in failing else EventStatus.RESOLVED
            )
            event.updated_at = now
            event.history.append(
                EventUpdate(
                    at=now,
                    status=event.status,
                    note=f"superseded by {incoming.pattern.value}",
                )
            )


def _status_rank(status: EventStatus) -> int:
    return {
        EventStatus.SUSPECTED: 0,
        EventStatus.CONFIRMED: 1,
        EventStatus.RESOLVED: 2,
        EventStatus.FAILED: 2,
    }[status]
