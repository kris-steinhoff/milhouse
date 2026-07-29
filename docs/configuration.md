# Configuration

milhouse resolves its settings from four layers. Later layers win **key by key**, so a config file that sets only `[agent] kind` keeps every other default:

1. Built-in defaults (`src/milhouse/config.py`)
2. `.milhouse/config.toml` in the repository root — committed
3. Environment variables
4. Command-line flags

An unset flag is not the same as an explicit one: `None` overrides are dropped before merging, so omitting `--agent` leaves whatever the config file says.

`.milhouse/config.toml` is optional. Without it, the defaults below apply.

## Example

```toml
# .milhouse/config.toml
[agent]
kind = "claude"
args = ["--permission-mode", "acceptEdits"]

[run]
max_iterations = 50
max_attempts = 3

[verify]
command = ["uv", "run", "pytest", "-m", "not herdr and not beads"]

[tracker]
label = "agent"
```

## `[agent]`

How the interactive agent is started in its herdr pane. See [ADR 0003](decisions/0003-agents-run-in-herdr-panes.md).

| Key | Type | Default | Environment | Meaning |
| --- | --- | --- | --- | --- |
| `kind` | string | `"claude"` | `MILHOUSE_AGENT_KIND` | `herdr agent start --kind` value. Any kind herdr supports (`claude`, `codex`, `gemini`, `amp`, `opencode`, …). |
| `args` | list\[str] | `[]` | `MILHOUSE_AGENT_ARGS` (shell-split) | Extra arguments passed to the agent binary after `--`. See [ADR 0009](decisions/0009-permission-posture.md). |
| `start_timeout_ms` | int | `60000` | `MILHOUSE_AGENT_START_TIMEOUT_MS` | How long `herdr agent start` may take to report the agent ready. |
| `exit_timeout_ms` | int | `8000` | `MILHOUSE_AGENT_EXIT_TIMEOUT_MS` | How long to wait for the pane to return to a shell prompt before replacing it. |
| `exit_keys` | list\[str] | `["ctrl+c", "ctrl+c", "ctrl+d"]` | — | Key sequence returning the pane from the agent TUI to a shell prompt. Use the `ctrl+` spelling. See [ADR 0011](decisions/0011-exiting-the-agent.md). |
| `turn_timeout_ms` | int | `1800000` (30 minutes) | `MILHOUSE_TURN_TIMEOUT_MS` | Bound on one `herdr agent prompt --wait` turn, so a wedged agent cannot hang a step forever. |

`--agent` on the CLI overrides `agent.kind`.

## `[run]`

What bounds one `milhouse run`. Nothing here affects `step`, `dispatch`, or `reap`, which take one turn and hand back. See [ADR 0022](decisions/0022-the-loop-is-earned.md).

| Key              | Type | Default | Environment                   | Meaning                                                                       |
| ---------------- | ---- | ------- | ----------------------------- | ----------------------------------------------------------------------------- |
| `max_iterations` | int  | `50`    | `MILHOUSE_RUN_MAX_ITERATIONS` | Turns one run may take before it stops and reports.                           |
| `max_attempts`   | int  | `3`     | `MILHOUSE_RUN_MAX_ATTEMPTS`   | Attempts one issue gets before the run defers it and works on something else. |

`--max-iterations` and `--max-attempts` override these. Both must be at least 1: zero is rejected rather than silently meaning a run that stops at the ceiling having done nothing, which reports a stop reason that sounds like progress.

`max_iterations` bounds turns, not spend, and turns are not the same size. It is the only thing bounding an overnight run ([ADR 0012](decisions/0012-no-cost-controls-in-v1.md)).

`max_attempts` is counted over the whole audit history rather than over one run, so re-running a target does not hand a hopeless issue three more turns. An issue that uses them up is deferred with the reason on it: still unfinished, still listed, no longer offered as ready. `bd undefer <id>` is how it comes back, and the run's report names everything it set aside.

The old `[loop]` section is not this one under a new name, and the name is not reused so an old config file cannot silently start meaning something new. `on_blocked` and `blocked_timeout_ms` are gone for good: a blocked agent stops the run, because nobody is there to approve and every later turn would meet the same prompt. `turn_timeout_ms` bounds one agent turn regardless of what drives it, and stayed in `[agent]`.

## `[verify]`

How milhouse checks an issue the agent says it finished. See [ADR 0016](decisions/0016-milhouse-verifies.md).

| Key          | Type       | Default               | Environment                             | Meaning                                                                  |
| ------------ | ---------- | --------------------- | --------------------------------------- | ------------------------------------------------------------------------ |
| `command`    | list\[str] | `[]`                  | `MILHOUSE_VERIFY_COMMAND` (shell-split) | Run in the lane after an iteration closes its issue. Empty runs nothing. |
| `timeout_ms` | int        | `600000` (10 minutes) | `MILHOUSE_VERIFY_TIMEOUT_MS`            | How long it may take before it counts as failed.                         |

A non-zero exit re-opens the issue with the outcome `rejected` and appends the tail of the output as a `bd` note, so the next agent sees why the last one's work was turned down.

Empty by default, so out of the box milhouse takes the agent at its word. There is no safe guess about a given repository's gate, and a wrong one fails every iteration. Point it at the fast suite rather than the full matrix: it runs once per closed issue.

No shell is involved, so this is argv rather than a command line. `MILHOUSE_VERIFY_COMMAND` is split with `shlex`, so quoting works the way it does in a shell.

There is no `[git]` section. `branch_strategy` and `branch_prefix` named a branch after the task definition, and there is no task definition ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md)). Where commits land is `[lane]`'s answer now.

## `[lane]`

Where an issue's agent works. See [ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md).

| Key             | Type   | Default       | Environment                   | Meaning                                             |
| --------------- | ------ | ------------- | ----------------------------- | --------------------------------------------------- |
| `branch_prefix` | string | `"milhouse/"` | `MILHOUSE_LANE_BRANCH_PREFIX` | Prefix for a lane's branch, e.g. `milhouse/bd-e.1`. |

A lane is a herdr worktree labelled with the issue id, and herdr chooses where it goes — under `~/.herdr/worktrees/<repo>/<branch>` — so there is no key saying where lanes live. Nothing here turns lanes off: every turn happens in one.

## `[tracker]`

Which issues milhouse is allowed to work. See [ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md).

| Key      | Type   | Default | Environment               | Meaning                                                      |
| -------- | ------ | ------- | ------------------------- | ------------------------------------------------------------ |
| `label`  | string | unset   | `MILHOUSE_TRACKER_LABEL`  | Only work issues carrying this label. Unset considers all.   |
| `parent` | string | unset   | `MILHOUSE_TRACKER_PARENT` | Only work issues under this epic. Unset considers every one. |

`--label` and `--parent` override these.

Unfenced by default, because a repository whose beads database is only agent work needs no fence. Set one where the ready queue also carries issues that were never meant for an agent — milhouse will otherwise claim them, since it has no way of telling.

Both are passed straight to `bd ready`, which already excludes blocked, deferred, and in-progress issues. milhouse adds `--exclude-type epic` on top: an epic is a container for work, not a unit of it.

## `[herdr]`

Workspace and transcript settings. See [ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md).

| Key           | Type   | Default     | Environment                                | Meaning                                                                                  |
| ------------- | ------ | ----------- | ------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `workspace`   | string | unset       | `MILHOUSE_WORKSPACE`, `HERDR_WORKSPACE_ID` | Reuse this workspace instead of creating one. Unset creates (and owns) one.              |
| `self_pane`   | string | unset       | `HERDR_PANE_ID`                            | The pane milhouse is running in, which it will never start an agent in.                  |
| `read_lines`  | int    | `400`       | —                                          | Lines of pane transcript captured after each turn into `<issue-id>/iter-NNN.term`.       |
| `read_source` | enum   | `"visible"` | —                                          | `herdr agent read --source` value: `visible`, `recent`, `recent-unwrapped`, `detection`. |

`HERDR_WORKSPACE_ID` is exported by herdr into every pane it launches, so milhouse running inside a pane reuses that workspace by default. `MILHOUSE_WORKSPACE` takes precedence over it, and `--workspace` over both.

A reused workspace is not an empty one, which is why `self_pane` exists. Its panes belong to somebody — very often to the terminal `milhouse step` was just typed into, since that pane is the reason `HERDR_WORKSPACE_ID` is set at all. milhouse therefore picks a pane rather than taking the first one: it skips `self_pane`, skips any pane already running an agent, and splits a new pane when nothing is free. `HERDR_PANE_ID` is set by herdr for you, so this is not a key you should need to write down.

`read_source` defaults to `visible` rather than `recent` because `recent` returns only output since the previous read, which is empty when nothing has been read before — a surprising transcript to find in a post-mortem.

## Environment variables at a glance

| Variable                          | Sets                     |
| --------------------------------- | ------------------------ |
| `MILHOUSE_AGENT_KIND`             | `agent.kind`             |
| `MILHOUSE_AGENT_ARGS`             | `agent.args`             |
| `MILHOUSE_AGENT_START_TIMEOUT_MS` | `agent.start_timeout_ms` |
| `MILHOUSE_AGENT_EXIT_TIMEOUT_MS`  | `agent.exit_timeout_ms`  |
| `MILHOUSE_TURN_TIMEOUT_MS`        | `agent.turn_timeout_ms`  |
| `MILHOUSE_RUN_MAX_ITERATIONS`     | `run.max_iterations`     |
| `MILHOUSE_RUN_MAX_ATTEMPTS`       | `run.max_attempts`       |
| `MILHOUSE_VERIFY_COMMAND`         | `verify.command`         |
| `MILHOUSE_VERIFY_TIMEOUT_MS`      | `verify.timeout_ms`      |
| `MILHOUSE_LANE_BRANCH_PREFIX`     | `lane.branch_prefix`     |
| `MILHOUSE_TRACKER_LABEL`          | `tracker.label`          |
| `MILHOUSE_TRACKER_PARENT`         | `tracker.parent`         |
| `MILHOUSE_WORKSPACE`              | `herdr.workspace`        |
| `HERDR_WORKSPACE_ID`              | `herdr.workspace`        |
| `HERDR_PANE_ID`                   | `herdr.self_pane`        |

Variables holding an integer must parse as one, or milhouse exits with `ConfigError` (exit code 2). `MILHOUSE_AGENT_ARGS` and `MILHOUSE_VERIFY_COMMAND` are split with `shlex`, so quoting works the way it does in a shell.

## What is not configurable

- Prompt templates. They ship in the package and are versioned with the code, so a run is reproducible from a milhouse version. See [prompts](prompts.md).
- The `.milhouse/runs/` layout. See [troubleshooting](troubleshooting.md).
- Token or cost caps. milhouse cannot observe spend through a herdr pane; see [ADR 0012](decisions/0012-no-cost-controls-in-v1.md).
