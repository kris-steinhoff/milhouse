# Troubleshooting

Start with `milhouse doctor`. It checks every external dependency and the herdr
server in one pass.

## `doctor` failures

| Row            | What it means                                       | Fix                                                              |
| -------------- | --------------------------------------------------- | ---------------------------------------------------------------- |
| `bd`           | `bd` is not on `PATH`.                              | `brew install beads`, or the install script from the beads repo.  |
| `beads db`     | `bd` works but this repo has no database.           | `bd init` in the repository root.                                 |
| `herdr`        | `herdr` is not on `PATH`.                           | Install from [herdr.dev](https://herdr.dev).                      |
| `herdr server` | The server is not running, or protocol-incompatible.| Run `herdr` to start it; `herdr update` if the protocol mismatches.|
| `git`          | Not on `PATH`, or milhouse is not inside a repo.    | Run milhouse from inside a git repository.                        |
| `gh` (warn)    | Only needed for `gh:owner/repo#123` task sources.   | Install and `gh auth login`, or use a file source.                |
| agent (warn)   | The configured `[agent] kind` binary is missing.    | Install it, or point `[agent] kind` at one you have.              |

`doctor` exits `7` if any required row fails, so it can gate a script.

## Run artifacts

Everything milhouse records about a run lives here, and it is the primary
post-mortem surface — there is no event stream
([ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md)):

```
.milhouse/
  config.toml                  # committed — agent command, defaults
  runs/<task_slug>/
    state.json                 # workspace/pane id, epic id, per-issue attempts,
                               #   iteration history, the in-flight claim
    plan.json                  # what the planning agent proposed
    iter-007.prompt            # the exact prompt sent that iteration
    iter-007.term              # pane transcript captured after the turn
```

`.milhouse/runs/` is gitignored. Beads and git remain the source of truth for the
work itself; everything under `runs/` is loop bookkeeping and is safe to delete.
Deleting it loses the iteration history and the attempt counts, nothing else.

When a run misbehaves, read them in this order:

1. `state.json` — the `iterations` array says what milhouse thought happened.
2. `iter-NNN.term` for the first bad iteration — what the agent actually did.
3. `iter-NNN.prompt` — what it was actually asked. Prompts are rendered per
   iteration and differ between attempts.

## The agent is blocked

herdr reports `blocked` when the agent is waiting on a human, which is almost
always a permission prompt. milhouse prints the workspace to attach to and waits
(`--on-blocked wait`, the default):

```sh
herdr workspace list          # find the milhouse:<slug> workspace
herdr agent attach milhouse-<slug>
```

Approve the prompt and the loop continues on its own. `blocked` does not count
against `--max-attempts`.

If a run keeps blocking, the posture is wrong rather than the run. See
[ADR 0009](decisions/0009-permission-posture.md): either supervise it, use
`--on-blocked skip`, or make it explicitly unattended in `.milhouse/config.toml`.

## A stale claim

If milhouse is killed with `SIGKILL`, or the machine goes away, an issue is left
`in_progress` and assigned. `bd` has no lease expiry, so `bd ready` will never
return that issue again.

The normal fix is to **run `milhouse run` against the same task again**. It
reconciles at startup, re-opens the claim it recorded in `state.json`, and
resumes with the attempt counts intact
([ADR 0008](decisions/0008-crash-recovery-by-reconciliation.md)).

To fix it by hand instead:

```sh
bd update <issue-id> --status open --assignee ""
```

## The task was planned twice

`task_id` is derived from the path (`file:docs/tasks/hello.md`), so renaming or
moving a task definition orphans its epic and milhouse plans it again
([ADR 0002](decisions/0002-link-issues-via-bead-metadata.md)).

Point the old epic at the new path and delete the duplicate:

```sh
bd list --metadata-field milhouse_task=file:<old-path> --type epic --json
bd update <epic-id> --set-metadata milhouse_task=file:<new-path>
```

## The branch checkout failed

milhouse refuses to touch a dirty working tree. Commit or stash your changes
first. It will not stash for you: losing uncommitted work to an unattended loop
is the worst failure available ([ADR 0007](decisions/0007-branch-per-task.md)).

## The pane did not return to a shell prompt

`herdr agent start` needs the pane at an interactive shell prompt. If the exit
key sequence is wrong for your agent kind, milhouse falls back to closing the
pane and splitting a new one, which works but loses the scrollback
([ADR 0011](decisions/0011-exiting-the-agent.md)).

If that happens every iteration, the sequence is wrong for your agent. Set it:

```toml
[agent]
exit_keys = ["ctrl-c", "ctrl-d"]
```
