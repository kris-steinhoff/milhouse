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

| Parameter           | Offers                                                                            |
| ------------------- | --------------------------------------------------------------------------------- |
| `<task>`            | Markdown files and the directories holding them, `file:` prefix preserved.        |
| `--repo`            | Directories.                                                                      |
| `--agent`           | The common herdr agent kinds. Any kind herdr supports still works.                |
| `--branch-strategy` | `task`, `current`.                                                                |
| `--workspace`       | Workspace ids from this repo's earlier runs, most recent first, with their tasks. |

`gh:` task specs are not completed: that would mean a call to GitHub on a keypress. Nothing here contacts the herdr server either — `--workspace` reads `.milhouse/runs/*/state.json` — so completion stays instant and works with the server down.

## Task definitions

Every command that takes a `<task>` accepts the same spec forms. The spec determines the `task_id`, which is the stable link between the task and its beads epic ([ADR 0002](decisions/0002-link-issues-via-bead-metadata.md)).

| Spec                                          | `task_id`                  | Notes                                                  |
| --------------------------------------------- | -------------------------- | ------------------------------------------------------ |
| `docs/tasks/hello.md`                         | `file:docs/tasks/hello.md` | Relative to the current directory, then the repo root. |
| `file:docs/tasks/hello.md`                    | `file:docs/tasks/hello.md` | Same thing, said explicitly.                           |
| `/abs/path/hello.md`                          | `file:docs/tasks/hello.md` | Made repo-relative when it is inside the repo.         |
| `gh:owner/repo#123`                           | `gh:owner/repo#123`        | Fetched with `gh issue view`.                          |
| `gh:123`                                      | `gh:owner/repo#123`        | Repo inferred from the working directory.              |
| `gh:https://github.com/owner/repo/issues/123` | `gh:owner/repo#123`        | Paste a URL straight from a browser.                   |

A file task definition is any markdown. Its first `#` heading becomes the epic title (falling back to the filename), and the whole file is handed to the planning agent verbatim. An empty file is an error: there is nothing to decompose.

GitHub tasks also set `--external-ref gh-<number>` on the epic, so beads can round-trip the link.

Renaming a task file changes its `task_id` and orphans the existing epic. See [troubleshooting](troubleshooting.md#the-task-was-planned-twice).

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
ok   gh            gh version 2.96.0 (2026-07-02)
ok   claude        2.1.220 (Claude Code)
ok   config        using defaults (no .milhouse/config.toml)
```

Three result levels:

- `ok` — the check passed.
- `warn` — an optional tool is missing. `gh` is only needed for `gh:` task sources, and the agent binary only for real runs, so a `warn` on either is fine until you need it.
- `FAIL` — a required tool or service is missing. `doctor` exits `7`.

The agent row uses whatever `[agent] kind` is configured, so it checks the agent you will actually run.

## `milhouse step`

One iteration, then back to you. It claims one ready issue, hands it to a **freshly started** agent, classifies what happened, and stops. This is the supervised entry point, and the one to reach for while the loop's policy is still an open question ([ADR 0014](decisions/0014-step-is-the-primitive.md)).

```
milhouse step <task> [options]
```

| Option              | Default              | Meaning                                                           |
| ------------------- | -------------------- | ----------------------------------------------------------------- |
| `--agent`           | `claude`             | Agent kind to run. Any kind herdr supports.                       |
| `--workspace`       | `HERDR_WORKSPACE_ID` | Reuse this herdr workspace instead of creating one.               |
| `--branch-strategy` | `task`               | `task` for one branch per task definition; `current` to stay put. |
| `--attach`          | off                  | Focus the herdr workspace instead of leaving it hidden.           |
| `--yes`, `-y`       | off                  | Create the proposed issues without asking.                        |
| `--repo`            | the enclosing repo   | Repository to work in.                                            |

It decomposes the task first if it has not been decomposed yet, exactly as `run` does. Exits `0` when the issue was finished and `9` when it was not, so a shell loop can be your policy while milhouse does not have one:

```sh
while milhouse step docs/tasks/hello.md; do :; done
```

## `milhouse run`

`milhouse step` in a loop. Resolves the task, decomposes it if needed, then repeats: claim one ready issue, hand it to a fresh agent, classify, decide.

```
milhouse run <task> [options]
```

| Option              | Default              | Meaning                                                           |
| ------------------- | -------------------- | ----------------------------------------------------------------- |
| `--max-iterations`  | `50`                 | Iterations this invocation may run.                               |
| `--agent`           | `claude`             | Agent kind to run. Any kind herdr supports.                       |
| `--workspace`       | `HERDR_WORKSPACE_ID` | Reuse this herdr workspace instead of creating one.               |
| `--branch-strategy` | `task`               | `task` for one branch per task definition; `current` to stay put. |
| `--dry-run`         | off                  | Render the prompts and print the plan; start no agents.           |
| `--attach`          | off                  | Focus the herdr workspace instead of leaving it hidden.           |
| `--yes`, `-y`       | off                  | Create the proposed issues without asking.                        |
| `--repo`            | the enclosing repo   | Repository to work in.                                            |

Everything except `--dry-run`, `--attach`, and `--yes` can also be set in [`.milhouse/config.toml`](configuration.md).

**The run stops at the first iteration that does not succeed**, re-opens that issue, and says what needs a person. It does not retry, wait out a blocked agent, or give up on an issue and move to the next: those are decisions a supervised run hands back ([ADR 0014](decisions/0014-step-is-the-primitive.md)).

### Resuming

Re-running `milhouse run` or `milhouse step` against the same task **is** the resume mechanism. It re-opens any claim a previous run left behind and carries on ([ADR 0008](decisions/0008-crash-recovery-by-reconciliation.md)). There is no separate `resume` command.

`--max-iterations` bounds the invocation, not the task, so a resumed run gets the whole budget again. Iteration _numbers_ keep counting, because they name `iter-NNN.prompt`.

### One run at a time

A run holds a lock on its task's run directory. A second `milhouse run` or `milhouse step` against the same task refuses to start and names the process holding it, because both would drive the same pane and re-open each other's in-flight claim ([ADR 0015](decisions/0015-one-run-at-a-time.md)):

```console
$ milhouse step docs/tasks/hello.md
milhouse: another milhouse run holds hello (pid 48213 on carbon, since 2026-07-26T09:14:02+00:00)
  Wait for the other run, or delete .milhouse/runs/<task>/lock.json.
```

A lock left behind by a dead process is taken over automatically, with a line saying so. Different tasks have different locks and do not interfere.

### What each iteration does

1. `bd ready --parent <epic> --claim --limit 1` — an empty result means the epic is finished.
2. Render `iterate.md.j2` for that issue and save it to `iter-NNN.prompt`.
3. `herdr agent start` a **new** agent in the task's pane.
4. `herdr agent prompt --wait` until the turn settles.
5. Capture the pane transcript to `iter-NNN.term`.
6. Exit the agent, returning the pane to a shell prompt.
7. If the issue is closed and `[verify] command` is set, run it.
8. Classify the outcome from beads, git, and that command, and record it.

| Outcome    | Means                                         | Issue becomes | Run       |
| ---------- | --------------------------------------------- | ------------- | --------- |
| `success`  | The issue is closed, and verification passed. | closed        | continues |
| `rejected` | The issue is closed, but verification failed. | re-opened     | stops     |
| `partial`  | Still open, but `HEAD` moved.                 | re-opened     | stops     |
| `stalled`  | Still open and nothing was committed.         | re-opened     | stops     |
| `timeout`  | The turn did not settle in time.              | re-opened     | stops     |
| `blocked`  | The agent is waiting on a human.              | re-opened     | stops     |
| `error`    | herdr or `bd` failed.                         | re-opened     | stops     |

Re-opening matters: a claimed issue is `in_progress`, and `bd ready` excludes those, so an unfinished issue that was simply left alone would never be offered again and the epic would look finished with the work undone.

`rejected` is the one milhouse would otherwise miss. `bd close` is run by the agent, so "the issue is closed" is the agent grading its own exam. Point [`[verify] command`](configuration.md) at the repository's own gate and milhouse checks the answer, re-opening the issue with the failing output as a `bd` note ([ADR 0016](decisions/0016-milhouse-verifies.md)). It is empty by default.

What happens after an iteration is one pure function, `policy.decide()`. Changing how milhouse behaves between iterations means writing a second one, not rewriting the loop.

### How a run ends

Every iteration succeeded and the epic ran out of work:

```console
$ milhouse run docs/tasks/farewell.md
working on branch milhouse/farewell
created herdr workspace wG (milhouse:farewell)
using existing epic dogfood-6i2: Add a farewell function
iteration 1: dogfood-6i2.1 Add goodbye(name) to src/greet/__init__.py and document it in README.md
  → success: dogfood-6i2.1 closed in beads
iteration 2: dogfood-6i2.2 Make the src-layout greet package importable when running python -m pytest
  → success: dogfood-6i2.2 closed in beads
iteration 3: dogfood-6i2.3 Add tests/test_greet.py covering hello and goodbye
  → success: dogfood-6i2.3 closed in beads
no issues are ready; the epic is finished
the herdr workspace wG is left open

finished after 3 iterations: no issues are ready; the epic is finished
```

That is the only ending that exits `0`.

An iteration that does not succeed ends the run there, with the issue re-opened and a line saying what happened:

```console
$ milhouse run docs/tasks/farewell.md
working on branch milhouse/farewell
created herdr workspace wG (milhouse:farewell)
using existing epic dogfood-6i2: Add a farewell function
iteration 1: dogfood-6i2.1 Add goodbye(name) to src/greet/__init__.py and document it in README.md
  → success: dogfood-6i2.1 closed in beads
iteration 2: dogfood-6i2.2 Make the src-layout greet package importable when running python -m pytest
  → stalled: dogfood-6i2.2 is still open and nothing was committed
dogfood-6i2.2 did not finish (stalled: dogfood-6i2.2 is still open and nothing was committed)
the herdr workspace wG is left open

stopped after 2 iterations: dogfood-6i2.2 did not finish (stalled: dogfood-6i2.2 is still open and nothing was committed)
```

Read `iter-002.term`, fix whatever it shows, and run again. The first issue stays closed and the second is claimable, so nothing is repeated.

### Nothing ready is two opposite things

The loop also stops when `bd ready` offers nothing, which means either that everything is closed or that everything left is stuck behind something. milhouse tells them apart by looking at the epic's children:

```console
$ milhouse run docs/tasks/farewell.md
working on branch milhouse/farewell
created herdr workspace wG (milhouse:farewell)
using existing epic dogfood-6i2: Add a farewell function
nothing is ready but 3 issue(s) are unfinished (dogfood-6i2.1, dogfood-6i2.2, dogfood-6i2.3)
the herdr workspace wG is left open

stopped after 0 iterations: nothing is ready but 3 issue(s) are unfinished (dogfood-6i2.1, dogfood-6i2.2, dogfood-6i2.3)
```

That exits `9`, not `0`. A run that did no work has to be distinguishable from one that finished, or a script branching on the exit code cannot tell. Hitting the iteration budget exits `9` too.

The workspace is deliberately left open so the panes can be inspected ([ADR 0005](decisions/0005-milhouse-owns-the-loop.md)).

### Epics grow while you run them

An agent that spots work outside its issue is told to file it rather than do it ([ADR 0013](decisions/0013-iteration-prompt-contract.md)), so an epic can gain issues while the loop is working through it. In a dogfood run an agent working the third issue filed a fourth, and the loop picked it up next.

This is intended, and it means **a run is not guaranteed to terminate on its own**: agents can add issues as fast as the loop closes them. Two things bound it. The policy stops at the first iteration that does not succeed, and `--max-iterations` bounds the rest, which is why it has a default rather than being optional.

### `--dry-run`

Shows exactly what a run would do, including the prompt it would send, and starts nothing:

```console
$ milhouse run docs/tasks/hello.md --dry-run
dry run — no agents will be started
task      file:docs/tasks/hello.md
title     Add a hello command
branch    milhouse/hello
agent     claude
budget    50 iterations for one run
verify    (none — a closed issue is taken on trust)
run dir   /home/agent/code/github.com/kris-steinhoff/milhouse/.milhouse/runs/hello

not decomposed yet; the planning agent would be sent:

    You are planning one unit of work for the milhouse orchestrator. Your entire job
    this session is to decompose the task below into issues and write them to a
    file. You are not implementing anything.
    …
```

Once the task is decomposed, `--dry-run` prints the issue the next iteration would claim and the prompt it would get. It is the cheapest way to see the effect of a prompt or config change.

## `milhouse plan`

Decompose a task and stop. Runs the planning agent, shows what it proposes, creates the issues once you approve, and does not start the loop.

```
milhouse plan <task> [--yes] [--agent KIND] [--workspace ID] [--repo PATH]
```

Running it against a task that already has an epic prints the existing tree instead of planning it a second time:

```console
$ milhouse plan docs/tasks/farewell.md
created epic dogfood-6i2 with 3 issues
  [ ] dogfood-6i2.1  Add goodbye(name) to src/greet/__init__.py and document it in README.md  (open)
  [ ] dogfood-6i2.2  Make the src-layout greet package importable when running python -m pytest  (open)
  [ ] dogfood-6i2.3  Add tests/test_greet.py covering hello and goodbye  (open)
```

Run it again and it prints the existing tree rather than planning a second time:

```console
$ milhouse plan docs/tasks/farewell.md
file:docs/tasks/farewell.md is already decomposed as dogfood-6i2.
  [ ] dogfood-6i2.1  Add goodbye(name) to src/greet/__init__.py and document it in README.md  (blocked)
  [ ] dogfood-6i2.2  Make the src-layout greet package importable when running python -m pytest  (blocked)
  [ ] dogfood-6i2.3  Add tests/test_greet.py covering hello and goodbye  (open)
```

Worth noticing in that decomposition: only `goodbye` and the tests were asked for. The planning agent read the repository, found that the `src/` layout made `greet` unimportable under `python -m pytest`, and filed that as its own issue blocking the tests. Reading the code before decomposing is the reason the prompt insists on it ([prompts](prompts.md#the-plan-format)).

The planning agent never creates issues itself. It writes `.milhouse/runs/<task>/plan.json` and milhouse creates them, which is what makes the approval real rather than advisory ([ADR 0006](decisions/0006-planning-agent-proposes-milhouse-creates.md)). The format is in [prompts](prompts.md#the-plan-format), and the file is plain JSON, so editing it and re-running is a reasonable way to fix a bad decomposition.

## `milhouse status`

The issue tree and this run's iteration history. Reads beads and the run state; starts nothing and changes nothing.

```
milhouse status <task> [--repo PATH]
```

Before a task is decomposed:

```console
$ milhouse status docs/tasks/hello.md
task    file:docs/tasks/hello.md
epic    (not decomposed yet — run `milhouse plan`)
```

Once a run is under way it also reports the branch, the herdr workspace and pane, any claim or lock left behind by an unfinished run, and one line per iteration from `events.jsonl`:

```console
$ milhouse status docs/tasks/farewell.md
task    file:docs/tasks/farewell.md
epic    dogfood-6i2  Add a farewell function
branch  milhouse/farewell
herdr   workspace wY, pane wY:p3

  [x] dogfood-6i2.1  Add goodbye(name) to src/greet/__init__.py and document it in README.md  (closed)
  [ ] dogfood-6i2.2  Make the src-layout greet package importable when running python -m pytest  (open)
  [ ] dogfood-6i2.3  Add tests/test_greet.py covering hello and goodbye  (open)

iterations (2)
    1  success   dogfood-6i2.1  dogfood-6i2.1 closed in beads
    2  stalled   dogfood-6i2.2  dogfood-6i2.2 is still open and nothing was committed
```

The history spans every invocation against this task, not just the last one, because it is an append-only log ([ADR 0014](decisions/0014-step-is-the-primitive.md)).

## End-to-end check

The manual check that the loop really works. It needs eyes on it, because the thing being verified — that the context is fresh every iteration — is only visible in the pane.

```sh
milhouse doctor                                        # all required rows green
milhouse run docs/tasks/hello.md --dry-run             # prompts look right
milhouse plan docs/tasks/hello.md                      # inspect the issue tree
bd list --metadata-field milhouse_task=file:docs/tasks/hello.md --json
milhouse step docs/tasks/hello.md --attach             # one iteration, watched
milhouse step docs/tasks/hello.md --attach             # and another
```

Watch for:

1. A workspace named `milhouse:hello` appears.
2. The pane shows the agent starting and working.
3. **The pane returns to a shell prompt when the step ends.** This is the one that matters: it is what proves the next iteration gets a fresh context window.
4. The issue closes in `bd`, and `milhouse status` shows both iterations.

Then trigger a permission prompt deliberately and confirm herdr reports `blocked`, and that milhouse stops with the workspace to attach to rather than sitting there.

Finally, run `milhouse step` twice at once in two terminals and confirm the second refuses with exit code `10`.

## Exit codes

Stable, and safe to branch on in a script.

| Code  | Error                    | Means                                                             |
| ----- | ------------------------ | ----------------------------------------------------------------- |
| `0`   | —                        | Success.                                                          |
| `1`   | `MilhouseError`          | An expected failure with no more specific category.               |
| `2`   | `ConfigError`            | `.milhouse/config.toml`, an env var, or a flag is invalid.        |
| `3`   | `SourceError`            | The task definition could not be resolved.                        |
| `4`   | `TrackerError`           | `bd` failed, or the beads database is missing.                    |
| `5`   | `HerdrError`             | `herdr` failed, or the server is unreachable.                     |
| `6`   | `AgentError`             | An agent could not be started, prompted, or exited.               |
| `7`   | `MissingDependencyError` | A required tool is not on `PATH`. Also `doctor`'s failure code.   |
| `8`   | `ProcessError`           | A subprocess failed in a way no caller translated.                |
| `9`   | `LoopAbortedError`       | A run stopped before finishing the epic. State is left to resume. |
| `10`  | `RunLockedError`         | Another milhouse process is already working this task.            |
| `130` | `UserAbortError`         | Interrupted, or a confirmation was declined.                      |

Every error prints one line on stderr, plus a remedy line when there is something specific to try.
