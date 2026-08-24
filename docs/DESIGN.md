# Race Bot — Design

Tactical analysis of professional cycling races, driven by live text commentary.

## Problem

The obvious build is: send each new commentary post to an LLM, ask "what tactic is this?"

That fails on this domain. Tactical moves are not sentences — they are **state transitions
that unfold across many posts**. A breakaway forms over twenty minutes and thirty posts.
An echelon threat exists well before anyone writes the word "echelon". Classifying posts in
isolation produces either silence or a stream of duplicate, low-confidence guesses.

So the engine is built around a **running race state**. The model updates that state
incrementally, and tactical patterns are recognised as transitions in it.

## Two commitments

**1. The model proposes, code disposes.** The LLM never rewrites race state wholesale. It
emits a `StateDelta` — a small set of proposed changes — and deterministic merge logic
applies it. This is what stops the model hallucinating away a group it simply forgot to
mention in one response.

**2. Tactical knowledge is data, not prompt strings.** Each pattern lives in
`race_bot/knowledge/patterns.yaml` with its definition, the phrases commentators actually
use for it, confirming and contradicting signals, and the race phases it is plausible in.
This is the part that gets tuned most, and it should be tunable without touching Python.

## Pipeline

```
LiveTextSource.poll()          # new commentary posts, deduped and ordered
        |
   normalise()                 # regex extraction + noise filter, no LLM
        |
   ContextWindow               # rolling window of recent salient posts
        |
   Situation agent  ---------> StateDelta ---> merge ---> RaceState
        |                                                    |
   (only on meaningful change)                                |
        |                                                     |
   Tactics agent    ---------> TacticalEvent <----------------+
        |
   EventTracker                # lifecycle + dedup
        |
   ConsoleRenderer             # live state header, commentary, callouts
```

### 1. Ingestion

`LiveTextSource` is a protocol: `async def poll() -> list[CommentaryPost]`. Everything
downstream is written against it, so replay fixtures, a local file tail, and a future HTTP
live-blog adapter are interchangeable.

`ReplaySource` reads a fixture transcript and re-emits it on the original timing, scaled by
a speed factor — a five-hour race replays in five minutes. This makes the whole system
deterministically testable.

Posts are deduped by content hash and ordered by timestamp. A cursor is checkpointed so a
restart does not reprocess the race.

### 2. Deterministic pre-filter

Before a single token is spent, `normalise()` pulls out what regex can reliably get:
km-to-go, time gaps (`1'45"`, `at 45 seconds`), rider-name candidates, group sizes. It also
classifies sponsor filler and rider trivia as noise.

This cuts cost, and more importantly hands the model structured hints instead of raw prose.

### 3. Race state

`RaceState` holds the race phase, km-to-go, and a list of `Group`s — peloton, breakaway,
chase, grupetto, solo — each with size, gap to leader, and trend. Merge logic matches
proposed groups against existing ones by rider overlap and role, so a group tracks
continuously through the race rather than being recreated each tick.

### 4. Two agents

| Agent | Runs | Output | Model |
|---|---|---|---|
| Situation | every tick | `StateDelta` | cheap |
| Tactics | only on meaningful state change | `TacticalEvent` | stronger |

Splitting them keeps the per-tick cost low while letting the expensive reasoning happen
where it earns its keep. Both are Pydantic AI `Agent`s with typed `output_type` and
`deps_type=RaceState`, reaching Azure AI Foundry via `OpenAIChatModel` + `AzureProvider`.

Every `TacticalEvent` carries **evidence**: the IDs of the posts that support it. An
analysis you cannot trace back to the commentary is not worth showing.

### 5. Event lifecycle

This is what separates useful from spammy. Events are tracked through:

```
SUSPECTED -> CONFIRMED -> RESOLVED
          \-> FAILED
```

keyed by pattern plus participant set. An open `BREAKAWAY_ATTEMPT` is *promoted* to
`BREAKAWAY_ESTABLISHED` rather than re-emitted forty times. Failed predictions resolve as
`FAILED` and stay visible — a chase that collapses is itself tactical information.

## Build order

| Phase | Delivers | Status |
|---|---|---|
| 1 | Models, config, replay source, CLI, console renderer — end-to-end with a stub analyser | done |
| 2 | Normalisation + state merge — pure, heavily tested, zero LLM | done |
| 3 | Azure + Pydantic AI wiring, situation agent | |
| 4 | Tactics agent, pattern KB, event lifecycle | |
| 5 | Annotated fixtures + eval harness | |
| 6 | Real live-blog HTTP adapter | |

Phases 1–2 are fully testable without a model. That is deliberate: most of the bugs live
there, and they should be caught by fast deterministic tests rather than by staring at
model output.

## Known limits of the keyword baseline

Running the baseline over the reference fixture shows exactly what a model is
needed for. These are recorded rather than patched, because each one is a target
for phases 3-4 and a case for the phase 5 eval set:

- **No sense of when.** A closing post reflecting on "the crosswind section that
  decided it" re-triggers an echelon warning inside the final kilometre. The
  baseline cannot tell a live report from a retrospective one.
- **Phrasing, not situation.** It fires on words. Commentary that describes a
  lead-out without using the phrase is missed entirely; a rider named Bergstrom
  nearly triggered a cobbled-sector callout.
- **No group bookkeeping.** Groups are created but never dissolved, so the final
  state still shows a breakaway that was caught with 7km to go.
- **Flat confidence.** Every read is capped at 0.55 because a keyword match
  cannot justify more. Calibrated confidence needs an analyser that can weigh
  contradicting signals.

## Open questions

**Evaluation needs real transcripts.** The metric that matters is not "did it detect the
breakaway" but *detection lag* — how many posts after the first genuine cue — and
false-positive rate. Synthetic fixtures are enough to build against; honest evaluation
needs archived live blogs.

**Live-blog ingestion is a per-site question.** robots.txt and terms of service vary by
publisher. The `LiveTextSource` boundary means this decision blocks nothing before phase 6.
