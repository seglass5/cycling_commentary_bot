from __future__ import annotations

import pytest

from race_bot.models.commentary import PostKind
from race_bot.pipeline.normalise import (
    classify,
    extract_gaps,
    extract_group_sizes,
    extract_km_to_go,
    extract_rider_names,
    extract_teams,
    normalise,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("With 42km to go the gap is stable", 42.0),
        ("25 km to go and the pace is rising", 25.0),
        ("just 3.5km remaining", 3.5),
        ("inside 10km now", 10.0),
        ("They pass under the flamme rouge", 1.0),
        ("The final kilometre begins", 1.0),
        ("a 198km race along the coast", None),
        ("no distance mentioned here at all", None),
    ],
)
def test_extract_km_to_go(text, expected):
    assert extract_km_to_go(text) == expected


def test_km_to_go_rejects_implausible_distance():
    # A stage total, not a to-go figure.
    assert extract_km_to_go("with 4200km covered across the race") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("the gap is 1'45\"", [105.0]),
        ("they lead by 2'30\"", [150.0]),
        ("the gap is 45 seconds", [45.0]),
        ("the advantage is out to 2 minutes", [120.0]),
        ("leading by 3:20 at the line", [200.0]),
    ],
)
def test_extract_gaps(text, expected):
    assert extract_gaps(text) == expected


def test_bare_numbers_are_not_gaps():
    """Without a cue word nearby, a number is just a number."""
    assert extract_gaps("rider number 45 has punctured") == []
    assert extract_gaps("he is 28 years old and riding his first classic") == []


def test_explicit_minute_second_form_needs_no_cue():
    assert extract_gaps("1'45\" is the story of the day") == [105.0]


def test_invalid_seconds_rejected():
    assert extract_gaps("the gap is 1'75\"") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a group of 8 has formed", [8]),
        ("a five-man move", [5]),
        ("the trio out front", [3]),
        ("the four leaders", [4]),
        ("12 riders have gone clear", [12]),
    ],
)
def test_extract_group_sizes(text, expected):
    assert extract_group_sizes(text) == expected


def test_extract_teams_canonicalises_aliases():
    assert extract_teams("Quick-Step and Jumbo-Visma on the front") == [
        "Soudal Quick-Step",
        "Visma-Lease a Bike",
    ]


def test_extract_teams_prefers_longest_alias():
    assert extract_teams("Red Bull-BORA-hansgrohe drive the pace") == [
        "Red Bull-BORA-hansgrohe"
    ]


def test_extract_rider_names_handles_particles():
    names = extract_rider_names("Mathieu van der Poel leads Wout van Aert")
    assert "Mathieu van der Poel" in names
    assert "Wout van Aert" in names


def test_extract_rider_names_rejects_race_names():
    assert extract_rider_names("This is the Tour de France after all") == []


def test_extract_rider_names_rejects_sentence_initial_capitals():
    assert extract_rider_names("Only Wright remains out front") == []
    assert extract_rider_names("The peloton is chasing hard") == []


def test_extract_rider_names_excludes_team_words():
    names = extract_rider_names(
        "Soudal Quick-Step are chasing", known_teams=["Soudal Quick-Step"]
    )
    assert names == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Subscribe to our newsletter", PostKind.NOISE),
        ("Photo: the breakaway in the dunes", PostKind.NOISE),
        ("ok", PostKind.NOISE),
        ("General classification after stage 4", PostKind.RESULT),
        ("The gap is coming down quickly now", PostKind.RACE),
    ],
)
def test_classify(text, expected):
    assert classify(text) == expected


def test_noise_posts_skip_fact_extraction(make_post):
    post = make_post("Subscribe now — the gap is 45 seconds with 20km to go")
    result = normalise(post)
    assert result.kind is PostKind.NOISE
    assert result.facts.km_to_go is None
    assert result.salience == 0.0


def test_high_value_cues_outrank_routine_posts(make_normalised):
    echelons = make_normalised("Echelons are forming across the road")
    routine = make_normalised("Riders are taking on food from the team cars")
    assert echelons.salience > routine.salience


def test_salience_is_bounded(make_normalised):
    loud = make_normalised(
        "Echelons forming, crosswind, the bunch has split, attack, crash, "
        "lead-out, with 20km to go the gap is 1'45\" for the five leaders, "
        "Mathieu van der Poel and Lidl-Trek involved"
    )
    assert 0.0 <= loud.salience <= 1.0
