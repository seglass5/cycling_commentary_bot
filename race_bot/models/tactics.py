"""Tactical patterns and the events that report them."""

from __future__ import annotations

import itertools
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from race_bot.models.race_state import StateDelta


class TacticalPattern(StrEnum):
    """Recognised tactical situations.

    Definitions, commentary cues and confirming/contradicting signals for each of
    these live in `race_bot/knowledge/patterns.yaml`, not in code.
    """

    # Breakaway lifecycle
    BREAKAWAY_ATTEMPT = "breakaway_attempt"
    BREAKAWAY_ESTABLISHED = "breakaway_established"
    BREAKAWAY_CAUGHT = "breakaway_caught"

    # Peloton behaviour
    PELOTON_CONTROL = "peloton_control"
    CHASE_ORGANISED = "chase_organised"
    CHASE_COLLAPSING = "chase_collapsing"

    # Wind and splits
    ECHELON_RISK = "echelon_risk"
    ECHELONS_FORMING = "echelons_forming"
    SPLIT_CONFIRMED = "split_confirmed"

    # Classics
    CLASSICS_POSITIONING = "classics_positioning"
    COBBLE_SECTOR_APPROACH = "cobble_sector_approach"

    # Attacks
    CLIMB_ATTACK = "climb_attack"
    COUNTER_ATTACK = "counter_attack"
    SOLO_BID = "solo_bid"

    # Sprint finale
    LEADOUT_FORMING = "leadout_forming"
    LEADOUT_ESTABLISHED = "leadout_established"
    SPRINT_LAUNCHED = "sprint_launched"

    # Other
    GRUPETTO_FORMED = "grupetto_formed"
    CRASH_DISRUPTION = "crash_disruption"


PARTICIPANT_AGNOSTIC: frozenset[TacticalPattern] = frozenset(
    {
        TacticalPattern.ECHELON_RISK,
        TacticalPattern.ECHELONS_FORMING,
        TacticalPattern.SPLIT_CONFIRMED,
        TacticalPattern.CLASSICS_POSITIONING,
        TacticalPattern.COBBLE_SECTOR_APPROACH,
        TacticalPattern.PELOTON_CONTROL,
        TacticalPattern.GRUPETTO_FORMED,
    }
)
"""Patterns that describe the race rather than particular riders.

These dedup on pattern alone. "Echelons are forming" is one situation however
many different riders get named while it develops — keying it on participants
would emit a fresh event for every post.
"""


class EventStatus(StrEnum):
    """Lifecycle of a tactical read.

    SUSPECTED -> CONFIRMED -> RESOLVED, or SUSPECTED -> FAILED. A move that does
    not come off is information too, so failures are kept and shown, not dropped.
    """

    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    RESOLVED = "resolved"
    FAILED = "failed"


_event_counter = itertools.count(1)


class EventUpdate(BaseModel):
    """One step in an event's history."""

    at: datetime
    status: EventStatus
    note: str = ""
    evidence_post_ids: list[str] = Field(default_factory=list)


class TacticalEvent(BaseModel):
    """A recognised tactical move, tracked over its whole lifetime."""

    id: str = Field(default_factory=lambda: f"e{next(_event_counter)}")
    pattern: TacticalPattern
    status: EventStatus = EventStatus.SUSPECTED
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    headline: str
    """One line: what is happening. 'Alpecin and Lidl-Trek massing on the front.'"""

    explanation: str = ""
    """Why it matters tactically — the part a viewer could not read off the screen."""

    participants: list[str] = Field(default_factory=list)
    teams: list[str] = Field(default_factory=list)

    evidence_post_ids: list[str] = Field(default_factory=list)
    """Posts supporting this read. An analysis you cannot trace back is not worth showing."""

    km_to_go: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    history: list[EventUpdate] = Field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in (EventStatus.SUSPECTED, EventStatus.CONFIRMED)

    def key(self) -> tuple[str, frozenset[str]]:
        """Identity for dedup: the pattern plus who is involved.

        Same pattern, same participants means the same ongoing move — update it
        rather than emitting it again.
        """
        if self.pattern in PARTICIPANT_AGNOSTIC:
            return (self.pattern.value, frozenset())
        who = frozenset(p.lower() for p in self.participants) or frozenset(
            t.lower() for t in self.teams
        )
        return (self.pattern.value, who)


class AnalysisResult(BaseModel):
    """What an analyser returns for one tick."""

    delta: StateDelta = Field(default_factory=StateDelta)
    events: list[TacticalEvent] = Field(default_factory=list)
