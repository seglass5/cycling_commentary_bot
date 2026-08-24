"""Command line entry points."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from race_bot.analysis.heuristic import HeuristicAnalyser
from race_bot.config import load_settings
from race_bot.models.race_state import RaceState
from race_bot.pipeline.normalise import normalise
from race_bot.pipeline.orchestrator import Orchestrator
from race_bot.pipeline.window import ContextWindow
from race_bot.render.console import ConsoleRenderer
from race_bot.sources.replay import ReplaySource

app = typer.Typer(
    add_completion=False,
    help="Tactical analysis of cycling races from live text commentary.",
)


@app.command()
def follow(
    transcript: Annotated[Path, typer.Argument(help="JSONL transcript to replay.")],
    speed: Annotated[
        float, typer.Option(help="Replay speed multiplier. 0 replays instantly.")
    ] = 60.0,
    race_name: Annotated[str, typer.Option(help="Name shown in the status header.")] = "",
    stage: Annotated[str, typer.Option(help="Stage label for the status header.")] = "",
    commentary: Annotated[bool, typer.Option(help="Show the commentary stream.")] = True,
    noise: Annotated[bool, typer.Option(help="Show posts filtered out as noise.")] = False,
    changes: Annotated[bool, typer.Option(help="Show race-state changes.")] = True,
) -> None:
    """Follow a race, calling out tactical moves as they are recognised."""
    settings = load_settings()
    console = Console()

    try:
        source = ReplaySource(transcript, speed=speed)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if source.post_count == 0:
        console.print(f"[yellow]{transcript} contains no posts.[/yellow]")
        raise typer.Exit(code=1)

    state = RaceState(
        race_name=race_name or transcript.stem.replace("_", " "),
        stage=stage or None,
    )
    orchestrator = Orchestrator(
        source=source,
        analyser=HeuristicAnalyser(),
        state=state,
        window=ContextWindow(
            max_posts=settings.window_max_posts,
            keep_recent=settings.window_keep_recent,
        ),
        tick_seconds=settings.tick_seconds,
        salience_threshold=settings.salience_threshold,
    )
    renderer = ConsoleRenderer(
        console=console,
        show_commentary=commentary,
        show_noise=noise,
        show_changes=changes,
    )

    async def run() -> RaceState:
        with renderer.live(state):
            return await orchestrator.run(on_tick=renderer.handle)

    try:
        final = asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")
        raise typer.Exit(code=130) from None

    _print_summary(console, orchestrator, final)


@app.command()
def inspect(
    transcript: Annotated[Path, typer.Argument(help="JSONL transcript to analyse.")],
    show_noise: Annotated[bool, typer.Option(help="Include posts classified as noise.")] = True,
    limit: Annotated[int, typer.Option(help="Maximum rows to print. 0 for all.")] = 0,
) -> None:
    """Show what the deterministic pre-filter makes of a transcript.

    No model involved. This is the tool for tuning `knowledge/lexicon.yaml`:
    look for race posts scored as noise, or numbers that were not extracted.
    """
    console = Console()

    try:
        source = ReplaySource(transcript, speed=0)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    posts = asyncio.run(source.poll())

    table = Table(show_lines=False, header_style="bold")
    table.add_column("time", style="dim", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("sal", justify="right", no_wrap=True)
    table.add_column("facts", style="cyan", max_width=44)
    table.add_column("text", max_width=70)

    shown = 0
    kinds: dict[str, int] = {}
    for post in posts:
        normalised = normalise(post)
        kinds[normalised.kind.value] = kinds.get(normalised.kind.value, 0) + 1
        if normalised.is_noise and not show_noise:
            continue
        if limit and shown >= limit:
            continue

        facts = normalised.facts
        bits = []
        if facts.km_to_go is not None:
            bits.append(f"{facts.km_to_go:g}km")
        if facts.gaps_seconds:
            bits.append("gap " + ",".join(f"{g:g}s" for g in facts.gaps_seconds))
        if facts.group_sizes:
            bits.append("n=" + ",".join(str(s) for s in facts.group_sizes))
        if facts.team_mentions:
            bits.append("/".join(facts.team_mentions))
        if facts.rider_mentions:
            bits.append("/".join(facts.rider_mentions))

        style = "dim" if normalised.is_noise else ""
        table.add_row(
            post.timestamp.strftime("%H:%M"),
            normalised.kind.value,
            f"{normalised.salience:.2f}",
            " ".join(bits),
            post.text,
            style=style,
        )
        shown += 1

    console.print(table)
    console.print(
        f"[dim]{len(posts)} posts — "
        + ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items()))
        + "[/dim]"
    )


def _print_summary(console: Console, orchestrator: Orchestrator, state: RaceState) -> None:
    events = orchestrator.tracker.all_events
    console.print()
    console.rule("[bold]race summary")

    if not events:
        console.print("[dim]no tactical events recognised[/dim]")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("km to go", justify="right", style="yellow", no_wrap=True)
    table.add_column("pattern", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("headline")

    for event in events:
        table.add_row(
            f"{event.km_to_go:g}" if event.km_to_go is not None else "—",
            event.pattern.value.replace("_", " "),
            event.status.value,
            event.headline,
        )

    console.print(table)
    console.print(
        f"\n[dim]{state.posts_seen} posts seen, {len(events)} events tracked[/dim]"
    )


if __name__ == "__main__":
    app()
