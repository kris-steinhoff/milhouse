# Architecture

## The step

One iteration is the unit milhouse is built from, and one `milhouse step` runs exactly one ([ADR 0014](decisions/0014-step-is-the-primitive.md)). Three commands drive that same turn: `step` runs one and hands back, `dispatch` and `reap` are it cut in half so several can be in flight, and `run` repeats it until a target is finished.

```
milhouse step
  │
  ├─ open the session ──────► re-open any claim a crashed run left behind, note
  │                           the branch, find or create the source workspace
  │
  └─ one step ─────────────► step():
        bd ready --claim --limit 1 --exclude-type epic  → issue (empty ⇒ nothing ready)
        bd show <issue>          → its blockers, and its parent's description
        open the issue's LANE (a herdr worktree, found or created)
        render iterate prompt for that issue
        herdr agent start    (FRESH agent in the lane's pane)
        herdr agent prompt --wait --until idle --until blocked
        herdr agent read     (capture the transcript)
        exit the agent       (pane returns to a shell prompt)
        verify()             run the repo's own gate, if the issue closed
        outcome.classify()   what the turn achieved, from beads + git
        policy.decide()      what happens to the issue as a result
```

Nothing above puts work _into_ the tracker. There is no task definition and no planning agent: issues arrive in beads by whatever process the human wants, and milhouse works whatever `bd ready` offers ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md)).

### The seam in the middle

`herdr agent prompt --wait` is the fifth line, and blocking there is what stops two turns running at once. So the turn also exists cut in half ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)):

```
milhouse dispatch -n 3        milhouse reap
  claim, lane, prompt           for each dispatched turn:
  ...waiting for the              settled? no  → leave it
     submission, not              settled? yes → transcript, verify,
     for the turn                                classify, decide
  record the dispatch
  hand the claim off
  return
```

Everything above the prompt and everything below it is shared: `step` is `dispatch`-one plus the wait plus `reap`-that-one, which is why splitting it left `outcome.py` and `policy.py` untouched. The two halves are joined by a `dispatch` entry in the audit log, so the process that reaps a turn need not be the one that started it.

**The half that does not wait still waits for one thing.** herdr requires an observed state change before `--wait` waits on anything, so `dispatch` asks for that much and no more: it returns as soon as the agent is seen to react, which is a fraction of a second, and never waits out a turn. Without it a swallowed prompt left an agent reporting the same `idle` as one that had finished, and the poller collected a turn nothing had ever run ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)).

**A prompt that will not take ends the dispatch, and the turn is handed back.** It is settled on the spot, since an agent that never ran will never be reaped, and it is returned rather than dropped: it is an `error` iteration, which is a row of the halt table, and a caller that never learns the turn existed reports a sick agent side as a stuck queue. The call stops there because everything `dispatch` can settle is about the agent side rather than about one issue — a failure to prepare one issue is raised by `_prepare` instead — and the next issue would be handed to the same herdr.

Neither half is a loop. `dispatch` starts a bounded number of turns once and returns, and nothing in either decides whether there should be more. What went away is the requirement that turns be serial, and with it the repo-wide run lock — the lock is per lane now, and `bd ready --claim` is what makes two dispatchers safe ([ADR 0015](decisions/0015-one-run-at-a-time.md)).

## The run

`milhouse run <target>...` is the loop over that turn ([ADR 0022](decisions/0022-the-loop-is-earned.md)). A target is a beads id, so nothing about it reintroduces the task definition [ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md) removed. More than one target is worked as their union, in one run rather than several ([ADR 0025](decisions/0025-a-multi-target-run-shares-one-lane.md)).

```
milhouse run <target>... [--count N]
  │
  ├─ scope.resolve_many(targets) ─► a Tracker fenced to the union:
  │                             one target, epic     → bd ready --parent <target>
  │                             one target, leaf issue → the target + its unmet blockers
  │                             several targets       → each target's own members, unioned
  │
  ├─ open the session ──────► with lane_key=scope.key: the INTEGRATION lane and its lock
  │                           worker_lanes above --count 1: a lane and a lock per issue
  │
  └─ repeat ───────────────► the body, with policy=unattended(max_attempts)
        --count 1 → step():   claim, wait, settle, in the integration lane
        --count N → Parallel: dispatch up to N, poll at [run] poll_ms, reap,
                              merge each success into the integration branch,
                              hand back one finished turn per call
        nothing claimed?  → nothing_ready() says finished or deadlocked, stop
        should_halt()     → blocked agent, milhouse error, dirty tree, a merge
                            that did not land, a red integration branch, ceiling
        halting?          → drain: start nothing more, finish what is in flight
                            (and merge none of it, once a merge has not landed)
        otherwise         → go again
```

Three things are worth reading off that. **Scope is a tracker**, so no layer below `run` learns that a target exists, or how many. **The lanes nest**: the target's lane is the branch a person reviews, and above `--count 1` each issue in flight also gets a worker lane branched from that branch and merged back into it as its turn settles ([ADR 0023](decisions/0023-a-run-has-one-lane.md), [ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)). Above one target, `scope.key` is every target's id, sorted and joined with `+`, so the lane is still one name regardless of the order they were typed in ([ADR 0025](decisions/0025-a-multi-target-run-shares-one-lane.md)). And **the loop body is an argument**, defaulting to one `step`, which is how `--count N` arrived: `parallel.Parallel` is a different body rather than a different loop. What that actually cost is [below](#the-empty-layer-and-what-filling-it-cost).

The agent is still started fresh every iteration and exited when the turn ends. Reusing a lane's checkout does not change that, because the fresh context window comes from restarting the agent rather than from the worktree.

## Lanes

Every turn happens in a **lane**: a herdr worktree with a label on it, which is a checkout of its own, on a branch of its own, in a workspace of its own ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)). That container is what lets several agents work at once, and herdr already had it.

**The label is the unit somebody will review**, and that differs between the two ways of driving milhouse ([ADR 0023](decisions/0023-a-run-has-one-lane.md)). `dispatch` reviews an issue, so a lane is labelled with an issue id and assigned by the rules below. `run` reviews its target(s), so the run gets a lane labelled with `scope.key` — one target's id unchanged, or every target's id sorted and joined above one ([ADR 0025](decisions/0025-a-multi-target-run-shares-one-lane.md)) — and none of the rules apply.

A concurrent run needs both keys, one nested inside the other ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)):

| Lane            | Labelled with | On branch                       | Holds                                                 |
| --------------- | ------------- | ------------------------------- | ----------------------------------------------------- |
| **integration** | `scope.key`   | `milhouse/<scope.key>`          | the branch a person reviews; every merge happens here |
| **worker**      | an issue      | `milhouse/<scope.key>--<issue>` | one turn, branched from the integration branch        |
| `dispatch`      | an issue      | `milhouse/<issue>`              | one turn, branched from the primary checkout          |

At `--count 1` there are no worker lanes at all, so a serial run is ADR 0023 unchanged: it works in the integration lane and has nothing to merge into it.

**The worker separator is `--`, and that is git's decision rather than milhouse's.** Refs are a directory hierarchy, so `refs/heads/milhouse/bd-e` and `refs/heads/milhouse/bd-e/bd-e.1` are a file and a directory of the same name, and git refuses the second with `cannot lock ref`. The integration branch is the one a person reviews, so the worker branch is the one that gave way. It lives in one constant, `lanes.WORKER_SEPARATOR`, because `milhouse status` splits on it to print worker lanes under the integration lane they land in, and two spellings would silently stop grouping rather than fail.

A worker lane is labelled with its issue and not with its namespaced branch, which is what lets `Lanes.locate` find it and reconciliation tell an in-flight turn from an orphaned claim. The branch is then the only thing distinguishing it from a `dispatch` lane for the same issue, which is why an existing worker lane is looked up by branch.

**herdr is the registry.** `herdr worktree list` says what lanes exist and on what branches; `herdr workspace list` says what each one is labelled with. milhouse keeps no lane state — the same rule it applies to issues.

Assignment under `dispatch` is the one part that is milhouse's own judgement, and it is four rules over the dependency graph:

| The issue                               | Gets                                           |
| --------------------------------------- | ---------------------------------------------- |
| already has a lane                      | that lane, on the branch it is already on      |
| has one blocker in a live lane          | a new **tab** in that lane, same branch        |
| has none, or blockers with no live lane | a new worktree, branched from the primary      |
| has blockers in **two** lanes           | refused — two candidate bases, and no rule yet |

A second attempt at an issue therefore lands on the branch the first one committed to, and a chain of dependent issues stacks on one branch rather than forking. The last row is [deliberately undecided](decisions/README.md#still-open) and is the first thing that will bite.

herdr checks lanes out under `~/.herdr/worktrees/`, outside the repository, so a lane's untracked files cannot show up as a dirty tree in another lane's classification. milhouse writes a `.git/info/exclude` entry for any lane that lands inside the repository anyway, because that failure would be silent.

The defining property of ralph is a **fresh context window every iteration**. milhouse gets that by starting a new agent in the pane each step and exiting it when the turn ends, rather than reusing one long-lived session. State lives in beads and git, never in an accumulating chat session.

`milhouse run` reuses a lane across iterations and does not weaken that: the agent is still started fresh and exited each turn, and the only thing carried between them is what a `bd note` and a commit carry. That holds whether the turns share the integration lane or each get a worker lane of their own.

## The layering

Five layers, each defined by what it is **not** allowed to do. The filenames below are what they happen to be called here. The constraints are the design, and they are what any new piece of milhouse gets sorted into.

| Layer           | Owns                                      | Pure? | May not                                    |
| --------------- | ----------------------------------------- | ----- | ------------------------------------------ |
| **Resources**   | The lock, the lane, the runner, the claim | no    | decide anything                            |
| **Work**        | One unit of work, start to finish         | no    | decide what it means, or whether to repeat |
| **Observation** | What happened                             | yes   | perform I/O                                |
| **Judgement**   | What to do about what happened            | yes   | perform I/O, or observe                    |
| **Repetition**  | How many units of work happen             | no    | anything else                              |

In this codebase: `session.py`, `step.py`, `outcome.py`, `policy.py`, and `run.py`.

### Why observation and judgement are separate

The usual advice would stop at a functional core and an imperative shell, with one pure core. Splitting the core in two is the part worth keeping, because the two halves change for different reasons and at different rates.

**Observation** changes when the tools change: a new herdr agent status, a different question to ask git, a verification result to fold in. **Judgement** changes when you learn something about how runs actually fail. Keeping them apart means tuning what milhouse does about a stalled iteration never touches the code that reads `git log`, and adding a new outcome never silently changes what happens to an issue.

It also means two decision tables instead of one, and a table is the cheapest thing in the world to test exhaustively. `test_outcome.py` and `test_policy.py` between them run no subprocess and start no agent.

### The empty layer, and what filling it cost

**Repetition was a layer with nothing in it**, from [ADR 0017](decisions/0017-no-loop-until-it-is-earned.md) until [ADR 0022](decisions/0022-the-loop-is-earned.md). The argument for naming it anyway was that having it named and empty is what made removing the loop cost one file: nothing below it had a position on how many iterations there would be, so nothing below it changed, and putting one back would be cheap for the same reason.

That claim has now been tested twice: once by [ADR 0022](decisions/0022-the-loop-is-earned.md), which put a loop back, and once by [ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md), which made the loop concurrent. The first time it mostly held. `run.py` was the new file, and none of the four layers under it moved: `Session` did not learn what a run is, `step()` did not learn that it might be called again, and `outcome.classify` was not touched at all.

What the two cost is worth recording, because it is where the layering was not free:

| Change | Layer | For | Why it was needed |
| --- | --- | --- | --- |
| `Iteration.attempt` | values | 0022 | A pure policy cannot count attempts by looking them up, so the count had to arrive on the value it is handed |
| `policy.unattended`, `Tracker.defer` | Judgement | 0022 | A supervised policy hands every decision to a person; an unattended one has to settle "give up on this issue" itself |
| `Session(lane_key=...)`, `Lanes.open_for` | Resources | 0022 | A run reviews a target rather than an issue, so the lane it works in is a different lane ([ADR 0023](decisions/0023-a-run-has-one-lane.md)) |
| `scope.py` | Resources | 0022 | A target fences the ready queue, and expressing that as a `Tracker` is what kept it out of every layer above |
| `parallel.py` | Repetition | 0024 | The body itself: dispatch up to N, poll the lanes, hand back one finished turn per call so `should_halt` stays pure over one iteration |
| `step.DispatchResult` | Work | 0024 | A turn that could not be started is a finished turn, so `dispatch` hands it back rather than settling it out of sight, and the halt table gets to see it |
| `run.Draining`, `run._drain` | Repetition | 0024 | A halt stops starting work, not work already started, and a concurrent body has N-1 turns whose agents are still going when the table fires |
| `should_halt`'s `conflict` and `integration` rows | Repetition | 0024 | Two ways to stop that a serial run cannot produce: a branch that did not land, and a branch that went red once two histories were on it |
| `RunResult.still_running`, `merged()`, `unmerged()` | Repetition | 0024 | "Closed" and "on the branch you are about to review" stop being the same thing, and a report whose numbers look complete is worse than a short one |
| `Parallel(max_iterations=...)` | Repetition | 0024 | A turn is spent when it is dispatched rather than when it is reported, so the ceiling has to be counted where the dispatching happens |
| `Session(worker_lanes=...)`, `integration_lane()`, `lock_for` keyed by the issue | Resources | 0024 | Two levels of lane means two levels of lock: the target's, and one per lane an issue is being worked in |
| `Lanes.open_worker`, `worker_branch`, `WORKER_SEPARATOR` | Resources | 0024 | A worker lane's branch is namespaced under the target so two runs cannot collide, and git refused the first spelling of that name |
| `step._land`, `step._verify_integration`, `step.merge_line` | Work | 0024 | Landing a turn is part of finishing it, and a merge that joined two histories leaves a tree the gate has never been run against |
| `MergeRecord`, `Iteration.merge`, `Iteration.integration_verified` | values | 0024 | A pure halt table cannot ask git what a merge did, so what it did has to arrive on the value it is handed — the same shape as `Iteration.attempt` |
| `Session.refused_merge`, `MergeRecord.skipped` | Resources, values | 0024 | A merge that did not land is the last merge of the run, and the state that says so belongs to the integration branch rather than to the drain, because turns settling in one reap pass are all merged before the loop is told about the first |
| `scope.resolve_many`, `Scope.targets`, `Scope.key` | Resources, values | 0025 | Several targets have no single `bd --parent` fence to share, so the union is computed once in `scope.py`, and the lane needs a name several ids can share |
| `--count`, `cli._body`, `[run] max_parallel`, `[run] poll_ms` | surface | 0024 | The width is a flag and a config key, and `cli._body` is the one place that turns it into a body |

**The 0022 rows are not repetition leaking downwards.** Each is a thing that was underspecified while a person was in the loop, and had to be decided once nobody was.

**The 0024 prediction did not hold, and it is worth being exact about how.** The prediction was that a `--count N` run would replace `run.py`'s loop body and nothing else. What actually shipped added `parallel.py` and changed `run.py`, `session.py`, `step.py`, `lanes.py`, `models.py`, `config.py`, and `cli.py`.

What did **not** leak is the count. Nothing below the Repetition layer learned how many turns are coming: `worker_lanes` is a mode rather than a number, `step._land` merges one turn without knowing whether another exists (what it reads besides the turn is what the integration branch has already done, which is not a count either), `outcome.py` and `policy.py` were not touched at all, and N itself lives in `parallel.py`, the config key it arrives from, and the one line of `cli.py` that turns one into the other. The original test survived the thing it was written for.

What leaked instead is **simultaneity**, which is a different fact about a run than its width. Two turns at once means two branches where there was one, so every layer that touches a branch had to learn the difference between the branch a turn commits to and the branch a person reviews: `lanes.py` to name it, `session.py` to open and lock it, `step.py` to merge into it and verify it, `models.py` to carry what the merge did, and `run.py` to have an opinion when it fails. The leak went sideways rather than downwards.

The count did leak once, inside the Repetition layer: `Parallel` is handed `max_iterations` as well, because `run()`'s own counter sees a turn only when it is handed back, and a `--count 4` run would overshoot its ceiling by three. Two objects now count the same budget, and they agree only because `Parallel` counts what it has dispatched rather than what it has reported.

So the test to apply to the next addition is the original one plus what the second test taught: **if a new piece would need to know how many units of work are coming, it is in the wrong layer — but needing to know that another turn exists right now is a different question, and the answer to it is a mode, a branch, or a lock rather than a number.**

## Modules

```
src/milhouse/
  cli.py         typer app — step, run, dispatch, reap, status, doctor. Parsing only.
  completion.py  what each parameter offers on tab. Filesystem and constants only.
  config.py      layered: defaults < .milhouse/config.toml < env < flags
  models.py      Issue, Iteration, MergeRecord, Graph (pydantic values)
  rundir.py      .milhouse/runs — turn artifacts and the run lock
  audit.py       AuditLog — the iteration history, in bd's audit trail
  lanes.py       Lane, Lanes — which worktree a turn is worked in
  proc.py        run() / run_json() — the single subprocess chokepoint
  errors.py      MilhouseError hierarchy, mapped to exit codes
  gitrepo.py     one working directory: read HEAD, ask what landed, branch it
  doctor.py      preflight checks, as data
  tracker/
    base.py      Tracker protocol (ready, get, children, release, defer, note)
    beads.py     bd wrapper
  herdr.py       narrow client over the herdr CLI — swappable transport
  runner.py      Runner protocol, and AgentRunner — start/prompt/read/exit
  renderer.py    Event, Renderer protocol, PlainRenderer, LiveRenderer — what happened, how it looks
  session.py     Session — lock, branch, workspace, lane, claim. No policy.
  outcome.py     classify(issue_after, git, agent_state) -> Verdict
  policy.py      decide / unattended(max_attempts) -> Decision. No I/O.
  verify.py      run the repo's own gate over an issue the agent closed
  step.py        step / dispatch / reap — one turn, whole or in halves
  scope.py       resolve_many(targets) -> a Tracker fenced to their union
  run.py         the loop, the halt rules, and what a finished run reports
  parallel.py    Parallel — a loop body that keeps N turns in flight at once
  prompts/
    iterate.md.j2   per-issue prompt
```

### The rules the boundaries enforce

- **Everything external goes through `proc.py`.** No module calls `subprocess` directly. That is the seam tests fake, and the only place that knows about timeouts and JSON parsing.
- **`herdr.py` is a narrow client.** Swapping the CLI transport for the socket API ([ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md)) should be one file, not a refactor. Nothing above it knows argv exists.
- **`gitrepo.py` reads one working directory.** A `GitRepo` is bound to the path it was given, and a turn is classified against the directory the agent actually worked in — the repository root today, a worktree once lanes exist ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)). Reading the root instead would credit an issue with commits someone else made, whether that is another lane or a human in another terminal.
- **`outcome.py` and `policy.py` are pure.** See [the layering](#the-layering): values in, values out, so every row of both decision tables is a unit test with no subprocess involved.
- **`session.py` holds no policy.** It does not decide what to work on next or whether there is a next. That is what let `run.py` reuse it unchanged.
- **`renderer.py` is the only place that knows about terminals.** `step.py`, `run.py`, `parallel.py`, and `session.py` report what happened as an `Event`; nothing below the CLI decides how it looks, whether it is indented, or whether it is shown at all ([ADR 0026](decisions/0026-the-progress-channel-is-events-and-the-terminal-is-one-renderer.md)). `PlainRenderer` and `LiveRenderer` are two implementations of the same `handle(event)` contract, chosen by `--progress`/`MILHOUSE_PROGRESS`; `LiveRenderer` holds no reference to `Session`, the tracker, or herdr, so its table is a pure function of the event sequence it has seen.
- **`scope.py` produces a `Tracker`.** One target, or several unioned, fences the ready queue, and expressing the fence as a tracker is what keeps `Session`, `step`, `dispatch`, and `reap` from ever hearing that a target exists, let alone how many.
- **`run.py` owns only the count.** It may not classify a turn, decide what becomes of an issue, or know what is in scope. Its loop body is an argument for the same reason.
- **`cli.py` holds no behaviour, and no private attributes.** It resolves config, drives a `Session` through public methods, and formats the result.
- **`completion.py` never raises and never calls a server.** Its callbacks run on a keypress, in a shell with nowhere to show a traceback, so they answer from the filesystem and from constants rather than from `bd`, `herdr`, or `gh`.
- **`Tracker` and `Runner` are protocols with one implementation each.** The protocol is not speculative generality: it is what `tests/doubles.py` implements. `Tracker` is seven methods — `ready`, `get`, `children`, `graph`, `release`, `defer`, `note` — and nothing on it creates an issue.
- **`Graph` reasons, `graph()` fetches.** `Tracker.graph()` is the two `bd` calls that build the value; every question about it — `frontier()`, `waves()`, `width`, `blocked_behind()` — is a pure method on `models.Graph`, so what the dependency graph means is a unit test rather than a scenario. The ready queue already _is_ the frontier, so the graph is not what makes concurrency possible: it is how a run says how wide the scope is before it starts, and what everything is stuck behind when it stops.

## Data flow

```
bd ready --claim ──────► Issue ──► bd show <parent> ──► background
                              │                            │
                              ▼                            ▼
                        prompts/iterate.md.j2 ◄────────────┘
                              │
                              ▼
                     herdr.py ──► pane ──► agent ──► commits + bd close
                              │
                              ▼
                    outcome.classify(...) ──► Iteration ──► bd audit record
                              │                    │
                              │                    ▼
                              └──────────► policy.decide(...) ──► the issue's
                                                                   next status
```

The dependency graph is the only structure milhouse reads, and `bd` owns it. Nothing joins an issue to anything outside the tracker.

## Where state lives

| State                              | Home                                     | Authoritative? |
| ---------------------------------- | ---------------------------------------- | -------------- |
| The work: what to do, what is done | beads                                    | yes            |
| The code                           | git, on each lane's branch               | yes            |
| Which lane a turn is worked in     | herdr, found by workspace label          | yes            |
| Which branch a lane is on          | herdr's worktree list, and git           | yes            |
| What a run gave up on              | beads, as a deferred issue with a reason | yes            |
| Iteration and dispatch history     | `.beads/interactions.jsonl`, via `bd`    | bookkeeping    |
| Who is working a lane              | `.milhouse/runs/<lane-key>/lock.json`    | bookkeeping    |
| Exact prompt sent, pane transcript | `.milhouse/runs/<issue-id>/iter-NNN.*`   | bookkeeping    |

The lane key is the issue for a `dispatch` and `scope.key` for a `run`'s integration lane ([ADR 0023](decisions/0023-a-run-has-one-lane.md), [ADR 0025](decisions/0025-a-multi-target-run-shares-one-lane.md)), so a serial run holds one lock however many issues or targets it works. A concurrent one holds the integration lane's lock plus one per worker lane, keyed by the issue exactly as `dispatch` keys its own, so nothing else can be working an issue a run has in flight ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)). Turn artifacts stay filed under the issue that was worked, because that is what a post-mortem looks for.

**A run keeps nothing of its own either.** What it deferred is in beads, what it committed is in git, what it did is in the audit log, and how far it got is recoverable from those three. There is no run file.

**milhouse stores no state of its own.** Every row above is somebody else's, except the lock and the turn artifacts — and the artifacts are captured text with no other home, because herdr's scrollback is live, bounded, and gone once a pane is replaced.

Getting there was a rule applied one row at a time: for each thing milhouse was keeping, does something else already own it? `state.json` did not survive the question. The task fields went with the task, the epic went with the planner, the workspace is herdr's, the branch is git's, and the in-flight claim is `bd`'s ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md), [ADR 0021](decisions/0021-iteration-history-goes-in-the-beads-audit-log.md)).

The history went to `bd audit record`, which appends one JSON object per line to a file bd already writes its own `field_change` entries into. One ordered trail beats two, and it matters more as concurrency arrives: "agent closed `milhouse-5m6`" and "milhouse classified that turn as `rejected`" sitting in one file is a post-mortem no per-run file gives you. The price is that milhouse parses a file it does not own, against a schema it does not control, because `bd audit` has `record` and `label` and no query.

Entries stay small on purpose. That file has many concurrent writers — every agent's own `bd close` appends from its own process — and POSIX guarantees an atomic append only below `PIPE_BUF`. So the entry carries the verdict and a path to the transcript, never the verification output.

Everything under `.milhouse/runs/` is gitignored and safe to delete. Doing so loses the prompts and transcripts, nothing else. See [troubleshooting](troubleshooting.md) for the layout.

The history is no longer among them, and that is a cost as well as a gain: `rm -rf .milhouse/runs/` used to lose the history and nothing else, and now the history lives with the issue data.

milhouse keeps the run directory ignored itself, by writing a self-ignoring `.milhouse/runs/.gitignore` the first time it creates the directory. Nothing has to be committed for that to work, and nobody has to remember to do it. The alternative was worse than untidy: the run lock is the first thing a session writes, an unignored lock file is an uncommitted change, and a turn is classified partly on whether the working tree is dirty — so milhouse's own bookkeeping showed up in the reading it took of the agent.

## Testing

Three tiers, because there is no headless path to an interactive agent:

1. **Unit tests with fakes.** `tests/fakes.py` fakes at the `proc.py` boundary, replaying `bd` and `herdr` JSON recorded from the real tools, so the argv every client builds stays under test. `tests/doubles.py` fakes one level up, at the tracker, herdr client, git, and runner, because what the session and step tests are about is decisions rather than argv. `uv run pytest`.
2. **Live-tool tests**, marked `herdr` and `beads`. These drive the real herdr server with plain shell commands (no agents) and a real scratch `bd` database. `uv run pytest -m herdr` / `-m beads`.
3. **A manual end-to-end check.** Documented in [usage](usage.md#end-to-end-check). It needs eyes on it: confirm the pane returns to a shell prompt between iterations, which is what proves the context is actually fresh.
