"""Terminal output: a pinned race-state header over a scrolling commentary log."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.console import Group as RenderGroup
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from race_bot.models.race_state import GapTrend, GroupKind, RaceState
from race_bot.models.tactics import EventStatus, TacticalEvent, TacticalPattern
from race_bot.pipeline.orchestrator import TickResult
from race_bot.pipeline.state import format_gap

_PATTERN_STYLE: dict[TacticalPattern, str] = {
    TacticalPattern.BREAKAWAY_ATTEMPT: "cyan",
    TacticalPattern.BREAKAWAY_ESTABLISHED: "bright_cyan",
    TacticalPattern.BREAKAWAY_CAUGHT: "blue",
    TacticalPattern.PELOTON_CONTROL: "blue",
    TacticalPattern.CHASE_ORGANISED: "blue",
    TacticalPattern.CHASE_COLLAPSING: "yellow",
    TacticalPattern.ECHELON_RISK: "yellow",
    TacticalPattern.ECHELONS_FORMING: "bright_yellow",
    TacticalPattern.SPLIT_CONFIRMED: "bright_yellow",
    TacticalPattern.CLASSICS_POSITIONING: "green",
    TacticalPattern.COBBLE_SECTOR_APPROACH: "green",
    TacticalPattern.CLIMB_ATTACK: "bright_green",
    TacticalPattern.COUNTER_ATTACK: "bright_green",
    TacticalPattern.SOLO_BID: "bright_green",
    TacticalPattern.LEADOUT_FORMING: "magenta",
    TacticalPattern.LEADOUT_ESTABLISHED: "bright_magenta",
    TacticalPattern.SPRINT_LAUNCHED: "bright_magenta",
    TacticalPattern.GRUPETTO_FORMED: "dim",
    TacticalPattern.CRASH_DISRUPTION: "red",
}

_TREND_MARK = {
    GapTrend.GROWING: "^",
    GapTrend.SHRINKING: "v",
    GapTrend.STABLE: "=",
    GapTrend.UNKNOWN: " ",
}

_STATUS_MARK = {
    EventStatus.SUSPECTED: "?",
    EventStatus.CONFIRMED: "!",
    EventStatus.RESOLVED: "+",
    EventStatus.FAILED: "x",
}

_GROUP_ORDER = {
    GroupKind.SOLO: 0,
    GroupKind.BREAKAWAY: 1,
    GroupKind.CHASE: 2,
    GroupKind.PELOTON: 3,
    GroupKind.GRUPETTO: 4,
    GroupKind.DROPPED: 5,
}


class ConsoleRenderer:
    """Renders tick results as they arrive.

    Commentary scrolls; the race state stays pinned at the bottom so the current
    picture is always visible without scrolling back.
    """

    def __init__(
        self,
        *,
        console: Console | None = None,
        show_commentary: bool = True,
        show_noise: bool = False,
        show_changes: bool = True,
    ) -> None:
        self.console = console or Console()
        self.show_commentary = show_commentary
        self.show_noise = show_noise
        self.show_changes = show_changes
        self._live: Live | None = None

    @contextmanager
    def live(self, state: RaceState) -> Iterator[ConsoleRenderer]:
        with Live(
            self.status_panel(state),
            console=self.console,
            refresh_per_second=8,
            transient=False,
        ) as live:
            self._live = live
            try:
                yield self
            finally:
                self._live = None

    def _print(self, renderable: object) -> None:
        # With a Live region active, printing through its console places output
        # above the pinned panel instead of fighting with it.
        target = self._live.console if self._live else self.console
        target.print(renderable)

    def handle(self, result: TickResult, state: RaceState) -> None:
        if self.show_commentary:
            for post in result.new_posts:
                stamp = post.timestamp.strftime("%H:%M")
                line = Text(f"  {stamp}  ", style="dim")
                line.append(post.text, style="grey70")
                self._print(line)

        if self.show_noise:
            for post in result.filtered_posts:
                self._print(Text(f"  ----   {post.text}", style="dim italic"))

        if self.show_changes and result.changes:
            for change in result.changes:
                self._print(Text(f"         {change}", style="dim cyan"))

        for event in result.events:
            self._print(self.event_panel(event))

        if self._live is not None:
            self._live.update(self.status_panel(state))

    def event_panel(self, event: TacticalEvent) -> Panel:
        style = _PATTERN_STYLE.get(event.pattern, "white")
        mark = _STATUS_MARK[event.status]

        label = event.pattern.value.replace("_", " ").upper()
        title = Text(f"[{mark}] {label}", style=f"bold {style}")
        if event.km_to_go is not None:
            title.append(f"   {event.km_to_go:g}km to go", style="dim")

        body = Text(event.headline, style="bold")
        if event.explanation:
            body.append("\n" + event.explanation, style="")

        who = ", ".join(event.participants or event.teams)
        footer = Text()
        if who:
            footer.append(f"\n{who}", style="italic dim")
        footer.append(
            f"\nconfidence {_confidence_bar(event.confidence)}  "
            f"evidence: {', '.join(event.evidence_post_ids) or 'none'}",
            style="dim",
        )

        return Panel(
            RenderGroup(body, footer),
            title=title,
            title_align="left",
            border_style=style,
            padding=(0, 1),
        )

    def status_panel(self, state: RaceState) -> Panel:
        header = Text()
        header.append(state.race_name, style="bold")
        if state.stage:
            header.append(f" — {state.stage}", style="bold")
        header.append(f"   phase: {state.phase.value}", style="cyan")
        if state.km_to_go is not None:
            header.append(f"   {state.km_to_go:g}km to go", style="bold yellow")
        header.append(f"   posts: {state.posts_seen}", style="dim")

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_column(justify="right")
        table.add_column(justify="center")

        for group in sorted(state.groups, key=lambda g: _GROUP_ORDER.get(g.kind, 9)):
            riders = ", ".join(group.riders[:4])
            if len(group.riders) > 4:
                riders += f" +{len(group.riders) - 4}"
            table.add_row(
                group.kind.value,
                riders or (f"{group.size} riders" if group.size else "—"),
                format_gap(group.gap_to_leader_s),
                _TREND_MARK[group.trend],
            )

        if not state.groups:
            table.add_row("—", "no groups identified yet", "", "")

        if state.weather_notes:
            table.add_row("conditions", "; ".join(state.weather_notes[-2:]), "", "")

        return Panel(
            RenderGroup(header, Text(), table),
            border_style="grey37",
            padding=(0, 1),
        )


def _confidence_bar(confidence: float) -> str:
    filled = round(confidence * 5)
    return "#" * filled + "." * (5 - filled) + f" {confidence:.0%}"
