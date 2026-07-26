# Architecture

## The step

One iteration is the unit milhouse is built from, and one `milhouse step` runs exactly one ([ADR 0014](decisions/0014-step-is-the-primitive.md)). Nothing repeats it: a loop needs a policy, and that policy is the open question ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)).

```
milhouse step <task_definition>
  │
  ├─ resolve source ────────► TaskDefinition (title, body, task_id)
  │                            file:docs/feature-x.md  |  gh:owner/repo#123
  │
  ├─ open the session ──────► take the run lock, re-open any claim a crashed
  │                           run left behind, check out the branch, find or
  │                           create the herdr workspace
  │
  ├─ already decomposed? ───► bd list --metadata-field milhouse_task=<id> -t epic --json
  │     │
  │     └─ no ─────────────► run PLANNING agent (one shot)
  │                           it writes plan.json; milhouse creates the issues
  │
  └─ one step ─────────────► step():
        bd ready --parent <epic> --claim --json --limit 1   → issue (empty ⇒ done)
        render iterate prompt for that issue
        herdr agent start    (FRESH agent in the task's pane)
        herdr agent prompt --wait --until idle --until blocked
        herdr agent read     (capture the transcript)
        exit the agent       (pane returns to a shell prompt)
        verify()             run the repo's own gate, if the issue closed
        outcome.classify()   what the turn achieved, from beads + git
        policy.decide()      what happens to the issue as a result
```

The defining property of ralph is a **fresh context window every iteration**. milhouse gets that by starting a new agent in the pane each step and exiting it when the turn ends, rather than reusing one long-lived session. State lives in beads and git, never in an accumulating chat session.

That is what the ralph methodology is about, and it is not the part that was de-scoped. What is missing is stringing the iterations together automatically, which needs a policy nobody has earned yet. `step()` already takes the policy as an argument, so writing one is writing a function.

## The layering

Five layers, each defined by what it is **not** allowed to do. The filenames below are what they happen to be called here. The constraints are the design, and they are what any new piece of milhouse gets sorted into.

| Layer           | Owns                                             | Pure? | May not                                    |
| --------------- | ------------------------------------------------ | ----- | ------------------------------------------ |
| **Resources**   | The lock, branch, workspace, pane, runner, claim | no    | decide anything                            |
| **Work**        | One unit of work, start to finish                | no    | decide what it means, or whether to repeat |
| **Observation** | What happened                                    | yes   | perform I/O                                |
| **Judgement**   | What to do about what happened                   | yes   | perform I/O, or observe                    |
| **Repetition**  | How many units of work happen                    | no    | anything else                              |

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
  cli.py         typer app — step, plan, status, doctor. Parsing and output only.
  completion.py  what each parameter offers on tab. Filesystem and constants only.
  config.py      layered: defaults < .milhouse/config.toml < env < flags
  models.py      TaskDefinition, Issue, Iteration, RunState (pydantic values)
  state.py       RunStore — state.json, events.jsonl, and the run lock
  proc.py        run() / run_json() — the single subprocess chokepoint
  errors.py      MilhouseError hierarchy, mapped to exit codes
  gitrepo.py     find the repo, read HEAD, ask what landed, put it on a branch
  doctor.py      preflight checks, as data
  sources/
    base.py      Source protocol; resolve(spec) -> TaskDefinition
    file.py      local markdown
    github.py    gh issue view <n> --json title,body,number,url
  tracker/
    base.py      Tracker protocol (create_epic, ready, release, note, ...)
    beads.py     bd wrapper
  herdr.py       narrow client over the herdr CLI — swappable transport
  runner.py      Runner protocol, and AgentRunner — start/prompt/read/exit
  session.py     Session — lock, branch, workspace, epic, claim. No policy.
  outcome.py     classify(issue_after, git, agent_state) -> Verdict
  policy.py      decide(iteration) -> Decision. No I/O.
  verify.py      run the repo's own gate over an issue the agent closed
  step.py        step(session, epic) -> one Iteration, classified and settled
  planner.py     one-shot decomposition: prompt, plan.json, validate, create
  prompts/
    plan.md.j2      decomposition prompt
    iterate.md.j2   per-issue prompt
```

### The rules the boundaries enforce

- **Everything external goes through `proc.py`.** No module calls `subprocess` directly. That is the seam tests fake, and the only place that knows about timeouts and JSON parsing.
- **`herdr.py` is a narrow client.** Swapping the CLI transport for the socket API ([ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md)) should be one file, not a refactor. Nothing above it knows argv exists.
- **`outcome.py` and `policy.py` are pure.** See [the layering](#the-layering): values in, values out, so every row of both decision tables is a unit test with no subprocess involved.
- **`session.py` holds no policy.** It does not decide what to work on next or whether there is a next. That is what would let a loop reuse it unchanged.
- **`cli.py` holds no behaviour, and no private attributes.** It resolves config, drives a `Session` through public methods, and formats the result.
- **`completion.py` never raises and never calls a server.** Its callbacks run on a keypress, in a shell with nowhere to show a traceback, so they answer from the filesystem and from constants rather than from `bd`, `herdr`, or `gh`.
- **`tracker/`, `sources/`, and `Runner` are protocols with one implementation each.** The protocol is not speculative generality: it is what `tests/doubles.py` and `tests/fakes.py` implement.

## Data flow

```
spec string ──sources──► TaskDefinition ──planner──► epic id + child issues
                              │                            │
                              │                            ▼
                              │                    bd ready --claim
                              │                            │
                              ▼                            ▼
                        prompts/iterate.md.j2 ◄──────── Issue
                              │
                              ▼
                     herdr.py ──► pane ──► agent ──► commits + bd close
                              │
                              ▼
                    outcome.classify(...) ──► Iteration ──► events.jsonl
                              │                    │
                              │                    ▼
                              └──────────► policy.decide(...) ──► the issue's
                                                                   next status
```

`TaskDefinition.task_id` is the join key between the user's file and the beads epic ([ADR 0002](decisions/0002-link-issues-via-bead-metadata.md)). Nothing else links them, and nothing else needs to.

## Where state lives

| State                              | Home                                 | Authoritative? |
| ---------------------------------- | ------------------------------------ | -------------- |
| The work: what to do, what is done | beads                                | yes            |
| The code                           | git, on a `milhouse/<slug>` branch   | yes            |
| Which workspace, pane, and branch  | `.milhouse/runs/<slug>/state.json`   | bookkeeping    |
| Iteration history                  | `.milhouse/runs/<slug>/events.jsonl` | bookkeeping    |
| Who is running this task           | `.milhouse/runs/<slug>/lock.json`    | bookkeeping    |
| Exact prompt sent, pane transcript | `.milhouse/runs/<slug>/iter-NNN.*`   | bookkeeping    |

The history is an append-only log rather than a list inside `state.json`, which keeps the state file small enough to rewrite atomically on every save and gives a post-mortem something to read ([ADR 0014](decisions/0014-step-is-the-primitive.md)).

Everything under `.milhouse/runs/` is gitignored and safe to delete. Doing so loses the history, nothing else. See [troubleshooting](troubleshooting.md) for the layout.

milhouse keeps it ignored itself, by writing a self-ignoring `.milhouse/runs/.gitignore` the first time it creates the directory. Nothing has to be committed for that to work, and nobody has to remember to do it. The alternative was worse than untidy: the run lock is the first thing a session writes, an unignored lock file is an uncommitted change, and the branch checkout that comes next refuses to run over one — so the very first step in a fresh repository failed, blaming the user for milhouse's own bookkeeping.

## Testing

Three tiers, because there is no headless path to an interactive agent:

1. **Unit tests with fakes.** `tests/fakes.py` fakes at the `proc.py` boundary, replaying `bd` and `herdr` JSON recorded from the real tools, so the argv every client builds stays under test. `tests/doubles.py` fakes one level up, at the tracker, herdr client, git, and runner, because what the session and step tests are about is decisions rather than argv. `uv run pytest`.
2. **Live-tool tests**, marked `herdr` and `beads`. These drive the real herdr server with plain shell commands (no agents) and a real scratch `bd` database. `uv run pytest -m herdr` / `-m beads`.
3. **A manual end-to-end check.** Documented in [usage](usage.md#end-to-end-check). It needs eyes on it: confirm the pane returns to a shell prompt between iterations, which is what proves the context is actually fresh.
