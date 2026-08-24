"""Deterministic pre-filter: what regex can get before any token is spent.

Two jobs. First, drop filler so the model never pays for sponsor plugs. Second,
lift the numbers — km to go, time gaps, group sizes — out of prose and into
structured fields, because asking a model to re-read a number it was already
given is both slower and less reliable than reading it here.
"""

from __future__ import annotations

import re

from race_bot.knowledge import load
from race_bot.models.commentary import (
    CommentaryPost,
    ExtractedFacts,
    NormalisedPost,
    PostKind,
)

_LEX = load("lexicon")

_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
}
_COLLECTIVE_NOUNS: dict[str, int] = {
    "duo": 2, "pair": 2, "trio": 3, "quartet": 4, "quintet": 5, "sextet": 6,
    "septet": 7, "octet": 8,
}

_KM_UNIT = r"(?:km|kilometres?|kilometers?)"
_KM_PATTERNS = [
    re.compile(
        rf"(\d+(?:\.\d+)?)\s*{_KM_UNIT}\s*(?:to go|remaining|left|to race|"
        rf"from (?:the )?(?:finish|line))",
        re.I,
    ),
    re.compile(rf"(?:with|inside|under|just)\s+(\d+(?:\.\d+)?)\s*{_KM_UNIT}\b", re.I),
    re.compile(rf"\bat\s+(\d+(?:\.\d+)?)\s*{_KM_UNIT}\s*to\s*go\b", re.I),
]
_FLAMME_ROUGE = re.compile(r"flamme rouge|final kilomet(?:re|er)|1\s*km to go", re.I)

# `1'45"` and `1-45` are unambiguously gaps. Bare numbers need a nearby cue word,
# or every jersey number and rider age becomes a time gap.
_GAP_EXPLICIT = re.compile(r"\b(\d{1,2})\s*'\s*(\d{1,2})\s*\"?")
_GAP_CUE = re.compile(
    r"\b(?:gap|lead|leads|leading|advantage|behind|ahead|deficit|trail(?:s|ing)?|"
    r"down to|out to|up to|at|by)\b",
    re.I,
)
_GAP_MINUTES = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:minutes?|mins?)\b", re.I)
_GAP_SECONDS = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", re.I)
_GAP_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_GAP_CONTEXT_CHARS = 45

_NUM_OR_WORD = r"(\d+|" + "|".join(_NUMBER_WORDS) + r")"
_SIZE_PATTERNS = [
    re.compile(rf"\bgroup of {_NUM_OR_WORD}\b", re.I),
    re.compile(rf"\b{_NUM_OR_WORD}[\s-](?:man|rider|strong)\b", re.I),
    re.compile(
        rf"\b{_NUM_OR_WORD}\s+(?:riders?|leaders?|escapees?|attackers?|men|"
        rf"chasers?|survivors?)\b",
        re.I,
    ),
    re.compile(rf"\bleading (?:group of )?{_NUM_OR_WORD}\b", re.I),
    re.compile(rf"\bbunch of {_NUM_OR_WORD}\b", re.I),
]
_COLLECTIVE_RE = re.compile(r"\b(" + "|".join(_COLLECTIVE_NOUNS) + r")\b", re.I)

_PARTICLES = r"(?:van der|van den|van 't|van|de|den|der|del|della|di|du|da|dos|le|la|el|ten|ter)"
_NAME_RE = re.compile(
    rf"\b[A-Z][\w'’-]+(?:\s+{_PARTICLES})*(?:\s+[A-Z][\w'’-]+)+"
)

_NOISE_MARKERS = tuple(m.lower() for m in _LEX["noise_markers"])
_RESULT_MARKERS = tuple(m.lower() for m in _LEX["result_markers"])
_TACTICAL_CUES = tuple(c.lower() for c in _LEX["tactical_cues"])
_HIGH_VALUE_CUES = tuple(c.lower() for c in _LEX["high_value_cues"])
_NON_NAMES = {n.lower() for n in _LEX["non_names"]}
_NON_NAME_LEADING = set(_LEX["non_name_leading_tokens"])


def _build_team_index() -> dict[str, str]:
    """Map every alias (lowercased) to its canonical team name."""
    index: dict[str, str] = {}
    for canonical, aliases in _LEX["teams"].items():
        index[canonical.lower()] = canonical
        for alias in aliases or []:
            index[alias.lower()] = canonical
    return index


_TEAM_INDEX = _build_team_index()
# Longest alias first so "Red Bull-BORA-hansgrohe" wins over "Red Bull".
_TEAM_RE = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(a) for a in sorted(_TEAM_INDEX, key=len, reverse=True))
    + r")(?!\w)",
    re.I,
)


def _to_int(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def extract_km_to_go(text: str) -> float | None:
    for pattern in _KM_PATTERNS:
        match = pattern.search(text)
        if match:
            value = float(match.group(1))
            # Grand tour stages top out around 250km; anything larger is a
            # distance total or a stray number, not a to-go figure.
            if 0 <= value <= 300:
                return value
    if _FLAMME_ROUGE.search(text):
        return 1.0
    return None


def extract_gaps(text: str) -> list[float]:
    """Time gaps in seconds, in the order they appear."""
    gaps: list[float] = []

    for match in _GAP_EXPLICIT.finditer(text):
        minutes, seconds = int(match.group(1)), int(match.group(2))
        if seconds < 60:
            gaps.append(minutes * 60 + seconds)

    cue_spans = [m.end() for m in _GAP_CUE.finditer(text)]

    def near_cue(start: int) -> bool:
        return any(0 <= start - end <= _GAP_CONTEXT_CHARS for end in cue_spans)

    for match in _GAP_CLOCK.finditer(text):
        if not near_cue(match.start()):
            continue
        minutes, seconds = int(match.group(1)), int(match.group(2))
        if seconds < 60:
            value = float(minutes * 60 + seconds)
            if value not in gaps:
                gaps.append(value)

    for match in _GAP_MINUTES.finditer(text):
        if near_cue(match.start()):
            value = float(match.group(1)) * 60
            if value not in gaps:
                gaps.append(value)

    for match in _GAP_SECONDS.finditer(text):
        if near_cue(match.start()):
            value = float(match.group(1))
            if value not in gaps:
                gaps.append(value)

    return gaps


def extract_group_sizes(text: str) -> list[int]:
    sizes: list[int] = []
    for pattern in _SIZE_PATTERNS:
        for match in pattern.finditer(text):
            value = _to_int(match.group(1))
            if value is not None and 1 <= value <= 200 and value not in sizes:
                sizes.append(value)
    for match in _COLLECTIVE_RE.finditer(text):
        value = _COLLECTIVE_NOUNS[match.group(1).lower()]
        if value not in sizes:
            sizes.append(value)
    return sizes


def extract_teams(text: str) -> list[str]:
    found: list[str] = []
    for match in _TEAM_RE.finditer(text):
        canonical = _TEAM_INDEX[match.group(1).lower()]
        if canonical not in found:
            found.append(canonical)
    return found


def extract_rider_names(text: str, *, known_teams: list[str] | None = None) -> list[str]:
    """Candidate rider names.

    A heuristic, and honest about it: capitalised multi-token spans, minus team
    names, race names and ordinary sentence-initial capitalisation. It will miss
    single-name references and occasionally admit a place name. Downstream
    treats these as hints, never as ground truth.
    """
    team_words = {w.lower() for t in (known_teams or []) for w in re.split(r"[\s-]+", t)}
    names: list[str] = []

    for match in _NAME_RE.finditer(text):
        candidate = match.group(0).strip()
        tokens = candidate.split()

        if tokens[0] in _NON_NAME_LEADING:
            # Retry without the leading word: "Then Pogacar attacks" -> "Pogacar
            # attacks" is not a name, but "But Mathieu van der Poel" is.
            tokens = tokens[1:]
            if len(tokens) < 2:
                continue
            candidate = " ".join(tokens)

        if candidate.lower() in _NON_NAMES:
            continue
        if candidate.lower() in _TEAM_INDEX:
            continue
        if any(t.lower() in team_words for t in tokens):
            continue
        if len(tokens) > 4:
            continue
        if candidate not in names:
            names.append(candidate)

    return names


def classify(text: str) -> PostKind:
    lowered = text.lower()
    if any(marker in lowered for marker in _RESULT_MARKERS):
        return PostKind.RESULT
    if any(marker in lowered for marker in _NOISE_MARKERS):
        return PostKind.NOISE
    if len(lowered.strip()) < 15:
        return PostKind.NOISE
    return PostKind.RACE


def score_salience(kind: PostKind, facts: ExtractedFacts, text: str) -> float:
    """0-1 estimate of how much this post matters tactically.

    Two tiers of cue. Ordinary ones nudge the score; high-value ones move it
    sharply, because a post saying "echelons are forming" matters more than a
    routine gap update even though the gap update carries more numbers.
    """
    if kind is PostKind.NOISE:
        return 0.0

    lowered = text.lower()
    cue_hits = sum(1 for cue in _TACTICAL_CUES if cue in lowered)
    high_hits = sum(1 for cue in _HIGH_VALUE_CUES if cue in lowered)

    score = 0.1
    score += min(cue_hits, 4) * 0.08
    score += min(high_hits, 3) * 0.15

    if facts.gaps_seconds:
        score += 0.15
    if facts.km_to_go is not None:
        score += 0.1
    if facts.group_sizes:
        score += 0.1
    if facts.rider_mentions:
        score += 0.08
    if facts.team_mentions:
        score += 0.05
    if kind is PostKind.RESULT:
        score *= 0.5

    return round(min(score, 1.0), 3)


def normalise(post: CommentaryPost) -> NormalisedPost:
    """Turn a raw post into a classified, fact-extracted one."""
    text = post.text
    kind = classify(text)

    if kind is PostKind.NOISE:
        # Nothing downstream reads facts off noise, so skip the work.
        facts = ExtractedFacts()
    else:
        teams = extract_teams(text)
        facts = ExtractedFacts(
            km_to_go=extract_km_to_go(text),
            gaps_seconds=extract_gaps(text),
            group_sizes=extract_group_sizes(text),
            rider_mentions=extract_rider_names(text, known_teams=teams),
            team_mentions=teams,
        )

    return NormalisedPost(
        post=post,
        kind=kind,
        facts=facts,
        salience=score_salience(kind, facts, text),
    )
