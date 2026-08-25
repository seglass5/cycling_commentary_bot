"""Command line entry points."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from race_bot.analysis.base import Analyser
from race_bot.analysis.composite import CompositeAnalyser
from race_bot.analysis.heuristic import HeuristicAnalyser
from race_bot.config import Settings, load_settings
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


class AnalyserChoice(StrEnum):
    HEURISTIC = "heuristic"
    """Keyword baseline. No model, no credentials."""

    AZURE_STATE = "azure-state"
    """Situation agent for race state, keyword rules for callouts. Cheaper."""

    AZURE = "azure"
    """Both agents. Tactical callouts come from the model."""


def _build_source(
    console: Console,
    *,
    transcript: Path | None,
    url: str,
    site: str,
    speed: float,
    site_config: Path | None = None,
) -> Any:
    """A replayed transcript, a live blog, or a feed."""
    if bool(transcript) == bool(url):
        console.print("[red]Give either a transcript path or --url, not both.[/red]")
        raise typer.Exit(code=2)

    if transcript is not None:
        try:
            source = ReplaySource(transcript, speed=speed)
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if source.post_count == 0:
            console.print(f"[yellow]{transcript} contains no posts.[/yellow]")
            raise typer.Exit(code=1)
        return source

    if site or site_config:
        from race_bot.sources.liveblog import (
            LiveBlogSource,
            SiteConfigError,
            load_site_config,
            load_site_config_file,
        )

        try:
            config = (
                load_site_config_file(site_config)
                if site_config
                else load_site_config(site)
            )
        except SiteConfigError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        return LiveBlogSource(url, config)

    from race_bot.sources.feed import FeedSource

    return FeedSource(url)


def _build_analyser(choice: AnalyserChoice, settings: Settings, console: Console) -> Analyser:
    if choice is AnalyserChoice.HEURISTIC:
        return HeuristicAnalyser()

    from race_bot.agents.provider import AzureModels, AzureNotConfigured

    try:
        models = AzureModels(settings)
    except AzureNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    from race_bot.agents.situation import SituationAgent

    situation = SituationAgent(models.situation)

    if choice is AnalyserChoice.AZURE_STATE:
        return CompositeAnalyser(state_from=situation, events_from=HeuristicAnalyser())

    from race_bot.agents.tactics import TacticsAgent
    from race_bot.analysis.two_stage import TwoStageAnalyser

    return TwoStageAnalyser(
        situation=situation,
        tactics=TacticsAgent(models.tactics),
        salience_threshold=settings.tactics_salience_threshold,
        gap_shift_seconds=settings.tactics_gap_shift_seconds,
    )


@app.command()
def follow(
    transcript: Annotated[
        Path | None,
        typer.Argument(help="JSONL transcript to replay. Omit when using --url."),
    ] = None,
    url: Annotated[
        str, typer.Option(help="Live blog or feed URL to follow instead of a transcript.")
    ] = "",
    site: Annotated[
        str, typer.Option(help="Site config name for --url. Omit for an RSS/Atom feed.")
    ] = "",
    site_config: Annotated[
        Path | None, typer.Option(help="Site config file path, instead of --site.")
    ] = None,
    record_to: Annotated[
        Path | None, typer.Option(help="Also write everything seen to this JSONL file.")
    ] = None,
    speed: Annotated[
        float, typer.Option(help="Replay speed multiplier. 0 replays instantly.")
    ] = 60.0,
    race_name: Annotated[str, typer.Option(help="Name shown in the status header.")] = "",
    stage: Annotated[str, typer.Option(help="Stage label for the status header.")] = "",
    commentary: Annotated[bool, typer.Option(help="Show the commentary stream.")] = True,
    noise: Annotated[bool, typer.Option(help="Show posts filtered out as noise.")] = False,
    changes: Annotated[bool, typer.Option(help="Show race-state changes.")] = True,
    analyser: Annotated[
        AnalyserChoice, typer.Option(help="Which analyser drives race state.")
    ] = AnalyserChoice.HEURISTIC,
) -> None:
    """Follow a race, calling out tactical moves as they are recognised."""
    settings = load_settings()
    console = Console()

    source = _build_source(
        console,
        transcript=transcript,
        url=url,
        site=site,
        site_config=site_config,
        speed=speed,
    )
    if record_to is not None:
        from race_bot.sources.recording import RecordingSource

        source = RecordingSource(source, record_to)
        console.print(f"[dim]recording to {record_to}[/dim]")

    default_name = transcript.stem.replace("_", " ") if transcript else source.name
    state = RaceState(race_name=race_name or default_name, stage=stage or None)
    live = transcript is None
    tick_seconds = (
        getattr(getattr(source, "config", None), "poll_seconds", 30.0)
        if live
        else settings.tick_seconds
    )

    orchestrator = Orchestrator(
        source=source,
        analyser=_build_analyser(analyser, settings, console),
        state=state,
        window=ContextWindow(
            max_posts=settings.window_max_posts,
            keep_recent=settings.window_keep_recent,
        ),
        tick_seconds=tick_seconds,
        salience_threshold=settings.salience_threshold,
    )
    from race_bot.analysis.two_stage import TwoStageAnalyser

    if isinstance(orchestrator.analyser, TwoStageAnalyser):
        # The tactics agent needs to know what is already being tracked, so it
        # advances existing moves instead of re-reporting them.
        orchestrator.analyser.open_events = lambda: orchestrator.tracker.open_events

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


@app.command()
def record(
    url: Annotated[str, typer.Option(help="Live blog or feed URL to record.")],
    out: Annotated[Path, typer.Option(help="JSONL transcript to write.")],
    site: Annotated[
        str, typer.Option(help="Site config name. Omit for an RSS/Atom feed.")
    ] = "",
    site_config: Annotated[
        Path | None, typer.Option(help="Site config file path, instead of --site.")
    ] = None,
    polls: Annotated[
        int, typer.Option(help="Stop after this many polls. 0 records until interrupted.")
    ] = 0,
    interval: Annotated[float, typer.Option(help="Seconds between polls.")] = 30.0,
) -> None:
    """Record a live source to a transcript, without analysing it.

    Use this to build an evaluation corpus from real races, and to work out a
    site's selectors — record a few polls, then `race-bot inspect` the result.
    """
    console = Console()

    from race_bot.sources.recording import RecordingSource

    inner = _build_source(
        console, transcript=None, url=url, site=site, site_config=site_config, speed=0
    )
    source = RecordingSource(inner, out)

    async def run() -> None:
        poll_count = 0
        try:
            while polls == 0 or poll_count < polls:
                posts = await source.poll()
                poll_count += 1
                console.print(
                    f"[dim]poll {poll_count}: {len(posts)} new "
                    f"({source.recorded} total)[/dim]"
                )
                for post in posts:
                    console.print(f"  {post.timestamp.strftime('%H:%M')}  {post.text[:90]}")
                if polls and poll_count >= polls:
                    break
                await asyncio.sleep(interval)
        finally:
            await source.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")

    errors = getattr(inner, "poll_errors", [])
    if errors:
        console.print(f"\n[yellow]{len(errors)} poll errors:[/yellow]")
        for error in errors[:5]:
            console.print(f"[yellow]  {error}[/yellow]")

    console.print(f"\n[green]{source.recorded} posts written to {out}[/green]")


@app.command()
def sites() -> None:
    """List the site configurations available to --site."""
    from race_bot.sources.liveblog import SITES_DIR, available_sites

    console = Console()
    configured = available_sites()
    if not configured:
        console.print("[yellow]No sites configured.[/yellow]")
        console.print(f"[dim]Add one in {SITES_DIR} — see the README there first.[/dim]")
        return
    for name in configured:
        console.print(f"  {name}")


@app.command()
def check() -> None:
    """Verify Azure AI Foundry credentials and both deployments.

    Sends one trivial request per deployment. Run this after filling in .env,
    before trusting a live race to it.
    """
    console = Console()
    settings = load_settings()

    from race_bot.agents.provider import AzureModels, AzureNotConfigured

    console.print(f"endpoint:   {settings.azure_endpoint or '[red]not set[/red]'}")
    console.print(f"api version: {settings.azure_api_version}")
    console.print(
        f"key:        {'set' if settings.azure_api_key else '[red]not set[/red]'}\n"
    )

    try:
        models = AzureModels(settings)
    except AzureNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    from pydantic_ai import Agent

    failures = 0
    for label, deployment, model in (
        ("situation", settings.situation_model, models.situation),
        ("tactics", settings.tactics_model, models.tactics),
    ):
        try:
            result = asyncio.run(Agent(model).run("Reply with the single word: ok"))
        except Exception as exc:  # noqa: BLE001 - report any failure, do not raise
            failures += 1
            console.print(f"[red]x[/red] {label} ({deployment}): {type(exc).__name__}: {exc}")
            continue

        usage = result.usage
        console.print(
            f"[green]ok[/green] {label} ({deployment}) — "
            f"{usage.input_tokens or 0} in / {usage.output_tokens or 0} out"
        )

    if failures:
        raise typer.Exit(code=1)

    console.print("\n[green]Azure AI Foundry is reachable and both deployments respond.[/green]")


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
    _print_agent_stats(console, orchestrator.analyser)


def _print_agent_stats(console: Console, analyser: object) -> None:
    """Report model usage for every agent involved in the run."""
    from race_bot.agents.runner import collect_stats

    by_agent = collect_stats(analyser)
    if not by_agent:
        return

    console.print()
    for name, stats in by_agent.items():
        console.print(f"[dim]{name}: {stats.summary()}[/dim]")
        for error in stats.errors[:3]:
            console.print(f"[yellow]  {error}[/yellow]")

    gate = getattr(analyser, "tactics_skipped", None)
    if gate is not None:
        called = getattr(analyser, "tactics_calls", 0)
        console.print(
            f"[dim]tactics gate: {called} triggered, {gate} ticks skipped[/dim]"
        )


if __name__ == "__main__":
    app()
