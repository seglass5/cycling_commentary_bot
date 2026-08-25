# race-bot

Tactical analysis of professional cycling races, driven by live text commentary.

It follows a commentary feed, maintains a running picture of the race — phase,
km to go, groups and gaps — and calls out tactical moves as it recognises them:
a breakaway forming, echelons in a crosswind, lead-out trains assembling for a
sprint, the fight for position before a cobbled sector.

## Status

Phases 1-3 of [the design](docs/DESIGN.md) are complete. The pipeline runs end
to end on replayed transcripts, and the situation agent tracks race state through
Azure AI Foundry. The tactics agent — which produces the tactical callouts
themselves — arrives in phase 4; until then the keyword baseline supplies them.

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

`--analyser azure` puts the situation agent in charge of race state. If Azure is
unreachable the run degrades to the keyword baseline rather than failing, and the
end-of-race summary reports calls, tokens and any failures.

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
  sources/      LiveTextSource protocol; replay implementation
  pipeline/     normalise, window, state merge, event tracker, orchestrator
  analysis/     Analyser protocol; keyword baseline; composite
  agents/       Azure provider, prompts, situation agent
  knowledge/    lexicon.yaml — domain vocabulary as data
  render/       terminal output
fixtures/races/ transcripts for development and testing
```

## Development

```bash
pytest          # 152 tests, no network required
ruff check .
```

The keyword analyser in `analysis/heuristic.py` is not a placeholder to be
deleted. It stays as the control the model agents have to beat: a tactical read
a regex can produce is not evidence the model is adding anything.
