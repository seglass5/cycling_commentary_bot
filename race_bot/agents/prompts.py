"""Instructions and context rendering for the agents."""

from __future__ import annotations

from race_bot.models.race_state import GroupKind, RacePhase, RaceState
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

TACTICS_INSTRUCTIONS_PLACEHOLDER = """\
Reserved for the tactics agent in phase 4.
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
