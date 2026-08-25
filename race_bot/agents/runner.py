"""Shared resilience for agent calls.

A race is a five-hour process against a remote API, so both agents treat failure
as routine: timeouts and errors never propagate, and repeated failures stop
throwing calls at a dead endpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")

DEFAULT_TIMEOUT_SECONDS = 25.0
MAX_BACKOFF_TICKS = 32
"""Ceiling on how long a sustained outage suppresses calls."""

MAX_RECORDED_ERRORS = 20


@dataclass
class AgentStats:
    """Running totals, so cost and reliability are visible during a race."""

    calls: int = 0
    failures: int = 0
    skipped: int = 0
    """Ticks where the agent was not called because it was backing off."""

    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Non-fatal observations — rejected events, off-phase reads."""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record_error(self, message: str) -> None:
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append(message)

    def record_note(self, message: str) -> None:
        if len(self.notes) < MAX_RECORDED_ERRORS:
            self.notes.append(message)

    def summary(self) -> str:
        parts = [f"{self.calls} calls", f"{self.total_tokens} tokens"]
        if self.failures:
            parts.append(f"{self.failures} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped while backing off")
        return ", ".join(parts)


class CallGuard:
    """Wraps an agent call with a timeout, failure isolation and backoff."""

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self.stats = AgentStats()
        self._consecutive_failures = 0
        self._ticks_to_skip = 0

    @property
    def backing_off(self) -> bool:
        return self._ticks_to_skip > 0

    async def run(self, make_call: Callable[[], Awaitable[T]]) -> T | None:
        """Run the call. Returns None if it was skipped, timed out or failed."""
        if self._ticks_to_skip > 0:
            self._ticks_to_skip -= 1
            self.stats.skipped += 1
            return None

        self.stats.calls += 1
        try:
            result = await asyncio.wait_for(make_call(), timeout=self.timeout_seconds)
        except TimeoutError:
            self.record_failure(f"timed out after {self.timeout_seconds:g}s")
            return None
        except Exception as exc:  # noqa: BLE001 - a live race must survive any API failure
            self.record_failure(f"{type(exc).__name__}: {exc}")
            return None

        self._consecutive_failures = 0
        return result

    def record_failure(self, message: str) -> None:
        self.stats.failures += 1
        self._consecutive_failures += 1
        # First failure is free — transient errors are common and the next tick
        # carries the same commentary anyway. Sustained failure backs off.
        self._ticks_to_skip = min(
            2 ** (self._consecutive_failures - 1) - 1, MAX_BACKOFF_TICKS
        )
        self.stats.record_error(message)

    def record_usage(self, usage: object) -> None:
        self.stats.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.stats.output_tokens += getattr(usage, "output_tokens", 0) or 0


_WRAPPED_ATTRS = ("situation", "tactics", "state_from", "events_from")


def collect_stats(analyser: object) -> dict[str, AgentStats]:
    """Gather stats from an analyser and any analysers it wraps.

    Composite and two-stage analysers hold agents inside them, so the run summary
    has to look past the top-level object to report what a race actually cost.
    """
    found: dict[str, AgentStats] = {}

    stats = getattr(analyser, "stats", None)
    if isinstance(stats, AgentStats):
        name = str(getattr(analyser, "name", type(analyser).__name__))
        found[name] = stats

    for attr in _WRAPPED_ATTRS:
        child = getattr(analyser, attr, None)
        if child is not None and not callable(child):
            found.update(collect_stats(child))

    return found
