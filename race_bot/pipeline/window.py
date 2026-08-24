"""The rolling slice of commentary the model actually sees."""

from __future__ import annotations

from race_bot.models.commentary import NormalisedPost


class ContextWindow:
    """Recent commentary, budgeted.

    Noise never enters. When the window is full, the most recent posts are kept
    unconditionally — the model needs to know what just happened — and the rest
    of the budget goes to the highest-salience older posts, so the run-up to a
    move survives even as filler ages out.
    """

    def __init__(
        self,
        *,
        max_posts: int = 25,
        keep_recent: int = 8,
        min_salience: float = 0.0,
    ) -> None:
        if keep_recent > max_posts:
            raise ValueError("keep_recent cannot exceed max_posts")
        self.max_posts = max_posts
        self.keep_recent = keep_recent
        self.min_salience = min_salience
        self._posts: list[NormalisedPost] = []

    def __len__(self) -> int:
        return len(self._posts)

    @property
    def posts(self) -> list[NormalisedPost]:
        """Window contents, oldest first."""
        return list(self._posts)

    @property
    def latest(self) -> NormalisedPost | None:
        return self._posts[-1] if self._posts else None

    def add(self, post: NormalisedPost) -> bool:
        """Add a post. Returns False if it was filtered out."""
        if post.is_noise or post.salience < self.min_salience:
            return False
        self._posts.append(post)
        self._trim()
        return True

    def _trim(self) -> None:
        if len(self._posts) <= self.max_posts:
            return

        recent = self._posts[-self.keep_recent :] if self.keep_recent else []
        older = self._posts[: len(self._posts) - len(recent)]
        budget = self.max_posts - len(recent)

        kept_older = sorted(older, key=lambda p: p.salience, reverse=True)[:budget]
        kept_ids = {p.id for p in kept_older}
        self._posts = [p for p in older if p.id in kept_ids] + recent

    def render(self) -> str:
        """The window as a prompt block, with extracted facts inlined.

        Facts are repeated next to each post on purpose: the model gets the
        numbers pre-parsed rather than having to find them in prose again.
        """
        lines: list[str] = []
        for post in self._posts:
            stamp = post.timestamp.strftime("%H:%M")
            facts = _render_facts(post)
            suffix = f"  [{facts}]" if facts else ""
            lines.append(f"({post.id}) {stamp} {post.text}{suffix}")
        return "\n".join(lines)


def _render_facts(post: NormalisedPost) -> str:
    parts: list[str] = []
    facts = post.facts
    if facts.km_to_go is not None:
        parts.append(f"km_to_go={facts.km_to_go:g}")
    if facts.gaps_seconds:
        parts.append("gaps_s=" + ",".join(f"{g:g}" for g in facts.gaps_seconds))
    if facts.group_sizes:
        parts.append("sizes=" + ",".join(str(s) for s in facts.group_sizes))
    if facts.team_mentions:
        parts.append("teams=" + "; ".join(facts.team_mentions))
    if facts.rider_mentions:
        parts.append("riders=" + "; ".join(facts.rider_mentions))
    return " ".join(parts)
