# Architecture

## The step

One iteration is the unit milhouse is built from. `milhouse step` runs exactly one and hands back to a person. `milhouse run` runs them in a loop. Nothing else differs between them ([ADR 0014](decisions/0014-step-is-the-primitive.md)).

```
milhouse step <task_definition>          milhouse run <task_definition>
  │                                        │
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
  └─ ONE step ──────────────┴─ REPEAT the step ──► step():
        bd ready --parent <epic> --claim --json --limit 1   → issue (empty ⇒ done)
        render iterate prompt for that issue
        herdr agent start    (FRESH agent in the task's pane)
        herdr agent prompt --wait --until idle --until blocked
        herdr agent read     (capture the transcript)
        exit the agent       (pane returns to a shell prompt)
        verify()             run the repo's own gate, if the issue closed
        outcome.classify()   what the turn achieved, from beads + git
        policy.decide()      what happens to the issue, and whether to stop
```

The defining property of ralph is a **fresh context window every iteration**. milhouse gets that by starting a new agent in the pane each iteration and exiting it when the turn ends, rather than reusing one long-lived session. State lives in beads and git, never in an accumulating chat session. That holds for a single `step` as much as for a loop.

**The policy today is supervised**: the run stops at the first iteration that does not succeed and says what needs a person. An unattended ralph policy is a second `decide()` over this same step, not a different loop.

## Modules

```
src/milhouse/
  cli.py         typer app — step, run, plan, status, doctor. Parsing and output only.
  completion.py  what each parameter offers on tab. Filesystem and constants only.
  config.py      layered: defaults < .milhouse/config.toml < env < flags
  models.py      TaskDefinition, Issue, Iteration, RunState (pydantic values)
  state.py       RunStore — state.json, events.jsonl, and the run lock
  proc.py        run() / run_json() — the single subprocess chokepoint
  errors.py      MilhouseError hierarchy, mapped to exit codes
  gitrepo.py     find the repo, read HEAD, put the run on a branch
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
  loop.py        RalphLoop — repeat step() until something says stop
  prompts/
    plan.md.j2      decomposition prompt
    iterate.md.j2   per-issue prompt
```

### The rules the boundaries enforce

- **Everything external goes through `proc.py`.** No module calls `subprocess` directly. That is the seam tests fake, and the only place that knows about timeouts and JSON parsing.
- **`herdr.py` is a narrow client.** Swapping the CLI transport for the socket API ([ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md)) should be one file, not a refactor. Nothing above it knows argv exists.
- **`outcome.py` and `policy.py` are pure.** One says what happened, the other says what to do about it. Values in, values out, so every row of both decision tables is a unit test with no subprocess involved.
- **`session.py` holds no policy.** It does not decide what to work on next or when a run is over. That is what lets `step` and `run` share it.
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
                              └──────────► policy.decide(...) ──► the issue,
                                                                   and stop?
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

## Testing

Three tiers, because there is no headless path to an interactive agent:

1. **Unit tests with fakes.** `tests/fakes.py` fakes at the `proc.py` boundary, replaying `bd` and `herdr` JSON recorded from the real tools, so the argv every client builds stays under test. `tests/doubles.py` fakes one level up, at the tracker, herdr client, git, and runner, because what the session, step, and loop tests are about is decisions rather than argv. `uv run pytest`.
2. **Live-tool tests**, marked `herdr` and `beads`. These drive the real herdr server with plain shell commands (no agents) and a real scratch `bd` database. `uv run pytest -m herdr` / `-m beads`.
3. **A manual end-to-end run.** Documented in [usage](usage.md#end-to-end-check). It needs eyes on it: confirm the pane returns to a shell prompt between iterations, which is what proves the context is actually fresh.
