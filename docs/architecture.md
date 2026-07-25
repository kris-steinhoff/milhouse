# Architecture

## The loop

```
milhouse run <task_definition>
  │
  ├─ resolve source ────────► TaskDefinition (title, body, task_id)
  │                            file:docs/feature-x.md  |  gh:owner/repo#123
  │
  ├─ reconcile ────────────► re-open any claim a crashed run left behind
  │
  ├─ already decomposed? ───► bd list --metadata-field milhouse_task=<id> -t epic --json
  │     │
  │     └─ no ─────────────► run PLANNING agent (one shot)
  │                           it writes plan.json; milhouse creates the issues
  │
  └─ ralph loop ───────────► repeat:
        bd ready --parent <epic> --claim --json --limit 1   → issue (empty ⇒ done)
        render iterate prompt for that issue
        herdr agent start    (FRESH agent in the task's pane)
        herdr agent prompt --wait --until idle --until blocked
        herdr agent read     (capture the transcript)
        exit the agent       (pane returns to a shell prompt)
        classify outcome from beads + git, record it, repeat
```

The defining property of ralph is a **fresh context window every iteration**.
milhouse gets that by starting a new agent in the pane each iteration and exiting
it when the turn ends, rather than reusing one long-lived session. State lives in
beads and git, never in an accumulating chat session.

## Modules

```
src/milhouse/
  cli.py         typer app — run, plan, status, doctor. Parsing and output only.
  config.py      layered: defaults < .milhouse/config.toml < env < flags
  models.py      TaskDefinition, Issue, Iteration, RunState (pydantic)
  proc.py        run() / run_json() — the single subprocess chokepoint
  errors.py      MilhouseError hierarchy, mapped to exit codes
  gitrepo.py     find the repo, read HEAD, put the run on a branch
  doctor.py      preflight checks, as data
  sources/
    base.py      Source protocol; resolve(spec) -> TaskDefinition
    file.py      local markdown
    github.py    gh issue view <n> --json title,body,number,url
  tracker/
    base.py      Tracker protocol (create_epic, ready_claim, close, ...)
    beads.py     bd wrapper
  herdr.py       narrow client over the herdr CLI — swappable transport
  runner.py      AgentRunner — start/prompt/read/exit one iteration
  outcome.py     classify(issue_before, issue_after, git, agent_state)
  planner.py     one-shot decomposition: prompt, plan.json, validate, create
  loop.py        RalphLoop — guardrails, state, signal handling
  prompts/
    plan.md.j2      decomposition prompt
    iterate.md.j2   per-issue prompt
```

### The rules the boundaries enforce

- **Everything external goes through `proc.py`.** No module calls
  `subprocess` directly. That is the seam tests fake, and the only place that
  knows about timeouts and JSON parsing.
- **`herdr.py` is a narrow client.** Swapping the CLI transport for the socket
  API ([ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md)) should be one
  file, not a refactor. Nothing above it knows argv exists.
- **`outcome.py` is pure.** `classify()` takes values and returns an outcome. No
  I/O, so every row of the decision table is a unit test.
- **`cli.py` holds no behaviour.** It resolves config, calls into `loop.py` or
  `planner.py`, and formats the result.
- **`tracker/` and `sources/` are protocols with one implementation each.** The
  protocol is not speculative generality: it is what `tests/fakes.py` implements.

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
                    outcome.classify(...) ──► Iteration ──► RunState (state.json)
```

`TaskDefinition.task_id` is the join key between the user's file and the beads
epic ([ADR 0002](decisions/0002-link-issues-via-bead-metadata.md)). Nothing else
links them, and nothing else needs to.

## Where state lives

| State                              | Home                                    | Authoritative? |
| ---------------------------------- | --------------------------------------- | -------------- |
| The work: what to do, what is done | beads                                   | yes            |
| The code                           | git, on a `milhouse/<slug>` branch      | yes            |
| Which workspace and pane           | `.milhouse/runs/<slug>/state.json`      | bookkeeping    |
| Attempts per issue                 | `.milhouse/runs/<slug>/state.json`      | bookkeeping    |
| Iteration history                  | `.milhouse/runs/<slug>/state.json`      | bookkeeping    |
| Exact prompt sent, pane transcript | `.milhouse/runs/<slug>/iter-NNN.*`      | bookkeeping    |

Everything under `.milhouse/runs/` is gitignored and safe to delete. Doing so
loses the history and the attempt counts, nothing else. See
[troubleshooting](troubleshooting.md) for the layout.

## Testing

Three tiers, because there is no headless path to an interactive agent:

1. **Unit tests with fakes.** `tests/fakes.py` fakes at the `proc.py` boundary,
   replaying `bd` and `herdr` JSON recorded from the real tools. This covers the
   loop logic, classification, config, sources, and prompt rendering. `uv run pytest`.
2. **Live-tool tests**, marked `herdr` and `beads`. These drive the real herdr
   server with plain shell commands (no agents) and a real scratch `bd`
   database. `uv run pytest -m herdr` / `-m beads`.
3. **A manual end-to-end run.** Documented in [usage](usage.md#end-to-end-check).
   It needs eyes on it: confirm the pane returns to a shell prompt between
   iterations, which is what proves the context is actually fresh.
