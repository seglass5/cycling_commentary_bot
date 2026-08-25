# race-bot

Tactical analysis of professional cycling races, driven by live text commentary.

It follows a commentary feed, maintains a running picture of the race — phase,
km to go, groups and gaps — and calls out tactical moves as it recognises them:
a breakaway forming, echelons in a crosswind, lead-out trains assembling for a
sprint, the fight for position before a cobbled sector.

## Status

Phases 1-4 and 6 of [the design](docs/DESIGN.md) are complete. The pipeline runs
end to end on live sources or replayed transcripts, the situation agent tracks
race state through Azure AI Foundry, and the tactics agent recognises moves
against a knowledge base of tactical patterns and explains why they matter.

What remains is evaluation (phase 5). Prompt quality has not yet been measured
against real commentary — that is the largest open question in the project.

The whole test suite runs without a network or an API key.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Follow the reference race, replayed at 60x
race-bot follow fixtures/races/kustklassieker_2026.jsonl --speed 60
```

Commentary scrolls past, tactical callouts appear as panels, and the race state
stays pinned at the bottom of the terminal:

```
╭──────────────────────────────────────────────────────────╮
│ Kustklassieker 2026   phase: finale   24km to go         │
│                                                          │
│ breakaway  4 riders    0s                                │
│ peloton    —          55s  v                             │
╰──────────────────────────────────────────────────────────╯
```

Useful options: `--speed 0` replays instantly, `--noise` shows what the
pre-filter rejected, `--no-commentary` shows only the tactical analysis.

### Using Azure AI Foundry

Copy `.env.example` to `.env` and fill in your endpoint, key and the two
deployment names. Then verify before trusting a race to it:

```bash
race-bot check     # one trivial request per deployment
race-bot follow fixtures/races/kustklassieker_2026.jsonl --analyser azure
```

Three analyser modes:

| `--analyser` | Race state | Tactical callouts |
|---|---|---|
| `heuristic` (default) | keyword rules | keyword rules |
| `azure-state` | situation agent | keyword rules |
| `azure` | situation agent | tactics agent |

The end-of-race summary reports calls, tokens, failures and the tactics gate's
trigger rate per agent. If Azure is unreachable the run does not fail: state
tracking degrades to empty deltas, which change nothing, and `azure-state` keeps
producing callouts from the keyword rules.

### Following a live race

```bash
# An RSS or Atom feed — no site configuration needed
race-bot follow --url https://example.com/live/feed.xml

# A live blog page, using a site config for its selectors
race-bot follow --url https://example.com/live/race --site-config mysite.yaml

# Keep what you see; the transcript replays as a fixture afterwards
race-bot follow --url https://... --site-config mysite.yaml --record-to races/today.jsonl
```

**Which sites to poll is your decision.** No site configuration ships enabled —
see [`race_bot/sources/sites/README.md`](race_bot/sources/sites/README.md) for
what to check first. The bot identifies itself, enforces robots.txt with no
override, honours `Crawl-delay`, and uses conditional requests so an unchanged
page costs a 304 rather than a full fetch.

### Building a corpus

```bash
race-bot record --url https://... --site-config mysite.yaml --out races/today.jsonl
race-bot inspect races/today.jsonl        # check the selectors worked
race-bot follow races/today.jsonl         # replay it
```

`record` writes the same format `follow` replays, so a recorded race becomes a
fixture with no conversion step. This is how you get the real transcripts that
honest evaluation needs.

### Tuning the pre-filter

```bash
race-bot inspect fixtures/races/kustklassieker_2026.jsonl
```

Prints every post with its classification, salience score and extracted facts.
No model involved. This is the tool for editing
[`race_bot/knowledge/lexicon.yaml`](race_bot/knowledge/lexicon.yaml) — look for
race posts scored as noise, or numbers that were not picked up.

## How it works

```
LiveTextSource.poll()   ->  normalise()  ->  ContextWindow
                                                  |
                                            Analyser
                                          /            \
                                   StateDelta        TacticalEvent
                                        |                  |
                                   merge into         EventTracker
                                    RaceState        (lifecycle/dedup)
                                          \            /
                                          ConsoleRenderer
```

Three ideas do most of the work:

**Race state, not text classification.** Tactical moves unfold over many posts,
so the bot maintains a running picture of the race and recognises moves as
transitions in it, rather than classifying posts in isolation.

**The model proposes, code disposes.** An analyser never rewrites state
directly. It emits a `StateDelta` that deterministic merge logic applies, with
rules that stop a forgetful response from destroying accumulated state.

**Tactical knowledge is data.** `knowledge/patterns.yaml` holds all 19 patterns
with their definitions, the phrasing commentators use, confirming and
contradicting signals, and the phases each is plausible in. It is filtered by race
phase before going into the prompt, which keeps the prompt small and stops the
agent proposing a lead-out with 150km still to race. Edit the YAML when the bot
misreads a move — not the Python.

**Every callout must cite its evidence.** The tactics agent returns the ids of the
commentary posts supporting each read. Ids that do not exist are dropped, and an
event with no surviving evidence is discarded before it reaches the screen.

**Events have a lifecycle.** `SUSPECTED → CONFIRMED → RESOLVED | FAILED`, keyed
by pattern and participants. A breakaway forming produces the same read on
twenty consecutive ticks; the first opens an event and the rest quietly update
it. Moves that fail are kept and shown — a chase that collapses is tactical
information too.

See [docs/DESIGN.md](docs/DESIGN.md) for the reasoning in full.

## Layout

```
race_bot/
  models/       CommentaryPost, RaceState, TacticalEvent, StateDelta
  sources/      LiveTextSource protocol; replay, live blog, feed, recording
  sources/sites/  site configs — none shipped; see the README there
  pipeline/     normalise, window, state merge, event tracker, orchestrator
  analysis/     Analyser protocol; keyword baseline; composite
  agents/       Azure provider, prompts, situation and tactics agents
  knowledge/    lexicon.yaml, patterns.yaml — domain knowledge as data
  render/       terminal output
fixtures/races/ transcripts for development and testing
```

## Development

```bash
pytest          # 270 tests, no network required
ruff check .
```

The keyword analyser in `analysis/heuristic.py` is not a placeholder to be
deleted. It stays as the control the model agents have to beat: a tactical read
a regex can produce is not evidence the model is adding anything.
