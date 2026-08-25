"""The tactical pattern knowledge base."""

from __future__ import annotations

import pytest

from race_bot.knowledge.patterns import (
    build_knowledge,
    load_patterns,
    patterns_for_phase,
    render_patterns,
)
from race_bot.models.race_state import RacePhase
from race_bot.models.tactics import TacticalPattern


def test_every_pattern_has_knowledge():
    """A pattern with no entry would be silently undetectable."""
    assert set(load_patterns()) == set(TacticalPattern)


def test_every_entry_has_cues_and_meaning():
    for knowledge in load_patterns().values():
        assert knowledge.definition.strip()
        assert knowledge.means.strip()
        assert knowledge.cues


def test_phases_are_valid_race_phases():
    for knowledge in load_patterns().values():
        if knowledge.phases is not None:
            assert all(isinstance(p, RacePhase) for p in knowledge.phases)


def test_unknown_phase_considers_everything():
    """Suppressing patterns before we know the phase could hide the answer."""
    assert len(patterns_for_phase(RacePhase.UNKNOWN)) == len(TacticalPattern)


def test_phase_filtering_narrows_the_set():
    early = patterns_for_phase(RacePhase.EARLY_ATTACKS)
    assert len(early) < len(TacticalPattern)


def test_leadout_is_not_offered_early_in_the_race():
    """A lead-out with 150km to race is not a plausible read."""
    early = {k.pattern for k in patterns_for_phase(RacePhase.EARLY_ATTACKS)}
    assert TacticalPattern.LEADOUT_FORMING not in early
    assert TacticalPattern.BREAKAWAY_ATTEMPT in early


def test_sprint_phase_offers_sprint_patterns():
    sprint = {k.pattern for k in patterns_for_phase(RacePhase.SPRINT)}
    assert TacticalPattern.SPRINT_LAUNCHED in sprint


def test_crash_is_plausible_in_any_phase():
    knowledge = load_patterns()[TacticalPattern.CRASH_DISRUPTION]
    assert knowledge.phases is None
    assert knowledge.plausible_in(RacePhase.SPRINT)
    assert knowledge.plausible_in(RacePhase.EARLY_ATTACKS)


def test_render_includes_cues_and_significance():
    rendered = render_patterns(RacePhase.SPRINT)
    assert "sprint_launched" in rendered
    assert "Commentary cues:" in rendered
    assert "Why it matters:" in rendered


def test_phase_filtering_shrinks_the_prompt():
    assert len(render_patterns(RacePhase.SPRINT)) < len(
        render_patterns(RacePhase.UNKNOWN)
    )


# --- validation ----------------------------------------------------------


def _valid_entry() -> dict:
    return {"definition": "d", "means": "m", "cues": ["c"]}


def _full_mapping() -> dict:
    return {p.value: _valid_entry() for p in TacticalPattern}


def test_missing_pattern_is_rejected():
    raw = _full_mapping()
    del raw[TacticalPattern.SOLO_BID.value]
    with pytest.raises(ValueError, match="missing entries for: solo_bid"):
        build_knowledge(raw)


def test_unknown_pattern_is_rejected():
    raw = _full_mapping()
    raw["invented_pattern"] = _valid_entry()
    with pytest.raises(ValueError, match="no matching TacticalPattern"):
        build_knowledge(raw)


def test_missing_required_field_is_rejected():
    raw = _full_mapping()
    del raw[TacticalPattern.SOLO_BID.value]["cues"]
    with pytest.raises(ValueError, match="'cues' is required"):
        build_knowledge(raw)


def test_unknown_phase_name_is_rejected():
    raw = _full_mapping()
    raw[TacticalPattern.SOLO_BID.value]["phases"] = ["not_a_phase"]
    with pytest.raises(ValueError, match="unknown phase"):
        build_knowledge(raw)


def test_non_list_cues_rejected():
    raw = _full_mapping()
    raw[TacticalPattern.SOLO_BID.value]["cues"] = "a string"
    with pytest.raises(ValueError, match="'cues' must be a list"):
        build_knowledge(raw)
