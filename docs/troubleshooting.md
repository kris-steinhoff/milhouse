# Troubleshooting

Start with `milhouse doctor`. It checks every external dependency and the herdr server in one pass.

## `doctor` failures

| Row            | What it means                                        | Fix                                                                 |
| -------------- | ---------------------------------------------------- | ------------------------------------------------------------------- |
| `bd`           | `bd` is not on `PATH`.                               | `brew install beads`, or the install script from the beads repo.    |
| `beads db`     | `bd` works but this repo has no database.            | `bd init` in the repository root.                                   |
| `herdr`        | `herdr` is not on `PATH`.                            | Install from [herdr.dev](https://herdr.dev).                        |
| `herdr server` | The server is not running, or protocol-incompatible. | Run `herdr` to start it; `herdr update` if the protocol mismatches. |
| `git`          | Not on `PATH`, or milhouse is not inside a repo.     | Run milhouse from inside a git repository.                          |
| `gh` (warn)    | Only needed for `gh:owner/repo#123` task sources.    | Install and `gh auth login`, or use a file source.                  |
| agent (warn)   | The configured `[agent] kind` binary is missing.     | Install it, or point `[agent] kind` at one you have.                |

`doctor` exits `7` if any required row fails, so it can gate a script.

## Run artifacts

Everything milhouse records about a task lives here, and it is the primary post-mortem surface — there is no event stream from herdr ([ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md)):

```
.milhouse/
  config.toml                  # committed — agent command, defaults
  runs/<task_slug>/
    state.json                 # workspace/pane id, epic id, branch, the in-flight claim
    events.jsonl               # one iteration per line, append-only, every invocation
    lock.json                  # pid and host of the run holding this task, while it runs
    plan.json                  # what the planning agent proposed
    iter-007.prompt            # the exact prompt sent that iteration
    iter-007.term              # pane transcript captured after the turn
```

`.milhouse/runs/` is gitignored. Beads and git remain the source of truth for the work itself; everything under `runs/` is bookkeeping and is safe to delete. Deleting it loses the iteration history, nothing else.

When a run misbehaves, read them in this order:

1. `events.jsonl` — what milhouse thought happened, one line per iteration. `milhouse status` is the readable version.
2. `iter-NNN.term` for the first bad iteration — what the agent actually did.
3. `iter-NNN.prompt` — what it was actually asked. Prompts are rendered per iteration and differ between attempts.

## The agent produced nothing, and the turn looked fine

Symptom: `milhouse plan` reports that the agent did not write `plan.json`, or an iteration classifies as `stalled`, but nothing errored and the turn settled normally. The cause is almost always that the agent is sitting on a screen it cannot leave without a human. Open `iter-NNN.term` and look at the last few lines.

The one that bites first is `--dangerously-skip-permissions`. It shows a one-time consent screen before the agent will accept any input:

```
  WARNING: Claude Code running in Bypass Permissions mode
  ❯ 1. No, exit
    2. Yes, I accept
```

An unattended agent never answers it. herdr reports the agent as started and `interactive_ready`, the prompt goes into a dialog rather than into the agent, the turn settles having spent nothing, and the run ends with a confusing message. The tell in the transcript is a token count of zero.

Accepting it once, by hand, is the fix — the choice is remembered. Until then no `[agent] args` setting helps, because the screen comes up before the agent reads any of them.

A scoped list needs no consent screen, and is enough for **planning**:

```toml
[agent]
args = [
  "--permission-mode", "acceptEdits",
  "--allowedTools", "Write,Edit,Read,Bash(git:*),Bash(bd:*)",
]
```

The planning agent writes one file and stops, so an edit permission covers it. **Iterations are a different matter**, and the same config blocks them on the first composed shell command:

```
cat .milhouse/config.toml; echo "---"; ls -a ~/.venv; pip --version
```

That matches no prefix pattern, and an agent authoring its own commands writes things like it constantly. Widening the list until it stops is how you arrive at unscoped `Bash`, which is the unattended posture with extra steps — see [ADR 0009](decisions/0009-permission-posture.md), which is about this exact finding.

This is the general shape of the problem: an agent flag that opens an interactive gate is unusable in a loop, and the transcript is the only place it shows. See the `blocked` path below for gates that milhouse _can_ detect. `milhouse plan` names that case rather than reporting only the missing `plan.json`.

## The agent is blocked

herdr reports `blocked` when the agent is waiting on a human, which is almost always a permission prompt. milhouse re-opens the issue, stops, and names the workspace to attach to:

```sh
herdr workspace list          # find the milhouse:<slug> workspace
herdr agent attach milhouse-<slug>
```

The agent is exited by then, so attaching shows you the pane rather than a live turn. If the agent was sitting on a dialog the exit keys do not dismiss, milhouse will have replaced the pane and the scrollback is gone with it — `iter-NNN.term` survives either way, because the transcript is captured before the agent is exited. Read it, decide what the agent needed, then run again: the issue is back in the ready queue.

If a run keeps blocking, the posture is wrong rather than the run. Grant the permissions the work needs in `[agent] args`, in the scoped form shown above.

## An issue keeps being rejected

`rejected` means the agent closed the issue and `[verify] command` then failed. The failing output is on the issue as a `bd` note, so start there:

```sh
bd show <issue-id>
```

Then run the verification command yourself. Two things it usually means:

- **The work really is not done.** The note is now in the next agent's prompt, so stepping again may be enough.
- **The gate is wrong for the loop.** A command that fails for reasons unrelated to the issue rejects every issue in the epic. Point `[verify] command` at the fast suite rather than the full matrix, and make sure it passes on a clean checkout before pointing milhouse at it.

The second one has a trap worth naming, because it turns an epic into a loop that cannot finish. **`pytest` exits `5` when it collects no tests**, and a non-zero exit is a failed gate:

```console
$ pytest -q; echo $?
no tests ran in 0.00s
5
```

So a repository whose first issue writes the code and whose second writes the tests rejects the first issue every time, re-opens it, and hands the next agent a note saying verification failed when the work was fine. Anything that reports "nothing to check" as an error does the same. Either gate on something that holds from the first commit, or leave `[verify] command` unset until there is a suite to run — an unset gate means milhouse takes a closed issue at its word ([ADR 0016](decisions/0016-milhouse-verifies.md)).

## A stale claim

If milhouse is killed with `SIGKILL`, or the machine goes away, an issue is left `in_progress` and assigned. `bd` has no lease expiry, so `bd ready` will never return that issue again.

The normal fix is to **step against the same task again**. It takes the run lock, re-opens the claim it recorded in `state.json`, and carries on ([ADR 0008](decisions/0008-crash-recovery-by-reconciliation.md)). Holding the lock first is what makes that safe: the claim being re-opened cannot belong to a step still working it ([ADR 0015](decisions/0015-one-run-at-a-time.md)).

To fix it by hand instead:

```sh
bd update <issue-id> --status open --assignee ""
```

## milhouse says another run holds the task

```console
$ milhouse step docs/tasks/hello.md
milhouse: another milhouse run holds hello (pid 48213 on carbon, since 2026-07-26T09:14:02+00:00)
```

Exit code `10`. One process works a task at a time, because two would drive the same pane and re-open each other's in-flight claim ([ADR 0015](decisions/0015-one-run-at-a-time.md)).

A lock whose process is dead is taken over automatically. You only see this when the process is alive, or when it ran on another machine and its pid cannot be checked. If you are sure it is gone:

```sh
rm -f .milhouse/runs/<task_slug>/lock.json
```

## The task was planned twice

`task_id` is derived from the path (`file:docs/tasks/hello.md`), so renaming or moving a task definition orphans its epic and milhouse plans it again ([ADR 0002](decisions/0002-link-issues-via-bead-metadata.md)).

Point the old epic at the new path and delete the duplicate:

```sh
bd list --metadata-field milhouse_task=file:<old-path> --type epic --json
bd update <epic-id> --set-metadata milhouse_task=file:<new-path>
```

## A step reported the working tree is dirty

The iteration left uncommitted changes behind. That matters before you step again, because the next agent would inherit changes it did not make and cannot explain.

```sh
git status
git diff
```

Commit them if they are the work, discard them if they are not, then step again.

If `git status` shows only files your issue tracker wrote, that is not the agent's doing. `bd` appends to `.beads/interactions.jsonl` on every call, milhouse calls `bd` several times a step, and a repository that tracks that file therefore reads as dirty during and after every run. `bd init` ignores it for you; a repository that predates that does not. Ignore it and the report goes quiet:

```sh
echo '.beads/interactions.jsonl' >> .gitignore
```

milhouse's own bookkeeping cannot cause this: it keeps `.milhouse/runs/` ignored itself.

## The branch checkout failed

milhouse refuses to touch a dirty working tree. Commit or stash your changes first. It will not stash for you: losing uncommitted work to a checkout you did not ask for is the worst failure available ([ADR 0007](decisions/0007-branch-per-task.md)).

The same tracker files are worth ruling out here, because this check runs before the first agent starts and a dirty tree stops a step before it does anything at all.

## The pane did not return to a shell prompt

`herdr agent start` needs the pane at an interactive shell prompt. If the exit key sequence is wrong for your agent kind, milhouse falls back to closing the pane and splitting a new one, which works but loses the scrollback ([ADR 0011](decisions/0011-exiting-the-agent.md)).

If that happens every iteration, the sequence is wrong for your agent. Set it:

```toml
[agent]
exit_keys = ["ctrl+c", "ctrl+d"]
```

Run with `--verbose` and look for `invalid_key` first. herdr accepts the short form `c-c` but not `c-d`, so a sequence copied from tmux habits can be rejected outright — and a rejected sequence looks exactly like an agent that would not quit. Spell control keys `ctrl+c`, `ctrl+d`.
