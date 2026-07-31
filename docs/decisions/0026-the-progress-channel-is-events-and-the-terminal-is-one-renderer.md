# 0026 — The progress channel is events, and the terminal is one renderer

**Status:** accepted.

## Context

Every progress line in `milhouse run` goes through one untyped channel, `Reporter = Callable[[str], None]` (`session.py`), wired to `report=typer.echo` in `cli.py`. Formatting happens at the call site: `session.about()` bakes in a two-space indent and an issue-id label, and `step.py`, `run.py`, `parallel.py`, and `session.py` each build their own sentence and hand it straight to `typer.echo`. There is nothing between the code that knows what happened and the terminal that draws it.

Three consequences follow from that, and they are why this is being settled before the refactor rather than during it:

1. **A poll loop is not a milestone, and it is reported like one.** `step.reap()` calls `session.report(f"{pending.issue.id} is still working")` for every in-flight turn on every poll (`step.py:276`), and `parallel.Concurrent` polls at `[run] poll_ms` (default 5s) against a `turn_timeout_ms` of up to 30 minutes. One slow turn can emit up to 360 lines that say nothing changed, times the number of turns in flight. This is most of the wall of text the epic names.
2. **Everything prints at one weight.** A lane path, a verify cwd, a reconciliation note, and a turn that just failed are four `typer.echo` calls with no way for a renderer — or a reader — to tell them apart.
3. **There is no boundary to put a second renderer behind.** Formatting is interleaved with the logic that decides something happened, so a live view or a machine-readable log would each have to be built by re-deriving that logic rather than by consuming a stream.

The fix is a seam: a typed event replaces the pre-formatted string, and the terminal becomes one implementation of a renderer over it.

## Decision

### The channel carries events, not strings

`Reporter` is replaced by `Callable[[Event], None]`, and `Event` is a small frozen dataclass:

```python
@dataclass(frozen=True)
class Event:
    kind: EventKind
    text: str  # the human sentence; what `plain` prints verbatim
    issue_id: str | None = None  # whose turn, when there is one
    lane: str | None = None  # the workspace id or branch the call site has, when there is one
    state: str | None = None  # heartbeat only: what the turn is doing right now
    elapsed_ms: int | None = None  # heartbeat only: time since dispatch
```

`text` still carries a finished sentence, because every call site already has the right words and re-deriving them from structured fields alone would either lose information (`about() `'s callers format merge outcomes, verify commands, and error text in ways specific to each) or require the event schema to anticipate every sentence shape in advance. What moves out of the call sites is everything _about_ the sentence that is the renderer's business: indentation, colour, alignment, and whether it is shown at all. `about()`'s two-space indent and issue-id label are exactly that, and they go.

### Seven event kinds, mapped from the call sites that exist today

Enough for a renderer to rebuild a lane table from the stream alone, and named concretely so the next issue is a mechanical migration rather than a second design pass:

| Kind | Carries | Replaces |
| --- | --- | --- |
| `started` | issue, lane, iteration number, title | `step.py:326` — a turn begins |
| `dispatched` | issue, lane | `step.py:248` — the agent has the prompt and its lane is open |
| `heartbeat` | issue, lane, `state`, `elapsed_ms` | `step.py:276` (`"... is still working"`) and `step.py:278` (`"reaping iteration N..."`) — a turn already in flight, observed again |
| `settled` | issue, lane, outcome + detail | `step.py:171,242,282` — a turn ended, including one that never started |
| `merged` | issue, branch(es) | `step.py:535,538,553,661,667` — a worker branch's fate (fast-forward, merge commit, conflict, refused) and the integration gate's verdict |
| `halted` | detail; issue when the halt names one | `run.py:300` — the run is stopping, and why |
| `note` | issue/lane when known, else neither | everything else: `session.py:261,339,406,462,470,509`, `step.py:438,444`, `parallel.py:253`, `run.py:350` — bookkeeping the table has no row for |

`heartbeat` is the kind that answers consequence 1. It is not a line of text — it is an observation of a turn's current `state` (`"working"`, `"verifying"`, `"reaping"`, …) and how long it has been running, emitted on every poll regardless of whether anything changed. A renderer decides what to do with a repeat; the channel does not decide for it.

`note` is the escape hatch for session-level bookkeeping that never fit `about()`'s issue-and-lane shape either — a stale lock taken over, a lane left open at teardown, an overdue turn abandoned. It carries `issue_id` and `lane` when the call site has them and `None` when it does not, same as today.

### The renderers, and how one is chosen

Two renderers ship, both consuming the same `Iterator[Event]` (or callback):

- **`live`**: a lane table, one row per issue currently in flight, redrawn in place on `heartbeat`, `started`, and `dispatched`. `settled`, `merged`, `halted`, and `note` also print a line that scrolls above the table, since those are exactly the events with a finished sentence worth keeping once the row they described is gone. This is the epic's "milestones scrolling above it."
- **`plain`**: one line per event, in order, via `typer.echo` — today's output, unchanged in spirit. The one rule that isn't "print `text`": a `heartbeat` prints only when `state` differs from the last `heartbeat` shown for that `issue_id`. That turns the 360 identical polls in the epic's example into one line per state transition (`dispatched` → `working` → `verifying` → `reaping`), without suppressing heartbeats a renderer without a table still needs.

Selection: `--progress {auto,live,plain}` (default `auto`) alongside the existing `--verbose` flag in `cli.py`'s top-level callback, with `MILHOUSE_PROGRESS` as the environment override consulted when the flag is not given. Precedence matches `Config`'s existing rule for `MILHOUSE_*` variables and CLI flags: default, then env var, then explicit flag. `auto` resolves once, at startup, to `live` when the output stream is a capable terminal and to `plain` otherwise. "Capable" is `rich.console.Console(file=stream).is_terminal` — rich is already a hard dependency of typer 0.27, its `is_terminal` already folds in `NO_COLOR`, `TERM=dumb`, and a redirected stream, and re-implementing that check would just be a worse copy of it.

### Where `logging` sits

Unchanged. `--verbose` still sets `DEBUG` on the root logger (`cli.py:124`), it is still the channel for what subprocesses were run and what they returned, and it is still not the progress channel. An event and a log record answer different questions — "what should a person watching the run see" against "what did milhouse actually do" — and folding one into the other would mean a progress consumer filtering DEBUG records by a field invented for the purpose, which is typed events again with extra steps.

### Why there is no daemon

Nothing here needs one, and this reaffirms [ADR 0005](0005-milhouse-owns-the-loop.md), [ADR 0008](0008-crash-recovery-by-reconciliation.md), [ADR 0015](0015-one-run-at-a-time.md), and [ADR 0021](0021-iteration-history-goes-in-the-beads-audit-log.md) rather than revisiting any of them.

The state a watcher needs is already durable, and outside the process that is running:

- **Beads' audit log** has `dispatches()` for what is in flight and the `iteration`/`claim`/`dispatch` entries [ADR 0021](0021-iteration-history-goes-in-the-beads-audit-log.md) put there for what already happened.
- **herdr** has the live lane list — which workspaces exist and what branch each is on ([ADR 0020](0020-a-lane-is-a-herdr-worktree.md)).
- **git** has the branches and commits any lane produced.
- **`lock.json`** names the run that currently holds the lock ([ADR 0015](0015-one-run-at-a-time.md)).

`milhouse status` already reads exactly this, from outside the running process, including `lock.json` and `audit.unsettled_claims()`. Nothing proposed here adds state a daemon would need to hold in memory instead. What the foreground process is for is policy between turns — choosing the next ready issue, running the gate, merging a worker lane into the integration branch in a deterministic order ([ADR 0024](0024-an-integration-lane-and-worker-lanes.md)), and applying the halt table — and none of that is memory a second process could serve. A live renderer watching a run it did not start is therefore a consumer of the same durable state `status` already reads, not a reason to invent a process that stays up between runs.

### What is out of scope, and why the seam makes it cheap

**A JSONL renderer** — one event per line, machine-readable. Not built here, because nothing yet consumes one. Once something does, it is a third `Renderer` implementation behind the same selection rule (`--progress json`, say), with no change to any producer, because every call site already emits `Event` rather than a formatted string.

**A full-screen (alt-screen) TUI.** Considered and rejected, not deferred for lack of time. It takes over the terminal for the length of a run, and the epic's own diagnosis is that the thing actually wrong was the _volume_ of output and its uniform weight, not the absence of panes. `live`'s redrawn-in-place table gets the same legibility — one row per lane, current state visible at a glance — without giving up scrollback or taking over the terminal. If a full-screen view is wanted later, it is one more `Renderer` over the same `Event` stream, which is exactly what building the seam now buys.

## Rationale

The boundary belongs between "something happened" and "how it looks" because those are owned by different code today and should be owned by different code on purpose. `step.py`, `run.py`, and `parallel.py` know what happened; nothing in them should know about terminals, indentation, or whether the process is attached to one. Moving formatting into a renderer is what makes `live` and `plain` genuinely two implementations of one contract rather than one code path with a flag threaded through it.

The heartbeat's shape follows directly from the epic's own numbers: a poll every 5 seconds against a 30-minute timeout is not an event stream problem, it is a rendering problem, and the fix is to give the renderer the raw material (state, elapsed) to decide how often to say something, rather than to have the caller decide by not calling `report` as often — which would take the same information away from `live`, which wants every poll to redraw its table.

## Alternatives considered

**Keep `Reporter` as `Callable[[str], None]` and move indentation into a wrapper around it.** Rejected. It still bakes the sentence's final shape at the call site, so `live`'s table and `plain`'s dedup both need to parse strings back apart to get at what changed — the boundary is in the same wrong place, just wrapped.

**Route progress through `logging` at a dedicated level.** Rejected. It conflates two audiences that `--verbose` already keeps apart, and a progress consumer would end up filtering `DEBUG` records by an invented field, which is a worse-typed `Event`.

**Build the alt-screen TUI now, since the live table is most of the work anyway.** Rejected above, and for the reason given: the volume was the bug, not the lack of panes, and an alt-screen view costs scrollback that a redrawn-in-place table does not.

## Consequences

- `session.about()` and `Reporter` go. Every `session.report(...)` call site in `step.py`, `run.py`, `parallel.py`, and `session.py` becomes `session.report(Event(...))`, which is the shape of the migration [milhouse-lyq.2](.) does.
- `cli.py`'s `report=typer.echo` wiring (`cli.py:604`) is replaced by constructing a renderer from `--progress`/`MILHOUSE_PROGRESS` and handing its `handle` method (or equivalent callback) to the session instead.
- `rich` moves from an incidental transitive dependency (via typer) to one milhouse code imports directly, for `Console.is_terminal` and the live table. No new install weight, since typer 0.27 already requires it.
- A renderer is now a place a future kind of consumer plugs in without touching `step.py`, `run.py`, or `parallel.py` again — which is the point of the seam, and is what makes the JSONL renderer and the alt-screen TUI genuinely small later rather than merely postponed.
- `docs/usage.md` gains `--progress` and `MILHOUSE_PROGRESS` once [milhouse-lyq.2](.)/[milhouse-lyq.4](.) implement them; this ADR settles the rule, not the doc entry.

## Revisit when

A second renderer (JSONL, or a full-screen view) is actually wanted, or `live`'s table is watched against a wide `--count` run and found to need more than one row per issue to stay legible.
