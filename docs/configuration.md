# Configuration

milhouse resolves its settings from four layers. Later layers win **key by key**, so a config file that sets only `[loop] max_iterations` keeps every other default:

1. Built-in defaults (`src/milhouse/config.py`)
2. `.milhouse/config.toml` in the repository root — committed
3. Environment variables
4. Command-line flags

An unset flag is not the same as an explicit one: `None` overrides are dropped before merging, so omitting `--max-iterations` leaves whatever the config file says.

`.milhouse/config.toml` is optional. Without it, the defaults below apply.

## Example

```toml
# .milhouse/config.toml
[agent]
kind = "claude"
args = ["--permission-mode", "acceptEdits"]

[loop]
max_iterations = 30

[verify]
command = ["uv", "run", "pytest", "-m", "not herdr and not beads"]

[git]
branch_strategy = "task"
branch_prefix = "milhouse/"
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

`--agent` on the CLI overrides `agent.kind`.

## `[loop]`

What bounds a run. Small on purpose: the supervised policy stops at the first iteration that does not succeed, so nothing here decides when to retry ([ADR 0014](decisions/0014-step-is-the-primitive.md)).

| Key               | Type | Default                | Environment                | Meaning                                        |
| ----------------- | ---- | ---------------------- | -------------------------- | ---------------------------------------------- |
| `max_iterations`  | int  | `50`                   | `MILHOUSE_MAX_ITERATIONS`  | Iterations one `milhouse run` may take.        |
| `turn_timeout_ms` | int  | `1800000` (30 minutes) | `MILHOUSE_TURN_TIMEOUT_MS` | Bound on one `herdr agent prompt --wait` turn. |

`--max-iterations` overrides the first, and counts **this invocation**: re-running picks up where the last one left off and gets the budget again. Iteration numbers keep counting across invocations, because they name `iter-NNN.prompt`.

`max_attempts`, `on_blocked`, and `blocked_timeout_ms` were removed. They only ever answered questions an unattended run asks, and they come back with the ralph policy.

## `[verify]`

How milhouse checks an issue the agent says it finished. See [ADR 0016](decisions/0016-milhouse-verifies.md).

| Key          | Type       | Default               | Environment                             | Meaning                                                                       |
| ------------ | ---------- | --------------------- | --------------------------------------- | ----------------------------------------------------------------------------- |
| `command`    | list\[str] | `[]`                  | `MILHOUSE_VERIFY_COMMAND` (shell-split) | Run in the repo root after an iteration closes its issue. Empty runs nothing. |
| `timeout_ms` | int        | `600000` (10 minutes) | `MILHOUSE_VERIFY_TIMEOUT_MS`            | How long it may take before it counts as failed.                              |

A non-zero exit re-opens the issue with the outcome `rejected` and appends the tail of the output as a `bd` note, so the next agent sees why the last one's work was turned down.

Empty by default, so out of the box milhouse takes the agent at its word. There is no safe guess about a given repository's gate, and a wrong one fails every iteration. Point it at the fast suite rather than the full matrix: it runs once per closed issue.

No shell is involved, so this is argv rather than a command line. `MILHOUSE_VERIFY_COMMAND` is split with `shlex`, so quoting works the way it does in a shell.

## `[git]`

Where the commits land. See [ADR 0007](decisions/0007-branch-per-task.md).

| Key               | Type   | Default       | Environment                | Meaning                                                                       |
| ----------------- | ------ | ------------- | -------------------------- | ----------------------------------------------------------------------------- |
| `branch_strategy` | enum   | `"task"`      | `MILHOUSE_BRANCH_STRATEGY` | `task` puts the run on one branch per task definition; `current` stays put.   |
| `branch_prefix`   | string | `"milhouse/"` | `MILHOUSE_BRANCH_PREFIX`   | Prefix for branches created under the `task` strategy, e.g. `milhouse/hello`. |

`--branch-strategy` and `--branch` override these.

## `[tracker]`

How milhouse marks its own issues in beads. See [ADR 0002](decisions/0002-link-issues-via-bead-metadata.md). Changing either key orphans issues created under the old value, so set them once, at the start.

| Key            | Type   | Default           | Environment | Meaning                                                  |
| -------------- | ------ | ----------------- | ----------- | -------------------------------------------------------- |
| `label`        | string | `"milhouse"`      | —           | Label applied to every issue milhouse creates.           |
| `metadata_key` | string | `"milhouse_task"` | —           | Bead metadata key holding the task id that owns an epic. |

## `[herdr]`

Workspace and transcript settings. See [ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md).

| Key           | Type   | Default     | Environment                                | Meaning                                                                                  |
| ------------- | ------ | ----------- | ------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `workspace`   | string | unset       | `MILHOUSE_WORKSPACE`, `HERDR_WORKSPACE_ID` | Reuse this workspace instead of creating one. Unset creates (and owns) one.              |
| `read_lines`  | int    | `400`       | —                                          | Lines of pane transcript captured after each turn into `iter-NNN.term`.                  |
| `read_source` | enum   | `"visible"` | —                                          | `herdr agent read --source` value: `visible`, `recent`, `recent-unwrapped`, `detection`. |

`HERDR_WORKSPACE_ID` is exported by herdr into every pane it launches, so milhouse running inside a pane reuses that workspace by default. `MILHOUSE_WORKSPACE` takes precedence over it, and `--workspace` over both.

`read_source` defaults to `visible` rather than `recent` because `recent` returns only output since the previous read, which is empty when nothing has been read before — a surprising transcript to find in a post-mortem.

## Environment variables at a glance

| Variable                          | Sets                     |
| --------------------------------- | ------------------------ |
| `MILHOUSE_AGENT_KIND`             | `agent.kind`             |
| `MILHOUSE_AGENT_ARGS`             | `agent.args`             |
| `MILHOUSE_AGENT_START_TIMEOUT_MS` | `agent.start_timeout_ms` |
| `MILHOUSE_AGENT_EXIT_TIMEOUT_MS`  | `agent.exit_timeout_ms`  |
| `MILHOUSE_MAX_ITERATIONS`         | `loop.max_iterations`    |
| `MILHOUSE_TURN_TIMEOUT_MS`        | `loop.turn_timeout_ms`   |
| `MILHOUSE_BRANCH_STRATEGY`        | `git.branch_strategy`    |
| `MILHOUSE_BRANCH_PREFIX`          | `git.branch_prefix`      |
| `MILHOUSE_WORKSPACE`              | `herdr.workspace`        |
| `HERDR_WORKSPACE_ID`              | `herdr.workspace`        |

Variables holding an integer must parse as one, or milhouse exits with `ConfigError` (exit code 2). `MILHOUSE_AGENT_ARGS` and `MILHOUSE_VERIFY_COMMAND` are split with `shlex`, so quoting works the way it does in a shell.

## What is not configurable

- Prompt templates. They ship in the package and are versioned with the code, so a run is reproducible from a milhouse version. See [prompts](prompts.md).
- The `.milhouse/runs/` layout. See [troubleshooting](troubleshooting.md).
- Token or cost caps. milhouse cannot observe spend through a herdr pane; see [ADR 0012](decisions/0012-no-cost-controls-in-v1.md).
