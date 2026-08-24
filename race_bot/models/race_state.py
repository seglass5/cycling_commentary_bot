"""The running picture of the race, and the deltas that move it forward."""

from __future__ import annotations

import itertools
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RacePhase(StrEnum):
    """Coarse stage of the race. Constrains which tactics are plausible."""

    UNKNOWN = "unknown"
    PRE_RACE = "pre_race"
    NEUTRALISED = "neutralised"
    EARLY_ATTACKS = "early_attacks"
    """The fight to get in the move, before anything sticks."""

    BREAKAWAY_GONE = "breakaway_gone"
    CONTROLLED_CHASE = "controlled_chase"
    """A team is riding tempo with a gap it intends to close."""

    FINALE = "finale"
    """Roughly the last 30km: positioning, attacks, the race decided."""

    SPRINT = "sprint"
    POST_RACE = "post_race"


class GroupKind(StrEnum):
    PELOTON = "peloton"
    BREAKAWAY = "breakaway"
    CHASE = "chase"
    SOLO = "solo"
    GRUPETTO = "grupetto"
    DROPPED = "dropped"


class GapTrend(StrEnum):
    UNKNOWN = "unknown"
    GROWING = "growing"
    STABLE = "stable"
    SHRINKING = "shrinking"


_group_counter = itertools.count(1)


def _next_group_id() -> str:
    return f"g{next(_group_counter)}"


class Group(BaseModel):
    """A body of riders on the road, tracked continuously through the race."""

    id: str = Field(default_factory=_next_group_id)
    kind: GroupKind
    riders: list[str] = Field(default_factory=list)
    teams_present: list[str] = Field(default_factory=list)
    size: int | None = None
    gap_to_leader_s: float | None = None
    """Seconds behind the race leader on the road. 0 for the lead group."""

    trend: GapTrend = GapTrend.UNKNOWN
    first_seen: datetime | None = None
    last_updated: datetime | None = None

    @property
    def label(self) -> str:
        if self.riders and len(self.riders) <= 3:
            return ", ".join(self.riders)
        n = self.size if self.size is not None else len(self.riders) or None
        return f"{self.kind.value}" + (f" ({n})" if n else "")


class RaceState(BaseModel):
    """Everything we currently believe about the race."""

    race_name: str = "unknown race"
    stage: str | None = None
    phase: RacePhase = RacePhase.UNKNOWN
    km_to_go: float | None = None
    groups: list[Group] = Field(default_factory=list)
    weather_notes: list[str] = Field(default_factory=list)
    posts_seen: int = 0
    updated_at: datetime | None = None

    def group_by_id(self, group_id: str) -> Group | None:
        return next((g for g in self.groups if g.id == group_id), None)

    def groups_of_kind(self, kind: GroupKind) -> list[Group]:
        return [g for g in self.groups if g.kind is kind]

    @property
    def peloton(self) -> Group | None:
        return next(iter(self.groups_of_kind(GroupKind.PELOTON)), None)

    @property
    def leaders(self) -> Group | None:
        """The group at the front of the race, by gap."""
        on_road = [g for g in self.groups if g.kind is not GroupKind.DROPPED]
        if not on_road:
            return None
        return min(
            on_road,
            key=lambda g: g.gap_to_leader_s if g.gap_to_leader_s is not None else 0.0,
        )


class GroupDelta(BaseModel):
    """A proposed change to one group.

    The model emits these; `pipeline.state.apply_delta` decides how they land.
    """

    group_ref: str | None = None
    """Existing group id, when the model is updating a group it was shown."""

    kind: GroupKind
    riders_added: list[str] = Field(default_factory=list)
    riders_removed: list[str] = Field(default_factory=list)
    teams_present: list[str] = Field(default_factory=list)
    size: int | None = None
    gap_to_leader_s: float | None = None
    trend: GapTrend | None = None
    dissolved: bool = False
    """The group no longer exists — caught, or merged back into the peloton."""


class StateDelta(BaseModel):
    """The situation agent's proposed changes for one tick.

    Deliberately small and all-optional: unset fields mean "no change", never
    "unknown, wipe it". That asymmetry is what stops a forgetful response from
    destroying accumulated state.
    """

    phase: RacePhase | None = None
    km_to_go: float | None = None
    groups: list[GroupDelta] = Field(default_factory=list)
    weather_notes: list[str] = Field(default_factory=list)
    reasoning: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.phase or self.km_to_go is not None or self.groups or self.weather_notes
        )
