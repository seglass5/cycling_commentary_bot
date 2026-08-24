"""A keyword baseline. No model, no network.

Two purposes. It proves the pipeline end to end before any Azure credentials
exist, and it is the control the model agents have to beat in phase 5 — a
tactical read that a regex can produce is not evidence that the model is adding
anything.

It is deliberately shallow: it fires on phrasing, not on situation, so its
confidence never goes above `MAX_CONFIDENCE`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from race_bot.models.commentary import NormalisedPost
from race_bot.models.race_state import (
    GapTrend,
    GroupDelta,
    GroupKind,
    RacePhase,
    RaceState,
    StateDelta,
)
from race_bot.models.tactics import (
    AnalysisResult,
    EventStatus,
    TacticalEvent,
    TacticalPattern,
)
from race_bot.pipeline.window import ContextWindow

MAX_CONFIDENCE = 0.55
"""A keyword match is suggestive, never conclusive. The ceiling says so."""


@dataclass(frozen=True)
class Rule:
    pattern: TacticalPattern
    trigger: re.Pattern[str]
    headline: str
    explanation: str
    confidence: float = 0.4
    status: EventStatus = EventStatus.SUSPECTED
    max_km_to_go: float | None = None
    """Only fire inside this distance from the finish, when set."""


RULES: tuple[Rule, ...] = (
    Rule(
        pattern=TacticalPattern.BREAKAWAY_ATTEMPT,
        trigger=re.compile(
            r"\b(gone clear|goes clear|go clear|off the front|attacked|attacks|"
            r"jumped away|slipped away|has a gap)\b",
            re.I,
        ),
        headline="Riders trying to get clear",
        explanation="An attack is up the road. Whether it sticks depends on who is in it "
        "and whether the sprinters' teams are willing to let it go.",
    ),
    Rule(
        pattern=TacticalPattern.BREAKAWAY_CAUGHT,
        trigger=re.compile(
            r"\b(caught|reeled in|brought back|swallowed up|all back together|"
            r"race is back together|absorbed)\b",
            re.I,
        ),
        headline="Move brought back",
        explanation="The chase has closed it down. Expect immediate counter-attacks "
        "while the escapees are still recovering.",
        confidence=0.45,
    ),
    Rule(
        pattern=TacticalPattern.ECHELON_RISK,
        trigger=re.compile(r"\b(crosswind|cross-wind|exposed|gutter|side wind)\b", re.I),
        headline="Crosswind exposure",
        explanation="Open roads and a side wind. Teams that miss the front split here "
        "can lose the race in a matter of minutes.",
        confidence=0.45,
    ),
    Rule(
        pattern=TacticalPattern.ECHELONS_FORMING,
        trigger=re.compile(r"\b(echelon|echelons|fanned out|fanning)\b", re.I),
        headline="Echelons forming",
        explanation="The bunch is fanning across the road. Positions behind the first "
        "echelon are effectively out of the race unless the wind changes.",
        confidence=0.5,
    ),
    Rule(
        pattern=TacticalPattern.SPLIT_CONFIRMED,
        trigger=re.compile(
            r"\b(split|splits|has broken in two|gap has opened in the bunch)\b", re.I
        ),
        headline="Bunch has split",
        explanation="A genuine selection. What matters now is which favourites and how "
        "many teammates made the front group.",
        confidence=0.5,
    ),
    Rule(
        pattern=TacticalPattern.CLASSICS_POSITIONING,
        trigger=re.compile(
            r"\b(fighting for position|battle for position|positioning|"
            r"moving up|swarming|elbows)\b",
            re.I,
        ),
        headline="Fight for position",
        explanation="Teams are massing before a decisive point. Being ten places back "
        "at the entry usually means chasing for the next five kilometres.",
    ),
    Rule(
        pattern=TacticalPattern.COBBLE_SECTOR_APPROACH,
        trigger=re.compile(r"\b(cobbl\w*|pav[eé]|kasseien|sector \d+)\b", re.I),
        headline="Approaching a cobbled sector",
        explanation="Entry position decides everything on the pavé. Expect the pace to "
        "rise well before the sector itself.",
        confidence=0.45,
    ),
    Rule(
        pattern=TacticalPattern.CLIMB_ATTACK,
        trigger=re.compile(
            r"\b(attacks on the climb|kicks? clear|out of the saddle|dances? away)\b", re.I
        ),
        headline="Attack on the climb",
        explanation="A selection is being made on gradient. Watch who can follow and who "
        "is already looking at their stem.",
    ),
    Rule(
        pattern=TacticalPattern.LEADOUT_FORMING,
        trigger=re.compile(r"\b(lead-?out|leadout|train|lining up|massing on the front)\b", re.I),
        headline="Lead-out trains assembling",
        explanation="Sprint teams are organising. The last 3km become a fight for the "
        "right wheel rather than for the front.",
        confidence=0.45,
        max_km_to_go=15.0,
    ),
    Rule(
        pattern=TacticalPattern.SPRINT_LAUNCHED,
        trigger=re.compile(r"\b(sprint is on|launches?|opens? up the sprint|kicks?)\b", re.I),
        headline="Sprint launched",
        explanation="The sprint has opened. Going too early into a headwind is the "
        "classic way to lose from the front.",
        confidence=0.5,
        status=EventStatus.CONFIRMED,
        max_km_to_go=2.0,
    ),
    Rule(
        pattern=TacticalPattern.CRASH_DISRUPTION,
        trigger=re.compile(
            r"\b(crash|touch of wheels|down in the bunch|hit the deck|pile-?up)\b", re.I
        ),
        headline="Crash in the bunch",
        explanation="Beyond the riders involved, this splits the race and forces teams to "
        "burn domestiques pacing leaders back on.",
        confidence=0.5,
        status=EventStatus.CONFIRMED,
    ),
)

_BREAK_CUE = re.compile(
    r"\b(gone clear|goes clear|off the front|breakaway|escape|leaders|out front|up the road)\b",
    re.I,
)


def infer_phase(km_to_go: float | None, current: RacePhase) -> RacePhase | None:
    """Phase from distance alone. Crude, but never wrong about the finale."""
    if km_to_go is None:
        return None
    if km_to_go <= 1.5:
        return RacePhase.SPRINT
    if km_to_go <= 30:
        return RacePhase.FINALE
    if km_to_go <= 150:
        return RacePhase.CONTROLLED_CHASE if current in (
            RacePhase.BREAKAWAY_GONE,
            RacePhase.CONTROLLED_CHASE,
        ) else RacePhase.EARLY_ATTACKS
    return RacePhase.EARLY_ATTACKS


class HeuristicAnalyser:
    """Analyser implementation driven entirely by extracted facts and keywords."""

    name = "heuristic"

    async def analyse(
        self,
        *,
        window: ContextWindow,
        state: RaceState,
        new_posts: list[NormalisedPost],
    ) -> AnalysisResult:
        delta = self._build_delta(state, new_posts)
        events = self._detect_events(state, new_posts)
        return AnalysisResult(delta=delta, events=events)

    def _build_delta(self, state: RaceState, posts: list[NormalisedPost]) -> StateDelta:
        km_to_go = next(
            (p.facts.km_to_go for p in reversed(posts) if p.facts.km_to_go is not None), None
        )
        effective_km = km_to_go if km_to_go is not None else state.km_to_go
        delta = StateDelta(
            km_to_go=km_to_go,
            phase=infer_phase(effective_km, state.phase),
            reasoning="derived from extracted facts (heuristic baseline)",
        )

        gap = next(
            (p.facts.gaps_seconds[0] for p in reversed(posts) if p.facts.gaps_seconds), None
        )
        size = next(
            (p.facts.group_sizes[0] for p in reversed(posts) if p.facts.group_sizes), None
        )
        mentions_break = any(_BREAK_CUE.search(p.text) for p in posts)

        if mentions_break and (size is not None or gap is not None):
            # A plausible small group is a breakaway; a large one is the bunch.
            if size is not None and size <= 20:
                delta.groups.append(
                    GroupDelta(kind=GroupKind.BREAKAWAY, size=size, gap_to_leader_s=0.0)
                )
            if gap is not None:
                delta.groups.append(
                    GroupDelta(
                        kind=GroupKind.PELOTON,
                        gap_to_leader_s=gap,
                        trend=self._trend(state, gap),
                    )
                )

        teams = list(
            dict.fromkeys(t for p in posts for t in p.facts.team_mentions)
        )
        if teams and delta.groups:
            for group_delta in delta.groups:
                if group_delta.kind is GroupKind.PELOTON:
                    group_delta.teams_present = teams

        return delta

    def _trend(self, state: RaceState, gap: float) -> GapTrend:
        peloton = state.peloton
        if peloton is None or peloton.gap_to_leader_s is None:
            return GapTrend.UNKNOWN
        previous = peloton.gap_to_leader_s
        if abs(gap - previous) < 5:
            return GapTrend.STABLE
        return GapTrend.SHRINKING if gap < previous else GapTrend.GROWING

    def _detect_events(
        self, state: RaceState, posts: list[NormalisedPost]
    ) -> list[TacticalEvent]:
        events: list[TacticalEvent] = []
        # Distance is carried forward through the batch rather than read from
        # state, which is only merged after analysis returns. Without this, a
        # rule gated on km-to-go behaves differently depending on how many posts
        # happened to arrive in one tick.
        current_km = state.km_to_go

        for post in posts:
            if post.facts.km_to_go is not None:
                current_km = post.facts.km_to_go
            km_to_go = current_km

            for rule in RULES:
                if rule.max_km_to_go is not None and (
                    km_to_go is None or km_to_go > rule.max_km_to_go
                ):
                    continue
                if not rule.trigger.search(post.text):
                    continue
                events.append(
                    TacticalEvent(
                        pattern=rule.pattern,
                        status=rule.status,
                        confidence=min(rule.confidence, MAX_CONFIDENCE),
                        headline=rule.headline,
                        explanation=rule.explanation,
                        participants=list(post.facts.rider_mentions),
                        teams=list(post.facts.team_mentions),
                        evidence_post_ids=[post.id],
                        km_to_go=km_to_go,
                    )
                )
        return events
