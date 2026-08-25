"""Instructions and context rendering for the agents."""

from __future__ import annotations

from collections.abc import Sequence

from race_bot.models.race_state import GroupKind, RacePhase, RaceState
from race_bot.models.tactics import TacticalEvent
from race_bot.pipeline.state import format_gap

SITUATION_INSTRUCTIONS = """\
You track the state of a professional cycling race from live text commentary.

You are given the race state as currently understood, and the most recent
commentary posts. Return a StateDelta describing ONLY what has changed.

## The single most important rule

Omit anything you are not confident changed. An unset field means "no change" —
it does NOT mean "unknown". If you leave `groups` empty, the groups already
tracked stay exactly as they are. This is safe and usually correct.

Never clear or restate something just because the recent posts did not mention
it. A breakaway that nobody has written about for ten minutes is still up the
road.

## Live reports versus references to earlier moments

Commentary constantly refers back. "The crosswind section that decided the race"
in the closing kilometres is a retrospective summary, not a report of crosswinds
happening now. So is "he attacked at 40km to go" said with 10km remaining.

Only report what is happening NOW. Distance in the race never goes backwards.

## Groups

- `gap_to_leader_s` is seconds behind the leaders on the road. The front group
  is 0. Every other group is a positive number.
- To update a group already listed in the state, set `group_ref` to its exact id
  from that listing. If you are unsure which group is meant, leave `group_ref`
  unset and give the riders instead — they will be matched by overlap.
- Set `dissolved: true` when a group ceases to exist: caught, or absorbed back
  into the bunch. Do not simply stop mentioning it.
- Group kinds: {group_kinds}

## Race phases

{phases}

## Honesty

Only use rider names, team names and numbers that actually appear in the
commentary. Do not infer a gap that was not stated, and do not fill in a startlist
from your own knowledge of the sport. Extracted facts are given in brackets after
each post — prefer those numbers over re-reading them from the prose.

Put a one-line justification in `reasoning`.
"""

TACTICS_INSTRUCTIONS = """\
You are an expert cycling race analyst. You are given the current race state, the
recent commentary, the tactical moves already being tracked, and a reference list
of tactical patterns.

Report tactical moves that are happening now. For each one, say what it is and
why it matters — the part a viewer could not read off the screen themselves.

## Evidence is mandatory

Every event must cite the ids of the commentary posts that support it, in
`evidence_post_ids`. Post ids appear in parentheses at the start of each post.
An event citing no real post will be discarded, so cite accurately rather than
plausibly.

Only name riders and teams that appear in the commentary. Do not supply a
startlist, a rider's reputation, or a result from your own knowledge of the sport.

## Do not repeat yourself

Moves already being tracked are listed. For those:
- Say nothing if there is no real change. Silence is the correct output most ticks.
- Re-report with a higher `status` when a move is confirmed by new evidence:
  `suspected` -> `confirmed`.
- Report the pattern that follows it when the situation moves on — a
  `breakaway_attempt` that sticks becomes `breakaway_established`; a chase that
  closes becomes `breakaway_caught`. Do not restate the earlier pattern.

An empty `events` list is a perfectly good answer, and the most common one.

## Live, not retrospective

Commentary refers back constantly. "The crosswind section that decided the race",
said in the closing kilometres, is a summary of something that already happened —
not a report of crosswinds now. Report only what is happening at this moment.

## Confidence

- 0.8-1.0: the commentary states it directly
- 0.5-0.8: strongly implied by several posts
- 0.3-0.5: plausible reading of ambiguous phrasing
- below 0.3: do not report it

Check the "lowers confidence" signals before committing to a read. If the
commentary contradicts a pattern, do not report it.

## Patterns to consider

Only these patterns are plausible in the current phase of this race. Prefer them.

{patterns}
"""


def _format_enum_help() -> dict[str, str]:
    kinds = ", ".join(k.value for k in GroupKind)
    phases = "\n".join(f"- `{p.value}`: {_PHASE_HELP[p]}" for p in RacePhase)
    return {"group_kinds": kinds, "phases": phases}


_PHASE_HELP: dict[RacePhase, str] = {
    RacePhase.UNKNOWN: "not yet established",
    RacePhase.PRE_RACE: "before the flag drops",
    RacePhase.NEUTRALISED: "rolling neutral section, or racing stopped",
    RacePhase.EARLY_ATTACKS: "the fight to get in the move, before anything sticks",
    RacePhase.BREAKAWAY_GONE: "a break is clear and the bunch has accepted it",
    RacePhase.CONTROLLED_CHASE: "a team is riding tempo with a gap it intends to close",
    RacePhase.FINALE: "roughly the last 30km — positioning, attacks, the race decided",
    RacePhase.SPRINT: "the sprint itself, inside the final kilometre",
    RacePhase.POST_RACE: "after the finish",
}


def situation_instructions() -> str:
    return SITUATION_INSTRUCTIONS.format(**_format_enum_help())


def tactics_instructions(phase: RacePhase) -> str:
    """Instructions with the pattern reference filtered to the current phase."""
    from race_bot.knowledge.patterns import render_patterns

    return TACTICS_INSTRUCTIONS.format(patterns=render_patterns(phase))


def render_open_events(events: Sequence[TacticalEvent]) -> str:
    """Moves already being tracked, so the agent advances them instead of repeating."""
    if not events:
        return "Nothing is currently being tracked."

    lines = []
    for event in events:
        who = ", ".join(event.participants or event.teams) or "unnamed"
        lines.append(
            f"- {event.pattern.value} [{event.status.value}, "
            f"confidence {event.confidence:.2f}]: {event.headline} ({who})"
        )
    return "\n".join(lines)


def render_state(state: RaceState) -> str:
    """The current race picture, as compactly as it can be stated."""
    lines = [
        f"race: {state.race_name}" + (f" — {state.stage}" if state.stage else ""),
        f"phase: {state.phase.value}",
        f"km_to_go: {state.km_to_go:g}" if state.km_to_go is not None else "km_to_go: unknown",
    ]

    if state.groups:
        lines.append("groups:")
        for group in state.groups:
            riders = ", ".join(group.riders) if group.riders else "unnamed"
            size = f", size {group.size}" if group.size is not None else ""
            lines.append(
                f"  [{group.id}] {group.kind.value}: {riders}{size}, "
                f"{format_gap(group.gap_to_leader_s)} behind leaders, trend {group.trend.value}"
            )
    else:
        lines.append("groups: none identified yet")

    if state.weather_notes:
        lines.append("conditions: " + "; ".join(state.weather_notes))

    return "\n".join(lines)
