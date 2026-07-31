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
max_parallel = 1     # --count N overrides this

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
| `submit_timeout_ms` | int | `15000` | `MILHOUSE_AGENT_SUBMIT_TIMEOUT_MS` | How long herdr may take to confirm one prompt reached the agent. Not the turn timeout: the wait ends as soon as herdr observes the agent react. |
| `submit_attempts` | int | `3` | `MILHOUSE_AGENT_SUBMIT_ATTEMPTS` | How many times to submit a prompt before giving up on the turn. `1` disables the retry. |

`--agent` on the CLI overrides `agent.kind`.

`submit_timeout_ms` is a backstop rather than the deadline that normally fires. herdr requires an _observed state change_ within **5000 ms** of a submission made from a non-working state, and answers `agent_prompt_stalled` if it does not see one, whatever timeout milhouse asked for. That five-second floor is herdr's, is not configurable from here, and is why a `submit_timeout_ms` below it changes only the error code (`timeout` instead), not the behaviour.

`submit_attempts` exists because the prompt is what goes missing. A prompt sent to a just-started agent is regularly swallowed — three cold starts in a row against herdr 0.7.5 stalled at the floor, and all three took the prompt on the re-submission in about a third of a second. Re-submitting cannot double-run a turn: herdr's own count of the state changes it has observed is checked either side of the stall, and a count that moved means the prompt landed after all. See [ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md).

## `[run]`

What bounds one `milhouse run`. Nothing here affects `step`, `dispatch`, or `reap`, which take one turn and hand back. See [ADR 0022](decisions/0022-the-loop-is-earned.md).

| Key              | Type | Default | Environment                   | Meaning                                                                       |
| ---------------- | ---- | ------- | ----------------------------- | ----------------------------------------------------------------------------- |
| `max_iterations` | int  | `50`    | `MILHOUSE_RUN_MAX_ITERATIONS` | Turns one run may take before it stops and reports.                           |
| `max_attempts`   | int  | `3`     | `MILHOUSE_RUN_MAX_ATTEMPTS`   | Attempts one issue gets before the run defers it and works on something else. |
| `max_parallel`   | int  | `1`     | `MILHOUSE_RUN_MAX_PARALLEL`   | Turns one run may keep in flight at once.                                     |
| `poll_ms`        | int  | `5000`  | `MILHOUSE_RUN_POLL_MS`        | How often a concurrent run checks whether a lane has settled.                 |

`--max-iterations` and `--max-attempts` override the first two. Every key here must be at least 1: zero is rejected rather than silently meaning a run that stops at the ceiling having done nothing, which reports a stop reason that sounds like progress.

**`max_parallel` is the key `--count` overrides, and the two do not share a name.** They are the one such pair in milhouse, so the mapping is written down rather than inferred ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)): `[run]` is a section of ceilings, and a key in a committed file says what any run of this repository may not exceed, while a flag says what one invocation is doing. `milhouse run <target> --count 4` and `max_parallel = 4` are the same setting.

At `1` a run is exactly what [ADR 0023](decisions/0023-a-run-has-one-lane.md) describes: one lane, one turn at a time, nothing to merge. Above `1` each issue gets a worker lane branched from the run's integration branch and merged back into it as its turn settles, and `max_iterations` then counts turns that have been _started_ rather than finished, so a wide run cannot overshoot its ceiling. A width above what the dependency graph can use is accepted rather than refused, because it is harmless: `milhouse run <target> --count N --dry-run` prints the waves and says how much of `N` the target can actually use.

`poll_ms` is ignored by a serial run, which waits on each turn and has nothing to poll. Every poll asks herdr about each open lane and re-reads the audit trail, against turns that take minutes, so the default is deliberately unhurried. It is also the grace period on `[agent] turn_timeout_ms`: a concurrent run gives up on a lane herdr has lost one poll interval past the turn timeout, rather than a round early because the deadline fell between two checks.

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

Empty by default, so out of the box milhouse takes the agent at its word. There is no safe guess about a given repository's gate, and a wrong one fails every iteration. Point it at the fast suite rather than the full matrix: it runs at least once per closed issue.

**A concurrent run can pay for this twice per merged issue.** Once in the worker lane the turn happened in, and once on the integration branch after that lane is merged into it, because a lane that is green against its own base can be red combined with another one and the merged tree is the only place that shows up ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)).

The doubling is a **ceiling rather than a rate**, and the gap between the two is large. The second run happens only after a merge that joined two histories: a fast-forward is skipped, since it leaves the tree the worker lane was already verified against, and a merge that conflicted has nothing to verify. In the watched runs [ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md#what-the-first-concurrent-runs-taught) records, that was **one merge in eight** — three fast-forwarded, four conflicted, one joined. So a five-minute suite on a ten-issue epic has a worst case of a hundred minutes of verification rather than fifty, and those runs paid nearer the fifty. A run working one issue at a time pays exactly what it paid before, and with no command configured there are no runs at all, neither the first nor the second.

A red integration branch stops the run and reverts nothing. The merge stays, the issue stays closed, and the tail of the output is appended to that issue as a note, because the work was genuinely done and it is the combination that is red.

No shell is involved, so this is argv rather than a command line. `MILHOUSE_VERIFY_COMMAND` is split with `shlex`, so quoting works the way it does in a shell.

There is no `[git]` section. `branch_strategy` and `branch_prefix` named a branch after the task definition, and there is no task definition ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md)). Where commits land is `[lane]`'s answer now.

## `[lane]`

Where an issue's agent works. See [ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md).

| Key             | Type   | Default       | Environment                   | Meaning                                             |
| --------------- | ------ | ------------- | ----------------------------- | --------------------------------------------------- |
| `branch_prefix` | string | `"milhouse/"` | `MILHOUSE_LANE_BRANCH_PREFIX` | Prefix for a lane's branch, e.g. `milhouse/bd-e.1`. |

A lane is a herdr worktree labelled with the issue id, and herdr chooses where it goes — under `~/.herdr/worktrees/<repo>/<branch>` — so there is no key saying where lanes live. Nothing here turns lanes off: every turn happens in one.

The prefix is in front of every branch milhouse creates, which is three shapes rather than one ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)): `milhouse/<issue>` for a `dispatch` lane, `milhouse/<target>` for a run's integration branch, and `milhouse/<target>--<issue>` for a worker branch inside that run. The `--` is not configurable, and it is not `/` because git cannot hold `milhouse/bd-e` and `milhouse/bd-e/bd-e.1` at the same time. Given more than one target, `<target>` is every target's id, sorted and joined with `+` ([ADR 0025](decisions/0025-a-multi-target-run-shares-one-lane.md)).

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

**A workspace for a different repository is ignored, with a line saying so.** herdr resolves which repository a lane comes from by looking at its source workspace, so the wrong one silently branches, works, and commits somewhere nobody asked it to. That is reachable by accident, and it is exactly what the combination above sets up: the ambient `HERDR_WORKSPACE_ID` is right when you are stepping the repository you are sitting in, and wrong the moment `--repo` points elsewhere. milhouse falls back to its own labelled workspace rather than refusing, because there is a correct one to fall back to and an unattended run that carries on in the right repository beats one that stops. `milhouse status` reports the same verdict a run would.

A reused workspace is not an empty one, which is why `self_pane` exists. Its panes belong to somebody — very often to the terminal `milhouse step` was just typed into, since that pane is the reason `HERDR_WORKSPACE_ID` is set at all. milhouse therefore picks a pane rather than taking the first one: it skips `self_pane`, skips any pane already running an agent, and splits a new pane when nothing is free. `HERDR_PANE_ID` is set by herdr for you, so this is not a key you should need to write down.

`read_source` defaults to `visible` rather than `recent` because `recent` returns only output since the previous read, which is empty when nothing has been read before — a surprising transcript to find in a post-mortem.

## Environment variables at a glance

| Variable                           | Sets                      |
| ---------------------------------- | ------------------------- |
| `MILHOUSE_AGENT_KIND`              | `agent.kind`              |
| `MILHOUSE_AGENT_ARGS`              | `agent.args`              |
| `MILHOUSE_AGENT_START_TIMEOUT_MS`  | `agent.start_timeout_ms`  |
| `MILHOUSE_AGENT_EXIT_TIMEOUT_MS`   | `agent.exit_timeout_ms`   |
| `MILHOUSE_AGENT_SUBMIT_TIMEOUT_MS` | `agent.submit_timeout_ms` |
| `MILHOUSE_AGENT_SUBMIT_ATTEMPTS`   | `agent.submit_attempts`   |
| `MILHOUSE_TURN_TIMEOUT_MS`         | `agent.turn_timeout_ms`   |
| `MILHOUSE_RUN_MAX_ITERATIONS`      | `run.max_iterations`      |
| `MILHOUSE_RUN_MAX_ATTEMPTS`        | `run.max_attempts`        |
| `MILHOUSE_RUN_MAX_PARALLEL`        | `run.max_parallel`        |
| `MILHOUSE_RUN_POLL_MS`             | `run.poll_ms`             |
| `MILHOUSE_VERIFY_COMMAND`          | `verify.command`          |
| `MILHOUSE_VERIFY_TIMEOUT_MS`       | `verify.timeout_ms`       |
| `MILHOUSE_LANE_BRANCH_PREFIX`      | `lane.branch_prefix`      |
| `MILHOUSE_TRACKER_LABEL`           | `tracker.label`           |
| `MILHOUSE_TRACKER_PARENT`          | `tracker.parent`          |
| `MILHOUSE_WORKSPACE`               | `herdr.workspace`         |
| `HERDR_WORKSPACE_ID`               | `herdr.workspace`         |
| `HERDR_PANE_ID`                    | `herdr.self_pane`         |

Variables holding an integer must parse as one, or milhouse exits with `ConfigError` (exit code 2). `MILHOUSE_AGENT_ARGS` and `MILHOUSE_VERIFY_COMMAND` are split with `shlex`, so quoting works the way it does in a shell.

## What is not configurable

- Prompt templates. They ship in the package and are versioned with the code, so a run is reproducible from a milhouse version. See [prompts](prompts.md).
- The `.milhouse/runs/` layout. See [troubleshooting](troubleshooting.md).
- Token or cost caps. milhouse cannot observe spend through a herdr pane; see [ADR 0012](decisions/0012-no-cost-controls-in-v1.md).
