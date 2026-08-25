"""Typed access to the tactical pattern knowledge base."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from race_bot.knowledge import load
from race_bot.models.race_state import RacePhase
from race_bot.models.tactics import TacticalPattern


@dataclass(frozen=True)
class PatternKnowledge:
    """Everything the agent is told about one tactical pattern."""

    pattern: TacticalPattern
    definition: str
    means: str
    cues: tuple[str, ...]
    confirms: tuple[str, ...]
    contradicts: tuple[str, ...]
    phases: frozenset[RacePhase] | None
    """Phases where this is plausible. None means any phase."""

    def plausible_in(self, phase: RacePhase) -> bool:
        # An unknown phase constrains nothing — better to consider every pattern
        # than to suppress the one that would have told us what phase we are in.
        if self.phases is None or phase is RacePhase.UNKNOWN:
            return True
        return phase in self.phases

    def render(self) -> str:
        lines = [
            f"### {self.pattern.value}",
            f"{self.definition.strip()}",
            f"Why it matters: {self.means.strip()}",
            "Commentary cues: " + "; ".join(self.cues),
        ]
        if self.confirms:
            lines.append("Raises confidence: " + "; ".join(self.confirms))
        if self.contradicts:
            lines.append("Lowers confidence: " + "; ".join(self.contradicts))
        return "\n".join(lines)


def _parse_phases(raw: object, pattern: TacticalPattern) -> frozenset[RacePhase] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"patterns.yaml: {pattern.value}: 'phases' must be a list")

    phases = set()
    for name in raw:
        try:
            phases.add(RacePhase(name))
        except ValueError as exc:
            raise ValueError(
                f"patterns.yaml: {pattern.value}: unknown phase {name!r}"
            ) from exc
    return frozenset(phases)


def _tuple_field(entry: dict, key: str, pattern: TacticalPattern) -> tuple[str, ...]:
    raw = entry.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"patterns.yaml: {pattern.value}: '{key}' must be a list")
    return tuple(str(item).strip() for item in raw)


def build_knowledge(raw: dict) -> dict[TacticalPattern, PatternKnowledge]:
    """Validate and convert a raw knowledge mapping.

    Validation is strict: a pattern in the enum with no entry would silently
    become undetectable, which is exactly the kind of bug that hides until a race
    is running.
    """
    known = {p.value for p in TacticalPattern}
    missing = known - set(raw)
    if missing:
        raise ValueError(
            "patterns.yaml is missing entries for: " + ", ".join(sorted(missing))
        )
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            "patterns.yaml has entries with no matching TacticalPattern: "
            + ", ".join(sorted(unknown))
        )

    knowledge: dict[TacticalPattern, PatternKnowledge] = {}
    for pattern in TacticalPattern:
        entry = raw[pattern.value]
        if not isinstance(entry, dict):
            raise ValueError(f"patterns.yaml: {pattern.value}: expected a mapping")
        for required in ("definition", "means", "cues"):
            if not entry.get(required):
                raise ValueError(
                    f"patterns.yaml: {pattern.value}: '{required}' is required"
                )

        knowledge[pattern] = PatternKnowledge(
            pattern=pattern,
            definition=str(entry["definition"]),
            means=str(entry["means"]),
            cues=_tuple_field(entry, "cues", pattern),
            confirms=_tuple_field(entry, "confirms", pattern),
            contradicts=_tuple_field(entry, "contradicts", pattern),
            phases=_parse_phases(entry.get("phases"), pattern),
        )

    return knowledge


@cache
def load_patterns() -> dict[TacticalPattern, PatternKnowledge]:
    """The knowledge base as shipped."""
    return build_knowledge(load("patterns"))


def patterns_for_phase(phase: RacePhase) -> list[PatternKnowledge]:
    """The patterns worth considering in this phase.

    Filtering by phase does double duty: it keeps the prompt small, and it stops
    the agent proposing a lead-out with 150km still to race.
    """
    return [k for k in load_patterns().values() if k.plausible_in(phase)]


def render_patterns(phase: RacePhase) -> str:
    return "\n\n".join(k.render() for k in patterns_for_phase(phase))
