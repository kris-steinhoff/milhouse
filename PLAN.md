# milhouse — Design

## Context

`milhouse` is an agentic AI orchestrator. It takes a task definition, ensures the task has been decomposed into tracked issues, then drives a [ralph-loop](https://ghuntley.com/ralph/) over those issues until the work is done.

The pieces already exist as separate tools. What is missing is the thing that wires them together and keeps the loop running unattended:

- **[beads](https://github.com/steveyegge/beads) (`bd`)** — git-backed, dependency-aware issue tracker built for agents. Gives us durable memory and, critically, `bd ready --claim` for race-free "what do I work on next".
- **[herdr](https://herdr.dev)** — terminal workspace manager for AI coding agents. Every agent runs in a herdr pane. herdr owns agent startup, readiness, and lifecycle state, and gives a human a live pane to watch and intervene in. Driven through its CLI, with a [socket API](https://herdr.dev/docs/socket-api/) held in reserve.
- **`claude`** — the interactive agent that does one unit of work per iteration.

Scope is a locally installable tool (`uv tool install`), not a PyPI release.

The design decisions below are settled against the real tool surfaces. The [open questions](#open-questions) are not, and several of them block implementation rather than merely refining it.

### Current state

The repo contains only `LICENSE` and `README.md`. Everything below is new.

Locally available: `uv` 0.11.28, `herdr` 0.7.5 (server running), `gh`, `claude`. **`bd` is not installed** and is a hard dependency, as is `herdr`. See [Prerequisites](#prerequisites).

## Core loop

```
milhouse run <task_definition>
  │
  ├─ resolve source ────────► TaskDefinition (title, body, task_id)
  │                            file:docs/feature-x.md  |  gh:owner/repo#123
  │
  ├─ already decomposed? ───► bd list --metadata-field milhouse_task=<id> -t epic --json
  │     │
  │     └─ no ─────────────► run PLANNING agent (one shot)
  │                           creates epic + child issues + deps
  │
  └─ ralph loop ───────────► repeat:
        bd ready --parent <epic> --claim --json   → issue (empty ⇒ done)
        render iterate prompt for that issue
        herdr agent start  (FRESH agent in the task's pane)
        herdr agent prompt --wait --until idle --until blocked
        classify outcome from beads + git, then exit the agent
```

The defining property of ralph is a **fresh context window every iteration**. We get that by starting a new agent in the pane each iteration and exiting it when the turn ends, rather than reusing one long-lived session. State lives in beads and git, never in an accumulating chat session.

## Design decisions

### 1. Shell out to `bd` and `herdr`, parse JSON

Both tools emit JSON (`bd --json`, and `herdr`'s api/agent/pane/workspace subcommands do so by default). Every subprocess call goes through one audited helper in `proc.py` so it can be faked in tests.

herdr also has a socket API, and it is genuinely good: newline-delimited JSON over `$HERDR_SOCKET_PATH`, 89 methods, a versioned protocol (17), and a machine-readable contract via `herdr api schema --json`. There is no official SDK in any language, but the protocol is small enough that a client is one file. **We are not using it yet.** The CLI is one dependency instead of two (transport plus schema), it survives herdr protocol bumps without a pinned version, and it keeps the surface area small while the interesting problems are elsewhere.

What that costs us, stated plainly so the trade is revisitable:

- **No `events.subscribe`.** The socket can push `pane.agent_status_changed`; the CLI cannot. For a sequential v1 this costs nothing, because `herdr agent prompt --wait --until idle --until blocked` already blocks until the turn settles. It starts to bite at concurrency, where the CLI needs one blocking `herdr agent wait` process per pane against the socket's single connection watching all of them. See [Open questions](#open-questions).
- **No event log** for post-mortems on a run that went sideways.
- **Coarser errors.** Parsing stderr rather than a structured `error.code`.

`herdr.py` stays a narrow client so swapping the transport later is one file, not a refactor. If concurrency lands, that is the moment to reconsider.

### 2. Link issues to task definitions via bead metadata

Each task definition gets a stable `task_id`:

| Source       | `task_id`                    |
| ------------ | ---------------------------- |
| Local file   | `file:<repo-relative-path>`  |
| GitHub issue | `gh:<owner>/<repo>#<number>` |

The planning agent creates one **epic** carrying `--metadata '{"milhouse_task":"<task_id>"}'` plus `--labels milhouse`, and children under `--parent <epic-id>`. (Note the asymmetry: `bd create` takes `--metadata` with a JSON blob, while `--set-metadata key=value` exists only on `bd update`.) This buys us three things for free from `bd`:

- **Decomposed?** `bd list --metadata-field milhouse_task=<id> --type epic --json`
- **Next issue?** `bd ready --parent <epic-id> --claim --json --limit 1`
- **Done?** the same `bd ready` returns empty

GitHub-sourced tasks also set `--external-ref gh-<number>` so beads can round-trip the link.

### 3. Every agent runs in herdr, restarted per iteration

One workspace per task, one pane, one fresh agent per iteration:

```
once per run:      herdr workspace create --cwd <repo> --label "milhouse:<slug>" --no-focus
                     → workspace_id, pane_id  (pane sits at a shell prompt)

per iteration:     herdr agent start milhouse-<slug> --kind claude --pane <pane_id> \
                       --timeout 60000 -- <agent args>
                   herdr agent prompt milhouse-<slug> "<rendered prompt>" \
                       --wait --until idle --until blocked --timeout <ms>
                   herdr agent read milhouse-<slug> --source recent --lines 400 --format text
                   herdr agent send-keys milhouse-<slug> ctrl-c ctrl-d   # back to shell prompt
```

`herdr agent start` requires the pane to be at an interactive shell prompt and returns only once the agent is detected and ready, so startup is a synchronous, checkable step rather than a sleep. Exiting the agent at the end of each iteration returns the pane to that state for the next one.

This uses herdr's native lifecycle detection instead of scraping terminal output or polling sentinel files. Because the agent is freshly started and therefore `idle`, the `idle → working → idle` transition around a prompt is unambiguous. (`agent prompt --wait` warns that it cannot distinguish turns if the agent is _already_ working, which does not apply here.)

`--until idle --until blocked` is the whole turn-completion mechanism: one blocking subprocess per iteration, which is exactly what a sequential loop wants.

The `--kind` enum already covers codex, amp, opencode, gemini and others, so supporting a second agent backend later is a config change, not new code.

### 4. Iteration outcome comes from beads and git, not a process exit code

There is no exit status to read from an interactive agent, and that is fine. Process exit codes were never a good success signal anyway. After each turn:

| Signal                       | Outcome                                   |
| ---------------------------- | ----------------------------------------- |
| Issue is closed in beads     | **success**                               |
| Agent settled `blocked`      | **needs human** — see below               |
| Issue open, `git HEAD` moved | **partial** — counts as an attempt, retry |
| Issue open, `HEAD` unchanged | **stalled** — counts as an attempt, retry |
| `agent prompt` timed out     | **timeout** — counts as an attempt, retry |

`herdr` reports a distinct `blocked` state, which an interactive agent enters when it is waiting on a human (most often a permission prompt). This is the main payoff of running in panes rather than headless: milhouse can stop, tell the user which workspace to attach to, and wait for them to unblock it, instead of failing the iteration. Behavior is set by `--on-blocked {wait,skip,abort}`, default `wait` with a timeout.

### 5. milhouse owns the loop, the agent owns one step

The iteration prompt is deliberately narrow: here is one issue, do it, verify it, commit it, close it. The agent does not pick its own work and does not decide when the run is over. That stays with milhouse so the guardrails below actually bind.

## Package layout

```
pyproject.toml               # uv/hatchling, requires-python >=3.11, script: milhouse
README.md                    # what it is, install, quickstart
CHANGELOG.md                 # keep-a-changelog, updated per user-visible change
docs/
  usage.md                   # every command and flag, worked examples
  configuration.md           # .milhouse/config.toml reference
  architecture.md            # the loop, the module boundaries, the data flow
  prompts.md                 # what each template promises the agent, and why
  troubleshooting.md         # blocked agents, stale claims, doctor failures
  decisions/                 # one ADR per settled decision, including this file's
src/milhouse/
  cli.py                     # typer app — run, plan, status, doctor
  config.py                  # layered: defaults < .milhouse/config.toml < env < flags
  models.py                  # TaskDefinition, Issue, Iteration, RunState (pydantic)
  proc.py                    # run_json() / run() — the single subprocess chokepoint
  errors.py                  # MilhouseError hierarchy, mapped to exit codes
  sources/
    base.py                  # Source protocol; resolve(spec) -> TaskDefinition
    file.py                  # local markdown
    github.py                # gh issue view <n> --json title,body,number,url
  tracker/
    base.py                  # Tracker protocol (create_epic, ready_claim, close, ...)
    beads.py                 # bd wrapper
  herdr.py                   # narrow client over the herdr CLI — swappable transport
  runner.py                  # AgentRunner — start/prompt/read/exit one iteration
  outcome.py                 # classify(issue_before, issue_after, git, agent_state)
  loop.py                    # RalphLoop — guardrails, state, signal handling
  prompts/
    plan.md.j2               # decomposition prompt
    iterate.md.j2            # per-issue prompt
tests/
  fakes.py                   # FakeTracker, FakeRunner, recorded bd/herdr JSON
  test_*.py
```

Dependencies: `typer`, `pydantic`, `jinja2`. Dev: `pytest`, `ruff`, `pyright`. `tomllib` is stdlib on 3.11+, so no TOML dependency.

Tests fake at the `proc.py` boundary, replaying recorded `bd` and `herdr` JSON captured from the real tools.

## Commands

| Command                  | Behavior                                                                                                                                  |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `milhouse run <task>`    | Resolve, decompose if needed, then loop. The main entry point.                                                                            |
| `milhouse plan <task>`   | Decompose only. Prints the resulting issue tree and stops.                                                                                |
| `milhouse status <task>` | Issue tree plus this run's iteration history.                                                                                             |
| `milhouse doctor`        | Verify `bd`, `herdr`, `gh`, `claude`, report versions, and confirm via `herdr status` that the server is running and protocol-compatible. |

Key flags on `run`:

- `--max-iterations N` (default 50) — hard ceiling on the whole run
- `--max-attempts N` (default 3) — per-issue retry cap before escalating
- `--on-blocked {wait,skip,abort}` (default `wait`) — what to do when herdr reports the agent is waiting on a human
- `--agent claude` — any `herdr agent start --kind` value
- `--workspace <id>` — reuse an existing herdr workspace instead of creating one (defaults to `HERDR_WORKSPACE_ID` when milhouse is itself running in a pane)
- `--dry-run` — render prompts and print the plan, start no agents
- `--attach` — focus the herdr workspace after starting instead of `--no-focus`

## Guardrails

Unattended loops fail in boring, expensive ways. The loop enforces:

- **Iteration ceiling.** Stop at `--max-iterations`, report cleanly.
- **Per-issue attempt cap.** Three failed attempts on one issue marks it `blocked` with a note, then moves on rather than spinning.
- **Stall detection.** An iteration that produces no new git commit _and_ leaves the issue open counts as a failed attempt.
- **Turn timeout.** `herdr agent prompt --wait --timeout` bounds a single turn so a wedged agent cannot hang the run forever.
- **Clean teardown.** SIGINT/SIGTERM reverts the in-flight claim (`bd update <id> --status open --assignee ""`), exits the agent, and leaves the workspace open for inspection. Panes are never closed out from under a human.
- **Decomposition confirmation.** The planning agent's proposed issues are shown for approval before creation unless `--yes` is passed.

## Run state

```
.milhouse/
  config.toml                  # committed — agent command, defaults
  runs/<task_slug>/
    state.json                 # workspace/pane id, epic id, per-issue attempts
    iter-007.prompt            # exact prompt sent
    iter-007.term              # herdr agent read transcript after the turn
```

Without an event stream, `iter-NNN.term` is the primary post-mortem artifact, so capture it after every turn including failed ones.

`.milhouse/runs/` is gitignored. Beads and git remain the source of truth for the work itself. Everything under `runs/` is loop bookkeeping and is safe to delete.

## Documentation

Everything is documented. This is a build requirement, not a cleanup pass at the end: a build-order step is not finished until the docs that describe it exist and are accurate.

| Surface          | Requirement                                                                                                          |
| ---------------- | -------------------------------------------------------------------------------------------------------------------- |
| `README.md`      | What milhouse is, prerequisites, install, a 60-second quickstart, and links into `docs/`.                            |
| `docs/`          | The files in [package layout](#package-layout). Prose, not generated dumps.                                          |
| CLI help         | Every command and every flag carries a `help=` string. `milhouse --help` is a usable reference on its own.           |
| Docstrings       | Module, class, and public function docstrings everywhere, saying what and why. Enforced by ruff's `D` rules.         |
| Prompt templates | Each `.j2` opens with a comment block stating the contract it imposes on the agent and the variables it expects.     |
| Config           | Every key in `.milhouse/config.toml` documented in `docs/configuration.md` with its type, default, and env override. |
| Errors           | Every `MilhouseError` subclass documents its exit code and what a user should do about it.                           |
| Decisions        | One ADR per settled decision under `docs/decisions/`, including the numbered decisions above.                        |
| Open questions   | Each resolved [open question](#open-questions) becomes an ADR. The answer never lives only in a commit message.      |
| `CHANGELOG.md`   | Keep-a-changelog format, one entry per user-visible change.                                                          |
| Run artifacts    | `.milhouse/runs/` layout documented in `docs/troubleshooting.md`, since it is the primary post-mortem surface.       |

Two rules make this stick:

- **Docs ship in the same commit as the code they describe.** A behavior change with no doc change is an incomplete change.
- **Examples are real.** Every command shown in the docs has been run, and its output pasted rather than invented.

The same expectation is passed down the loop: `iterate.md.j2` tells the agent that an issue is not done until the docs covering it are updated in the same commit. That makes documentation part of the per-issue contract in [decision 5](#5-milhouse-owns-the-loop-the-agent-owns-one-step), and part of what [open question 1](#open-questions) has to pin down.

## Build order

1. **Bootstrap** — `pyproject.toml`, package skeleton, `milhouse doctor`, ruff + pytest config (including the `D` docstring rules), `.gitignore`, the `docs/` skeleton, `CHANGELOG.md`. Verify `uv tool install --editable .` works.
2. **Sources** — `TaskDefinition`, file and GitHub resolvers, `task_id` derivation.
3. **Tracker** — `proc.py` chokepoint, then the `bd` wrapper against a scratch `bd init` database.
4. **herdr client** — `herdr.py` against the live server: create a workspace, split a pane, run a command, read it back, close it. No agents yet.
5. **Runner** — `agent start` / `prompt --wait` / `read` / exit cycle, plus `outcome.py` classification.
6. **Prompts** — `plan.md.j2` and `iterate.md.j2`. Expect these to be tuned by observation, per the ralph methodology.
7. **Loop** — wire it together, add guardrails, signal handling, state persistence.
8. **Dogfood** — point `milhouse run` at a task definition in this repo.
9. **Docs pass** — fill out `docs/` from the finished behavior, replace every example with real captured output, and write the ADRs for whichever [open questions](#open-questions) got answered along the way.

Steps 1 through 5 are mechanical. Step 6 is where the real work is. Step 9 is a final sweep, not permission to defer documentation until then: each step lands with its own docs per the [documentation](#documentation) requirement.

## Verification

```sh
# bootstrap
uv sync && uv run milhouse doctor        # all four tools green
uv tool install --editable .             # milhouse on PATH

# unit tests — recorded bd/herdr JSON, no agent spawned
uv run pytest
uv run ruff check && uv run ruff format --check   # includes docstring (D) rules

# docs
uv run milhouse --help                   # every command and flag has help text
grep -r '](' docs README.md              # spot-check that no link is dangling

# herdr client against the live server, still no agent
uv run pytest -m herdr

# end to end, cheapest first
milhouse run docs/tasks/hello.md --dry-run
milhouse plan docs/tasks/hello.md        # inspect the issue tree
bd list --metadata-field milhouse_task=file:docs/tasks/hello.md --json
milhouse run docs/tasks/hello.md --max-iterations 2 --attach
```

Because there is no headless path, the test pyramid splits three ways: fakes for loop logic, a `herdr`-marked suite that drives a real workspace with plain shell commands instead of agents, and a manual end-to-end run.

The end-to-end run needs eyes on it. Confirm a workspace named `milhouse:hello` appears, the pane shows claude starting and working, the pane returns to a shell prompt between iterations (proving fresh context), and the issue closes in `bd`. Then deliberately trigger a permission prompt to confirm herdr reports `blocked` and milhouse waits rather than counting a failure.

## Prerequisites

`herdr` 0.7.5 is installed and its server is running. `bd` is not installed on this machine and everything depends on it:

```sh
brew install beads
# or
curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash
```

Then `bd init` in this repo before step 3.

## Open questions

Roughly ordered by how much they block implementation. The first four are load-bearing: the design does not fully specify what to build until they are answered.

1. **What goes in the iteration prompt.** `iterate.md.j2` is currently a filename. For a ralph loop the prompt _is_ the product, and the methodology is explicitly to tune it by observation. Unanswered: what contract the prompt imposes on the agent (must close the issue, must commit, must not pick up new work), whether to inject `bd prime` output or lean on the `AGENTS.md` that `bd init` writes, how much of the task definition to include alongside the issue, and how much repo convention to restate versus trust `CLAUDE.md` for.
2. **Git branching strategy.** Entirely unaddressed, and [decision 4](#4-iteration-outcome-comes-from-beads-and-git-not-a-process-exit-code) leans on "`HEAD` moved" without saying which branch moved. Fifty unattended iterations committing to `main` is not viable. Branch per task, branch per issue, or worktree per run? This interacts with question 5, since a worktree answers both at once.
3. **How the planning agent hands issues back.** The [approval guardrail](#guardrails) promises proposed issues are shown before creation, but the planning agent has `bd` and will simply create them. Making the guardrail real means having the agent emit a plan that milhouse creates. `bd create --graph <plan.json>` and `bd create --dry-run` exist for exactly this. Also undefined: how milhouse decides the planning agent succeeded, and what it does when the agent produces a bad decomposition.
4. **Crash recovery and stale claims.** [Teardown](#guardrails) covers SIGINT/SIGTERM, but `SIGKILL`, a lost SSH session, or a closed laptop leaves an issue `in_progress` and assigned forever. `bd` has no lease expiry, so milhouse needs its own reaper or a startup reconciliation pass. Related: re-running `milhouse run` against an in-flight task has undefined resume semantics.
5. **Permission posture.** An interactive agent that hits a permission prompt stops and waits. herdr surfaces that as `blocked`, so a supervised run is genuinely fine and a human can attach and approve. A long unattended run still needs `--dangerously-skip-permissions` passed through `herdr agent start ... -- <args>`, which argues for a container or a throwaway worktree. `herdr worktree create` makes the worktree option cheap. Decide before the first overnight run.
6. **Cost controls.** `--max-iterations` bounds turns, not spend, and the ralph writeups cite figures like $600 for an overnight run. Whether milhouse should track or cap token spend, and how it would even observe it through a herdr pane, is unanswered.
7. **`.milhouse/config.toml` schema.** Referenced in [package layout](#package-layout) and [run state](#run-state), never defined. At minimum it holds the agent kind and its args, the default caps, and the turn timeout.
8. **Exiting the agent cleanly.** `ctrl-c ctrl-d` via `herdr agent send-keys` should drop claude back to the shell prompt, but this needs verifying against the real TUI in step 5. Fallback is `herdr pane close` plus a fresh `herdr pane split`, which costs a pane churn per iteration but is unambiguous.
9. **Re-planning.** If an iteration discovers new work, the agent can `bd create --parent <epic>` mid-run. Currently allowed and untracked. May want a cap so the run cannot grow without bound.
10. **Concurrency.** `bd ready --claim` is already race-free, so N parallel loops over one epic is mostly a matter of one worktree and one herdr pane each, which `herdr worktree create` plus `herdr tab create` covers. The awkward part is waiting on N panes: over the CLI that means a blocking `herdr agent wait` process per pane, where the socket's `events.subscribe` would watch all of them from one connection. **This is the trigger for revisiting decision 1.** Not in scope for v1.
11. **Agent portability.** [Decision 3](#3-every-agent-runs-in-herdr-restarted-per-iteration) claims a second agent backend is "a config change, not new code". That is optimistic: the exit key sequence is claude-specific, and the prompts likely are too. Worth testing against one other `--kind` before believing it.
