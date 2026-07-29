# Architecture

## The step

One iteration is the unit milhouse is built from, and one `milhouse step` runs exactly one ([ADR 0014](decisions/0014-step-is-the-primitive.md)). Nothing repeats it: a loop needs a policy, and that policy is the open question ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)).

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
  ...without --wait               settled? no  → leave it
  record the dispatch             settled? yes → transcript, verify,
  hand the claim off                             classify, decide
  return
```

Everything above the prompt and everything below it is shared: `step` is `dispatch`-one plus the wait plus `reap`-that-one, which is why splitting it left `outcome.py` and `policy.py` untouched. The two halves are joined by a `dispatch` entry in the audit log, so the process that reaps a turn need not be the one that started it.

This is not a loop. `dispatch` starts a bounded number of turns once and returns; nothing decides whether there should be more ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)). What went away is the requirement that turns be serial, and with it the repo-wide run lock — the lock is per lane now, and `bd ready --claim` is what makes two dispatchers safe ([ADR 0015](decisions/0015-one-run-at-a-time.md)).

## Lanes

Every turn happens in a **lane**: a herdr worktree labelled with the issue id, which is a checkout of its own, on a branch of its own, in a workspace of its own ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)). That container is what will let several agents work at once, and herdr already had it.

**herdr is the registry.** `herdr worktree list` says what lanes exist and on what branches; `herdr workspace list` says which issue each one is for, because the id is the workspace label. milhouse keeps no lane state — the same rule it applies to issues.

Assignment is the one part that is milhouse's own judgement, and it is four rules over the dependency graph:

| The issue                               | Gets                                           |
| --------------------------------------- | ---------------------------------------------- |
| already has a lane                      | that lane, on the branch it is already on      |
| has one blocker in a live lane          | a new **tab** in that lane, same branch        |
| has none, or blockers with no live lane | a new worktree, branched from the primary      |
| has blockers in **two** lanes           | refused — two candidate bases, and no rule yet |

A second attempt at an issue therefore lands on the branch the first one committed to, and a chain of dependent issues stacks on one branch rather than forking. The last row is [deliberately undecided](decisions/README.md#still-open) and is the first thing that will bite.

herdr checks lanes out under `~/.herdr/worktrees/`, outside the repository, so a lane's untracked files cannot show up as a dirty tree in another lane's classification. milhouse writes a `.git/info/exclude` entry for any lane that lands inside the repository anyway, because that failure would be silent.

The defining property of ralph is a **fresh context window every iteration**. milhouse gets that by starting a new agent in the pane each step and exiting it when the turn ends, rather than reusing one long-lived session. State lives in beads and git, never in an accumulating chat session.

That is what the ralph methodology is about, and it is not the part that was de-scoped. What is missing is stringing the iterations together automatically, which needs a policy nobody has earned yet. `step()` already takes the policy as an argument, so writing one is writing a function.

## The layering

Five layers, each defined by what it is **not** allowed to do. The filenames below are what they happen to be called here. The constraints are the design, and they are what any new piece of milhouse gets sorted into.

| Layer           | Owns                                      | Pure? | May not                                    |
| --------------- | ----------------------------------------- | ----- | ------------------------------------------ |
| **Resources**   | The lock, the lane, the runner, the claim | no    | decide anything                            |
| **Work**        | One unit of work, start to finish         | no    | decide what it means, or whether to repeat |
| **Observation** | What happened                             | yes   | perform I/O                                |
| **Judgement**   | What to do about what happened            | yes   | perform I/O, or observe                    |
| **Repetition**  | How many units of work happen             | no    | anything else                              |

In this codebase: `session.py`, `step.py`, `outcome.py`, `policy.py`, and — today — nothing at all.

### Why observation and judgement are separate

The usual advice would stop at a functional core and an imperative shell, with one pure core. Splitting the core in two is the part worth keeping, because the two halves change for different reasons and at different rates.

**Observation** changes when the tools change: a new herdr agent status, a different question to ask git, a verification result to fold in. **Judgement** changes when you learn something about how runs actually fail. Keeping them apart means tuning what milhouse does about a stalled iteration never touches the code that reads `git log`, and adding a new outcome never silently changes what happens to an issue.

It also means two decision tables instead of one, and a table is the cheapest thing in the world to test exhaustively. `test_outcome.py` and `test_policy.py` between them run no subprocess and start no agent.

### Why the empty layer matters

**Repetition is a layer with nothing in it.** That is not an omission, it is the current state of the design ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)): `milhouse step` runs one unit of work and a person decides whether there is another.

Having it named and empty is what made removing the loop cost one file. Nothing below it had a position on how many iterations there would be, so nothing below it changed. The same property is what will make putting one back cheap, and it is the test to apply when adding anything: if a new piece would need to know how many units of work are coming, it is in the wrong layer.

## Modules

```
src/milhouse/
  cli.py         typer app — step, dispatch, reap, status, doctor. Parsing only.
  completion.py  what each parameter offers on tab. Filesystem and constants only.
  config.py      layered: defaults < .milhouse/config.toml < env < flags
  models.py      Issue, Iteration (pydantic values)
  rundir.py      .milhouse/runs — turn artifacts and the run lock
  audit.py       AuditLog — the iteration history, in bd's audit trail
  lanes.py       Lane, Lanes — which worktree an issue is worked in
  proc.py        run() / run_json() — the single subprocess chokepoint
  errors.py      MilhouseError hierarchy, mapped to exit codes
  gitrepo.py     one working directory: read HEAD, ask what landed, branch it
  doctor.py      preflight checks, as data
  tracker/
    base.py      Tracker protocol (ready, get, children, release, note)
    beads.py     bd wrapper
  herdr.py       narrow client over the herdr CLI — swappable transport
  runner.py      Runner protocol, and AgentRunner — start/prompt/read/exit
  session.py     Session — lock, branch, workspace, lane, claim. No policy.
  outcome.py     classify(issue_after, git, agent_state) -> Verdict
  policy.py      decide(iteration) -> Decision. No I/O.
  verify.py      run the repo's own gate over an issue the agent closed
  step.py        step / dispatch / reap — one turn, whole or in halves
  prompts/
    iterate.md.j2   per-issue prompt
```

### The rules the boundaries enforce

- **Everything external goes through `proc.py`.** No module calls `subprocess` directly. That is the seam tests fake, and the only place that knows about timeouts and JSON parsing.
- **`herdr.py` is a narrow client.** Swapping the CLI transport for the socket API ([ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md)) should be one file, not a refactor. Nothing above it knows argv exists.
- **`gitrepo.py` reads one working directory.** A `GitRepo` is bound to the path it was given, and a turn is classified against the directory the agent actually worked in — the repository root today, a worktree once lanes exist ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)). Reading the root instead would credit an issue with commits someone else made, whether that is another lane or a human in another terminal.
- **`outcome.py` and `policy.py` are pure.** See [the layering](#the-layering): values in, values out, so every row of both decision tables is a unit test with no subprocess involved.
- **`session.py` holds no policy.** It does not decide what to work on next or whether there is a next. That is what would let a loop reuse it unchanged.
- **`cli.py` holds no behaviour, and no private attributes.** It resolves config, drives a `Session` through public methods, and formats the result.
- **`completion.py` never raises and never calls a server.** Its callbacks run on a keypress, in a shell with nowhere to show a traceback, so they answer from the filesystem and from constants rather than from `bd`, `herdr`, or `gh`.
- **`Tracker` and `Runner` are protocols with one implementation each.** The protocol is not speculative generality: it is what `tests/doubles.py` implements. `Tracker` is six methods — `ready`, `get`, `children`, `release`, `defer`, `note` — and nothing on it creates an issue.

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

| State                              | Home                                   | Authoritative? |
| ---------------------------------- | -------------------------------------- | -------------- |
| The work: what to do, what is done | beads                                  | yes            |
| The code                           | git, on each lane's branch             | yes            |
| Which lane an issue is worked in   | herdr, found by workspace label        | yes            |
| Which branch a lane is on          | herdr's worktree list, and git         | yes            |
| Iteration and dispatch history     | `.beads/interactions.jsonl`, via `bd`  | bookkeeping    |
| Who is working a lane              | `.milhouse/runs/<issue-id>/lock.json`  | bookkeeping    |
| Exact prompt sent, pane transcript | `.milhouse/runs/<issue-id>/iter-NNN.*` | bookkeeping    |

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
