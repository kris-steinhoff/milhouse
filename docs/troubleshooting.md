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
| agent (warn)   | The configured `[agent] kind` binary is missing.     | Install it, or point `[agent] kind` at one you have.                |

`doctor` exits `7` if any required row fails, so it can gate a script.

## Run artifacts

The turn artifacts are the primary post-mortem surface, because there is no event stream from herdr ([ADR 0001](decisions/0001-shell-out-to-bd-and-herdr.md)) and its scrollback is gone once a pane is replaced:

```
.milhouse/
  config.toml                  # committed — agent command, defaults
  runs/
    .gitignore                 # written by milhouse; ignores this whole directory
    <issue-id>/
      lock.json                # pid and host of whoever is working this lane
      iter-007.prompt          # the exact prompt sent that iteration
      iter-007.term            # pane transcript captured after the turn
```

Each issue gets a directory, so every attempt at one issue sits together and two agents working different issues cannot collide on a filename.

`.milhouse/runs/` is gitignored. Beads and git remain the source of truth for the work itself, and the iteration history is in the beads audit log ([ADR 0021](decisions/0021-iteration-history-goes-in-the-beads-audit-log.md)). Everything under `runs/` is safe to delete: doing so loses the prompts and transcripts, nothing else.

When a run misbehaves, read them in this order:

1. `milhouse status` — what milhouse thought happened, one line per iteration. The underlying entries are in `.beads/interactions.jsonl`, interleaved with bd's own.
2. `iter-NNN.term` for the first bad iteration — what the agent actually did.
3. `iter-NNN.prompt` — what it was actually asked. Prompts are rendered per iteration and differ between attempts.

## The agent produced nothing, and the turn looked fine

Symptom: an iteration classifies as `stalled`, but nothing errored and the turn settled normally. The cause is almost always that the agent is sitting on a screen it cannot leave without a human. Open `iter-NNN.term` and look at the last few lines.

The one that bites first is `--dangerously-skip-permissions`. It shows a one-time consent screen before the agent will accept any input:

```
  WARNING: Claude Code running in Bypass Permissions mode
  ❯ 1. No, exit
    2. Yes, I accept
```

An unattended agent never answers it. herdr reports the agent as started and `interactive_ready`, the prompt goes into a dialog rather than into the agent, the turn settles having spent nothing, and the run ends with a confusing message. The tell in the transcript is a token count of zero.

Accepting it once, by hand, is the fix — the choice is remembered. Until then no `[agent] args` setting helps, because the screen comes up before the agent reads any of them.

A scoped list needs no consent screen, and is enough for an agent that only edits files:

```toml
[agent]
args = [
  "--permission-mode", "acceptEdits",
  "--allowedTools", "Write,Edit,Read,Bash(git:*),Bash(bd:*)",
]
```

**Iterations are a different matter**, and that config blocks them on the first composed shell command:

```
cat .milhouse/config.toml; echo "---"; ls -a ~/.venv; pip --version
```

That matches no prefix pattern, and an agent authoring its own commands writes things like it constantly. Widening the list until it stops is how you arrive at unscoped `Bash`, which is the unattended posture with extra steps — see [ADR 0009](decisions/0009-permission-posture.md), which is about this exact finding.

This is the general shape of the problem: an agent flag that opens an interactive gate is unusable in a loop, and the transcript is the only place it shows. See the `blocked` path below for gates that milhouse _can_ detect.

## The agent is blocked

herdr reports `blocked` when the agent is waiting on a human, which is almost always a permission prompt. milhouse re-opens the issue and stops. The agent runs in the issue's lane, so that is where to look:

```sh
milhouse status               # the lanes, and which issue each one is for
herdr agent attach milhouse-<issue-id>
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
- **The gate is wrong for the loop.** A command that fails for reasons unrelated to the issue rejects every issue in the queue. Point `[verify] command` at the fast suite rather than the full matrix, and make sure it passes on a clean checkout before pointing milhouse at it.

The second one has a trap worth naming, because it turns the queue into a loop that cannot finish. **`pytest` exits `5` when it collects no tests**, and a non-zero exit is a failed gate:

```console
$ pytest -q; echo $?
no tests ran in 0.00s
5
```

So a repository whose first issue writes the code and whose second writes the tests rejects the first issue every time, re-opens it, and hands the next agent a note saying verification failed when the work was fine. Anything that reports "nothing to check" as an error does the same. Either gate on something that holds from the first commit, or leave `[verify] command` unset until there is a suite to run — an unset gate means milhouse takes a closed issue at its word ([ADR 0016](decisions/0016-milhouse-verifies.md)).

## A stale claim

If milhouse is killed with `SIGKILL`, or the machine goes away, an issue is left `in_progress` and assigned. `bd` has no lease expiry, so `bd ready` will never return that issue again.

The normal fix is to **step again**. It takes the run lock, re-opens every claim the audit log shows milhouse made and never finished, and carries on ([ADR 0008](decisions/0008-crash-recovery-by-reconciliation.md)). Holding the lock first is what makes that safe: the claim being re-opened cannot belong to a step still working it ([ADR 0015](decisions/0015-one-run-at-a-time.md)).

To fix it by hand instead:

```sh
bd update <issue-id> --status open --assignee ""
```

## milhouse says another run is working this lane

```console
$ milhouse step
milhouse: another milhouse run is working bd-e.2 (pid 48213 on carbon, since 2026-07-26T09:14:02+00:00)
```

Exit code `10`. One process works a lane at a time, because two would drive the same pane and re-open each other's in-flight claim ([ADR 0015](decisions/0015-one-run-at-a-time.md)). Another lane is unaffected.

A lock whose process is dead is taken over automatically. You only see this when the process is alive, or when it ran on another machine and its pid cannot be checked. If you are sure it is gone:

```sh
rm -f .milhouse/runs/<issue-id>/lock.json
```

## milhouse claimed an issue that was not meant for an agent

By default the ready queue is the whole repository, and milhouse cannot tell your own reminders from work an agent should pick up. Fence it, either per invocation or once in the config ([usage](usage.md#fencing-the-queue)):

```sh
milhouse step --parent <epic-id>
milhouse step --label agent
```

Epics are excluded whatever the fence says.

## milhouse refuses because an issue depends on two lanes

```console
$ milhouse step
milhouse: bd-e.3 depends on work in more than one lane (bd-e.1 on milhouse/bd-e.1, bd-e.2 on milhouse/bd-e.2),
and which branch it should continue from is not decided. Land one of them first.
```

`--base` takes one ref, and an issue whose blockers ran in separate lanes has two candidates. Which one to build on is [deliberately undecided](decisions/README.md#still-open), so milhouse stops rather than guessing.

Merge one of the lane branches into the other, or into your main line, and step again: with one live lane left, the issue stacks onto it.

## Where did the work go?

Nothing is committed in your checkout. Every turn happens in a lane — a herdr worktree under `~/.herdr/worktrees/<repo>/<branch>`, on its own branch ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)):

```sh
milhouse status                       # the lanes and their branches
git log --oneline milhouse/<issue-id> # what an agent actually committed
git merge milhouse/<issue-id>         # landing it is yours to do
```

A lane outlives the workspace holding it: closing the workspace leaves the checkout and the branch alone, and stepping the same issue again re-opens it. `herdr worktree remove` is how you get rid of one for good.

## Verification fails in a lane but passes in my checkout

A fresh worktree has no `.venv` and no `node_modules`, and `[verify] command` runs in the lane. A gate that assumes a built environment therefore fails for environmental reasons rather than real ones, and the issue is re-opened with a note saying so.

The per-lane bootstrap is [an open question](decisions/README.md#still-open). Until it has an answer: point the gate at something that bootstraps itself (`uv run …` creates the environment it needs), or leave `[verify] command` unset and take the agent at its word.

## A run stopped and I want to know why

Every run ends with a line saying what stopped it. The six answers, and what each one wants:

| Stop reason                             | What happened                                                 | Do                                                      |
| --------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------- |
| everything in scope is closed           | The target is done.                                           | Review the branch and land it.                          |
| nothing is ready but N are unfinished   | The queue deadlocked, usually behind a deferral or a blocker. | `bd blocked`, and the `deferred` section of the report. |
| the agent stopped waiting on a human    | A permission prompt, most often.                              | Fix the posture, then run again. See below.             |
| milhouse itself failed                  | `bd` or herdr, not the agent.                                 | Read the message; `milhouse doctor`.                    |
| a closed issue left uncommitted changes | An agent closed an issue with work still in the tree.         | Look at the lane before running again.                  |
| the ceiling                             | `--max-iterations` reached.                                   | Read the iteration list before raising it.              |

Everything a run did is also in the beads audit log, which outlives the terminal:

```sh
milhouse status                          # every iteration, with outcomes
bd list --status open                    # what is left
```

## A run deferred an issue

Three failed attempts, so milhouse set it aside and spent the rest of the run on work that might still land ([ADR 0022](decisions/0022-the-loop-is-earned.md)). It is still open and still unfinished — the run exits `9` because of it — but `bd ready` will not offer it again.

```sh
bd show <id>          # the notes are what the attempts found
bd undefer <id>       # put it back in the queue
```

**Read the notes before undeferring.** Attempts are counted over the whole audit history, not per run, so simply running again gives it no more turns. If the notes say the same thing three times, the issue is the problem, not the number of attempts: it is usually too big, or its description assumes context the agent does not have.

A deferral is not always a failure of the work. An issue whose change is implemented and committed but never `bd close`d will defer, and the next agent to see it closes it immediately — that is what happened on the first dogfood run.

## A run stopped on a blocked agent, immediately

The agent hit a permission prompt and waited for a human. An unattended run stops rather than skipping to the next issue, because the next issue would meet the same prompt and the run would spend its whole budget discovering that ([ADR 0022](decisions/0022-the-loop-is-earned.md)).

Attach to the lane herdr left open and look at what it is asking:

```sh
herdr workspace list          # the lane is labelled with the target id
```

Then set the posture for the next run ([ADR 0009](decisions/0009-permission-posture.md)):

```toml
[agent]
args = ["--permission-mode", "acceptEdits", "--allowedTools", "Write,Edit,Read,Bash"]
```

Scoping `Bash` to specific commands looks safer and is not workable in practice: agents compose shell commands, and a scoped allowlist blocks nearly every iteration on something. An agent's own consent screen for a wider posture also has to be accepted by hand once, in a real terminal, before any run gets past it.

## A run worked the wrong repository

Check which workspace it used. herdr exports `HERDR_WORKSPACE_ID` into every pane it launches, `milhouse` reads it as `[herdr] workspace`, and that is right when you are stepping the repo you are sitting in and wrong when you passed `--repo` somewhere else.

```sh
milhouse status --repo <path>            # names the workspace and where it came from
env -u HERDR_WORKSPACE_ID milhouse run <target> --repo <path>
```

The symptom is a lane whose checkout is under `~/.herdr/worktrees/<other-repo>/`, an agent that stalls because the files it was told about are not there, and a branch left in a repository nobody asked it to touch.

## A step reported the working tree is dirty

The iteration left uncommitted changes behind. That matters before you step again, because the next agent would inherit changes it did not make and cannot explain.

```sh
git status
git diff
```

Commit them if they are the work, discard them if they are not, then step again.

A **run** stops outright when this happens after a closed issue, rather than reporting it and continuing. Every iteration in a run shares one lane, so the next agent would start in the mess ([ADR 0023](decisions/0023-a-run-has-one-lane.md)). After a _failed_ turn it is reported and the run carries on: that issue is going to be retried anyway.

If `git status` shows only files your issue tracker wrote, that is not the agent's doing. `bd` appends to `.beads/interactions.jsonl` on every call, milhouse calls `bd` several times a step, and a repository that tracks that file therefore reads as dirty during and after every run. `bd init` ignores it for you; a repository that predates that does not. Ignore it and the report goes quiet:

```sh
echo '.beads/interactions.jsonl' >> .gitignore
```

milhouse's own bookkeeping cannot cause this: it keeps `.milhouse/runs/` ignored itself, and lanes live outside the repository. A lane that does land inside one gets a `.git/info/exclude` entry.

## The pane did not return to a shell prompt

`herdr agent start` needs the pane at an interactive shell prompt. If the exit key sequence is wrong for your agent kind, milhouse falls back to closing the pane and splitting a new one, which works but loses the scrollback ([ADR 0011](decisions/0011-exiting-the-agent.md)).

If that happens every iteration, the sequence is wrong for your agent. Set it:

```toml
[agent]
exit_keys = ["ctrl+c", "ctrl+d"]
```

Run with `--verbose` and look for `invalid_key` first. herdr accepts the short form `c-c` but not `c-d`, so a sequence copied from tmux habits can be rejected outright — and a rejected sequence looks exactly like an agent that would not quit. Spell control keys `ctrl+c`, `ctrl+d`.
