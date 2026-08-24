from __future__ import annotations

import pytest

from race_bot.pipeline.window import ContextWindow


def test_noise_is_rejected(make_normalised):
    window = ContextWindow()
    assert window.add(make_normalised("Subscribe to our newsletter")) is False
    assert len(window) == 0


def test_race_posts_accepted(make_normalised):
    window = ContextWindow()
    assert window.add(make_normalised("The gap is down to 45 seconds with 20km to go")) is True
    assert len(window) == 1


def test_min_salience_filter(make_normalised):
    window = ContextWindow(min_salience=0.5)
    assert window.add(make_normalised("Riders are taking on food from the team cars")) is False


def test_trim_respects_max_posts(make_normalised):
    window = ContextWindow(max_posts=5, keep_recent=2)
    for i in range(20):
        window.add(
            make_normalised(f"The riders continue along the road at point {i}", minute=i)
        )
    assert len(window) == 5


def test_trim_keeps_most_recent_unconditionally(make_normalised):
    window = ContextWindow(max_posts=3, keep_recent=2)
    window.add(make_normalised("Echelons are forming across the road", minute=0, post_id="high"))
    for i in range(1, 6):
        window.add(
            make_normalised(f"The race continues quietly at point {i}", minute=i, post_id=f"low{i}")
        )

    ids = [p.id for p in window.posts]
    assert ids[-2:] == ["low4", "low5"]


def test_trim_keeps_high_salience_history(make_normalised):
    """The run-up to a move must survive as filler ages out."""
    window = ContextWindow(max_posts=3, keep_recent=2)
    window.add(make_normalised("Echelons are forming across the road", minute=0, post_id="high"))
    for i in range(1, 6):
        window.add(
            make_normalised(f"The race continues quietly at point {i}", minute=i, post_id=f"low{i}")
        )

    assert "high" in [p.id for p in window.posts]


def test_window_stays_chronological(make_normalised):
    window = ContextWindow(max_posts=3, keep_recent=1)
    window.add(make_normalised("Echelons are forming across the road", minute=0, post_id="a"))
    window.add(make_normalised("The bunch has split in the crosswind", minute=1, post_id="b"))
    for i in range(2, 6):
        window.add(
            make_normalised(f"Quiet moment number {i} in the race", minute=i, post_id=f"c{i}")
        )

    timestamps = [p.timestamp for p in window.posts]
    assert timestamps == sorted(timestamps)


def test_render_inlines_extracted_facts(make_normalised):
    window = ContextWindow()
    window.add(make_normalised("With 42km to go the gap is 1'45\" for the five leaders"))
    rendered = window.render()
    assert "km_to_go=42" in rendered
    assert "gaps_s=105" in rendered
    assert "sizes=5" in rendered


def test_keep_recent_cannot_exceed_max_posts():
    with pytest.raises(ValueError):
        ContextWindow(max_posts=3, keep_recent=5)
