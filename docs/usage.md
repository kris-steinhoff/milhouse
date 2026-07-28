# Usage

Every command and flag, with worked examples. Output shown here was captured from a real run.

## Global options

```
milhouse [--version] [--verbose] <command> [options]
```

| Option                 | Meaning                                                            |
| ---------------------- | ------------------------------------------------------------------ |
| `--version`            | Print the milhouse version and exit.                               |
| `--verbose`, `-v`      | Log every subprocess milhouse runs, to stderr. The debugging tool. |
| `--install-completion` | Install shell completion for milhouse, then exit.                  |
| `--show-completion`    | Print the completion script instead of installing it.              |

## Shell completion

Install it once, then restart the shell:

```console
$ milhouse --install-completion
zsh completion installed in /home/you/.zfunc/_milhouse
Completion will take effect once you restart the terminal
```

The shell is detected from the process tree, and bash, zsh, fish, and PowerShell are supported. `--show-completion` prints the same script instead of writing anything, which is what you want when your shell config is generated or version-controlled elsewhere.

What completes:

| Parameter | Offers                                                             |
| --------- | ------------------------------------------------------------------ |
| `--repo`  | Directories.                                                       |
| `--agent` | The common herdr agent kinds. Any kind herdr supports still works. |

Nothing here contacts the herdr server or `bd`, so completion stays instant and works with the server down. That is why `--workspace` is not on the list: milhouse writes no workspace id down any more, and the only thing that knows one is herdr itself.

## Getting work into the tracker

milhouse does not do this, and it has no planning agent. It claims whatever `bd ready` offers and works it ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md)).

That means issue quality is entirely yours to keep up. `iterate.md.j2` hands one issue to an agent with no context and nothing else ([ADR 0013](decisions/0013-iteration-prompt-contract.md)), so an issue with a thin description simply produces a worse iteration, and nothing warns you. Useful shape:

- **One agent-turn of work per issue**, independently verifiable.
- **A description written for a stranger**, plus `--acceptance` saying how they know they are done.
- **`bd dep add` for real ordering constraints only.** A dependency that is not real just serialises work that could have run in parallel.
- **An epic over the set.** Its description becomes the background every child's prompt carries, which is the only place the wider context lives now.

### Fencing the queue

By default milhouse considers every ready issue in the repository. Where the beads database also carries work that was never meant for an agent, fence it with a label or a parent, either on the command line or once in [`.milhouse/config.toml`](configuration.md#tracker):

```toml
[tracker]
parent = "milhouse-6or"   # only issues under this epic
label = "agent"           # only issues carrying this label
```

Epics are never offered whatever the fence says: an epic is a container for work, not a unit of it.

## `milhouse doctor`

Verify the tools milhouse depends on and the state of the herdr server. Run this first, and again whenever a run fails in a way that does not make sense.

```
milhouse doctor [--repo PATH]
```

| Option   | Default            | Meaning                                      |
| -------- | ------------------ | -------------------------------------------- |
| `--repo` | the enclosing repo | Repository to check, if not the current one. |

```console
$ milhouse doctor
ok   bd            bd version 1.1.0 (8e4e59d39: HEAD@8e4e59d39f34)
ok   beads db      /home/agent/code/github.com/kris-steinhoff/milhouse/.beads
ok   herdr         herdr 0.7.5
ok   herdr server  running, protocol 17
ok   git           git version 2.47.3
ok   claude        2.1.220 (Claude Code)
ok   config        using defaults (no .milhouse/config.toml)
```

Three result levels:

- `ok` — the check passed.
- `warn` — an optional tool is missing. The agent binary is only needed for real runs, so a `warn` there is fine until you need it.
- `FAIL` — a required tool or service is missing. `doctor` exits `7`.

The agent row uses whatever `[agent] kind` is configured, so it checks the agent you will actually run.

## `milhouse step`

One iteration, then back to you. It claims the next ready issue, hands it to a **freshly started** agent, classifies what happened, and stops.

This is the whole of how milhouse is driven. There is no command that repeats it, on purpose: the policy a loop would need is still the open question, and the way to answer it is to watch real iterations rather than reason about them ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)).

```
milhouse step [options]
```

| Option        | Default              | Meaning                                                 |
| ------------- | -------------------- | ------------------------------------------------------- |
| `--agent`     | `claude`             | Agent kind to run. Any kind herdr supports.             |
| `--workspace` | `HERDR_WORKSPACE_ID` | Reuse this herdr workspace instead of creating one.     |
| `--parent`    | unfenced             | Only work issues under this epic.                       |
| `--label`     | unfenced             | Only work issues carrying this label.                   |
| `--dry-run`   | off                  | Render the prompt and print the plan; start no agent.   |
| `--attach`    | off                  | Focus the herdr workspace instead of leaving it hidden. |
| `--repo`      | the enclosing repo   | Repository to work in.                                  |

Everything except `--dry-run` and `--attach` can also be set in [`.milhouse/config.toml`](configuration.md).

Run from inside a herdr pane, `--workspace` defaults to the workspace that pane is in. Otherwise milhouse looks for an open workspace labelled `milhouse:<repo>` — the one an earlier step left behind — and creates one if there is none. Either way it works in a free pane, never the one you typed into, splitting a new pane if it has to ([configuration](configuration.md#herdr)).

The agent commits on whatever branch is checked out. milhouse no longer creates one: there is no task to name a branch after, and lanes are what decides this next ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)).

Exits `0` when the issue was finished and `9` when it was not, so a shell loop can stand in for the policy milhouse does not have:

```sh
while milhouse step; do :; done
```

That stops at the first iteration that does not succeed, which is what a supervised policy would do anyway. Everything a real loop would add beyond it is the part nobody has earned yet.

### Resuming

Stepping again **is** the resume mechanism. Any claim a previous step left behind is re-opened first ([ADR 0008](decisions/0008-crash-recovery-by-reconciliation.md)). There is no separate `resume` command.

Iteration numbers keep counting across invocations, because they name `<issue-id>/iter-NNN.prompt`, and the history in the beads audit log spans all of them.

### One step at a time

A step holds a lock on the repository's run directory. A second `milhouse step` in the same repository refuses to start and names the process holding it, because both would drive the same pane and re-open each other's in-flight claim ([ADR 0015](decisions/0015-one-run-at-a-time.md)):

```console
$ milhouse step
milhouse: another milhouse run is working this repository (pid 48213 on carbon, since 2026-07-26T09:14:02+00:00)
  Wait for the other run, or delete .milhouse/runs/lock.json.
```

A lock left behind by a dead process is taken over automatically, with a line saying so.

### What each iteration does

1. `bd ready --claim --limit 1 --exclude-type epic`, plus the configured fence — an empty result means nothing is ready.
2. Render `iterate.md.j2` for that issue and save it to `<issue-id>/iter-NNN.prompt`.
3. `herdr agent start` a **new** agent in the pane.
4. `herdr agent prompt --wait` until the turn settles.
5. Capture the pane transcript to `<issue-id>/iter-NNN.term`.
6. Exit the agent, returning the pane to a shell prompt.
7. If the issue is closed and `[verify] command` is set, run it.
8. Classify the outcome from beads, git, and that command, and record it.

| Outcome    | Means                                         | Issue becomes | Exits |
| ---------- | --------------------------------------------- | ------------- | ----- |
| `success`  | The issue is closed, and verification passed. | closed        | `0`   |
| `rejected` | The issue is closed, but verification failed. | re-opened     | `9`   |
| `partial`  | Still open, but commits landed.               | re-opened     | `9`   |
| `stalled`  | Still open and nothing was committed.         | re-opened     | `9`   |
| `timeout`  | The turn did not settle in time.              | re-opened     | `9`   |
| `blocked`  | The agent is waiting on a human.              | re-opened     | `9`   |
| `error`    | herdr or `bd` failed.                         | re-opened     | `9`   |

Re-opening matters: a claimed issue is `in_progress`, and `bd ready` excludes those, so an unfinished issue that was simply left alone would never be offered again and the work would look finished with the work undone.

Git is read in the directory the turn ran in, not at the repository root, so a commit made anywhere else is not attributed to this issue ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)).

`partial` distinguishes a commit that names the issue from one that does not, because `HEAD` moving on its own could be anyone — a hook, or you in another terminal. The shas either way are in the audit entry ([ADR 0004](decisions/0004-outcome-from-beads-and-git.md)).

A turn that leaves the working tree dirty is reported too, whatever its outcome, because the next agent would inherit changes it did not make and cannot explain.

`rejected` is the one milhouse would otherwise miss. `bd close` is run by the agent, so "the issue is closed" is the agent grading its own exam. Point [`[verify] command`](configuration.md) at the repository's own gate and milhouse checks the answer, re-opening the issue with the failing output as a `bd` note ([ADR 0016](decisions/0016-milhouse-verifies.md)). It is empty by default.

What happens after an iteration is one pure function, `policy.decide()`. Changing how milhouse behaves between iterations means writing a second one, and that is where a loop's policy will go when there is one to write ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)).

### What a step prints

```console
$ milhouse step
created herdr workspace wG (milhouse:greet)
iteration 1: dogfood-6i2.1 Add goodbye(name) to src/greet/__init__.py and document it in README.md
  → success: dogfood-6i2.1 closed in beads
the herdr workspace wG is left open

dogfood-6i2.1: success — dogfood-6i2.1 closed in beads
```

Exit `0`. Step again for the next issue.

An iteration that does not finish its issue re-opens it and says what happened:

```console
$ milhouse step
iteration 2: dogfood-6i2.2 Make the src-layout greet package importable when running python -m pytest
  → stalled: dogfood-6i2.2 is still open and nothing was committed
  dogfood-6i2.2 did not finish (stalled: dogfood-6i2.2 is still open and nothing was committed)
the herdr workspace wG is left open

dogfood-6i2.2: stalled — dogfood-6i2.2 is still open and nothing was committed
```

Exit `9`. Read `iter-002.term`, fix whatever it shows, and step again. The issue is back in the ready queue, and the next agent is told this is attempt 2 and how attempt 1 ended.

### Nothing ready is two opposite things

A step also does nothing when `bd ready` offers nothing, which means either that everything in scope is closed or that everything left is stuck behind something. milhouse tells them apart by listing what is unfinished:

```console
$ milhouse step
the herdr workspace wG is left open

nothing is ready but 3 issue(s) are unfinished (dogfood-6i2.1, dogfood-6i2.2, dogfood-6i2.3); `bd blocked` says what is stuck
```

That exits `9`. Only "no issues are ready; everything in scope is closed" exits `0`. A step that did no work has to be distinguishable from one that finished the work, or the `while` loop above never terminates.

Repo-wide this question is weaker than it was under an epic: "unfinished" now means every open issue in scope, so a fence usually makes the answer worth more. `bd blocked` is the tool for the follow-up.

The workspace is deliberately left open so the panes can be inspected ([ADR 0005](decisions/0005-milhouse-owns-the-loop.md)).

### The queue grows while you step through it

An agent that spots work outside its issue is told to file it rather than do it ([ADR 0013](decisions/0013-iteration-prompt-contract.md)), so the ready queue can gain issues while you are working through it. In a dogfood run an agent working the third issue filed a fourth, and the next step picked it up.

This is intended, and it is one of the reasons there is no loop yet: agents can add issues as fast as they are closed, so "work until the queue is empty" is not guaranteed to terminate. A person deciding whether to step again is a bound that needs no configuration.

### `--dry-run`

Shows exactly what the next step would do, including the prompt it would send, and starts nothing:

```console
$ milhouse step --dry-run
dry run — no agent will be started
scope     every ready issue in the repository
branch    main
agent     claude
verify    (none — a closed issue is taken on trust)
run dir   /home/agent/code/github.com/kris-steinhoff/milhouse/.milhouse/runs

the next step would work dogfood-6i2.2 and send:

    You are working **one issue** for the milhouse orchestrator. milhouse picked it,
    milhouse decides what happens next, and this session ends when the issue does.
    …
```

It is the cheapest way to see the effect of a prompt, fence, or config change.

## `milhouse status`

What is in scope, what is claimed, and this repository's iteration history. Reads beads and the run state; starts nothing and changes nothing.

```
milhouse status [--repo PATH]
```

```console
$ milhouse status
repo    /home/agent/code/github.com/kris-steinhoff/greet
scope   every ready issue in the repository
branch  main
herdr   workspace wY, pane wY:p3

  [x] dogfood-6i2.1  Add goodbye(name) to src/greet/__init__.py and document it in README.md  (closed)
  [ ] dogfood-6i2.2  Make the src-layout greet package importable when running python -m pytest  (open)
  [ ] dogfood-6i2.3  Add tests/test_greet.py covering hello and goodbye  (open)

iterations (2)
    1  success   dogfood-6i2.1  dogfood-6i2.1 closed in beads
    2  stalled   dogfood-6i2.2  dogfood-6i2.2 is still open and nothing was committed
```

It also flags any claim or lock left behind by an unfinished run. The history spans every invocation in this repository, not just the last one, because it is read back out of the beads audit log — one append-only trail that `bd`'s own entries share ([ADR 0021](decisions/0021-iteration-history-goes-in-the-beads-audit-log.md)).

`bd audit` has no query, so `milhouse status` is the readable view of `.beads/interactions.jsonl`. Reading the file directly works too, and shows bd's entries interleaved with milhouse's:

```sh
jq -c 'select(.kind == "iteration") | {issue_id, extra}' .beads/interactions.jsonl
```

## End-to-end check

The manual check that the loop really works. It needs eyes on it, because the thing being verified — that the context is fresh every iteration — is only visible in the pane.

```sh
milhouse doctor            # all required rows green
bd ready                   # there is something to work
milhouse step --dry-run    # the prompt looks right
milhouse step --attach     # one iteration, watched
milhouse step --attach     # and another
```

Watch for:

1. A workspace named `milhouse:<repo>` appears.
2. The pane shows the agent starting and working.
3. **The pane returns to a shell prompt when the step ends.** This is the one that matters: it is what proves the next iteration gets a fresh context window.
4. The issue closes in `bd`, and `milhouse status` shows both iterations.

Then trigger a permission prompt deliberately and confirm herdr reports `blocked`, and that milhouse stops with the workspace to attach to rather than sitting there.

Finally, run `milhouse step` twice at once in two terminals and confirm the second refuses with exit code `10`.

## Exit codes

Stable, and safe to branch on in a script.

| Code  | Error                    | Means                                                                 |
| ----- | ------------------------ | --------------------------------------------------------------------- |
| `0`   | —                        | Success.                                                              |
| `1`   | `MilhouseError`          | An expected failure with no more specific category.                   |
| `2`   | `ConfigError`            | `.milhouse/config.toml`, an env var, or a flag is invalid.            |
| `4`   | `TrackerError`           | `bd` failed, or the beads database is missing.                        |
| `5`   | `HerdrError`             | `herdr` failed, or the server is unreachable.                         |
| `6`   | `AgentError`             | An agent could not be started, prompted, or exited.                   |
| `7`   | `MissingDependencyError` | A required tool is not on `PATH`. Also `doctor`'s failure code.       |
| `8`   | `ProcessError`           | A subprocess failed in a way no caller translated.                    |
| `9`   | (no exception)           | A step did not finish its issue. The run directory is left to resume. |
| `10`  | `RunLockedError`         | Another milhouse process is already working this repository.          |
| `130` | `UserAbortError`         | Interrupted, or a confirmation was declined.                          |

`3` is retired. It was `SourceError`, raised when a task definition could not be resolved, and there are no task definitions ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md)). The codes do not renumber, because scripts branch on them.

Every error prints one line on stderr, plus a remedy line when there is something specific to try. `9` is the exception: it is an outcome rather than a failure, so it is reported on stdout like any other result.
