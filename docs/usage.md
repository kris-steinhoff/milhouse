# Usage

Every command and flag, with worked examples. Output shown here was captured
from a real run.

## Global options

```
milhouse [--version] [--verbose] <command> [options]
```

| Option            | Meaning                                                            |
| ----------------- | ------------------------------------------------------------------ |
| `--version`       | Print the milhouse version and exit.                               |
| `--verbose`, `-v` | Log every subprocess milhouse runs, to stderr. The debugging tool.  |

## Task definitions

Every command that takes a `<task>` accepts the same spec forms. The spec
determines the `task_id`, which is the stable link between the task and its beads
epic ([ADR 0002](decisions/0002-link-issues-via-bead-metadata.md)).

| Spec                                                  | `task_id`                        | Notes                                          |
| ----------------------------------------------------- | -------------------------------- | ---------------------------------------------- |
| `docs/tasks/hello.md`                                  | `file:docs/tasks/hello.md`       | Relative to the current directory, then the repo root. |
| `file:docs/tasks/hello.md`                             | `file:docs/tasks/hello.md`       | Same thing, said explicitly.                   |
| `/abs/path/hello.md`                                   | `file:docs/tasks/hello.md`       | Made repo-relative when it is inside the repo. |
| `gh:owner/repo#123`                                    | `gh:owner/repo#123`              | Fetched with `gh issue view`.                  |
| `gh:123`                                               | `gh:owner/repo#123`              | Repo inferred from the working directory.      |
| `gh:https://github.com/owner/repo/issues/123`          | `gh:owner/repo#123`              | Paste a URL straight from a browser.           |

A file task definition is any markdown. Its first `#` heading becomes the epic
title (falling back to the filename), and the whole file is handed to the
planning agent verbatim. An empty file is an error: there is nothing to
decompose.

GitHub tasks also set `--external-ref gh-<number>` on the epic, so beads can
round-trip the link.

Renaming a task file changes its `task_id` and orphans the existing epic. See
[troubleshooting](troubleshooting.md#the-task-was-planned-twice).

## `milhouse doctor`

Verify the tools milhouse depends on and the state of the herdr server. Run this
first, and again whenever a run fails in a way that does not make sense.

```
milhouse doctor [--repo PATH]
```

| Option   | Default            | Meaning                                       |
| -------- | ------------------ | --------------------------------------------- |
| `--repo` | the enclosing repo | Repository to check, if not the current one.  |

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
- `warn` — an optional tool is missing. `gh` is only needed for `gh:` task
  sources, and the agent binary only for real runs, so a `warn` on either is fine
  until you need it.
- `FAIL` — a required tool or service is missing. `doctor` exits `7`.

The agent row uses whatever `[agent] kind` is configured, so it checks the agent
you will actually run.

## `milhouse run`

The main entry point. Resolves the task, decomposes it if it has not been
decomposed yet, then loops: claim one ready issue, hand it to a **freshly
started** agent, classify what happened, repeat.

```
milhouse run <task> [options]
```

| Option              | Default                | Meaning                                                                     |
| ------------------- | ---------------------- | --------------------------------------------------------------------------- |
| `--max-iterations`  | `50`                   | Hard ceiling on iterations for the whole run.                               |
| `--max-attempts`    | `3`                    | Failed attempts on one issue before it is marked blocked and skipped.       |
| `--on-blocked`      | `wait`                 | `wait`, `skip`, or `abort` when herdr reports the agent needs a human.      |
| `--agent`           | `claude`               | Agent kind to run. Any kind herdr supports.                                 |
| `--workspace`       | `HERDR_WORKSPACE_ID`   | Reuse this herdr workspace instead of creating one.                         |
| `--branch-strategy` | `task`                 | `task` for one branch per task definition; `current` to stay put.           |
| `--dry-run`         | off                    | Render the prompts and print the plan; start no agents.                     |
| `--attach`          | off                    | Focus the herdr workspace instead of leaving it hidden.                     |
| `--yes`, `-y`       | off                    | Create the proposed issues without asking.                                  |
| `--repo`            | the enclosing repo     | Repository to work in.                                                      |

Everything except `--dry-run`, `--attach`, and `--yes` can also be set in
[`.milhouse/config.toml`](configuration.md).

### Resuming

Re-running `milhouse run` against the same task **is** the resume mechanism. It
re-opens any claim a previous run left behind, keeps the attempt counts, and
carries on ([ADR 0008](decisions/0008-crash-recovery-by-reconciliation.md)).
There is no separate `resume` command.

### What each iteration does

1. `bd ready --parent <epic> --claim --limit 1` — an empty result means the epic
   is finished.
2. Render `iterate.md.j2` for that issue and save it to `iter-NNN.prompt`.
3. `herdr agent start` a **new** agent in the task's pane.
4. `herdr agent prompt --wait` until the turn settles.
5. Capture the pane transcript to `iter-NNN.term`.
6. Exit the agent, returning the pane to a shell prompt.
7. Classify the outcome from beads and git, and record it.

| Outcome   | Means                                          | Issue becomes                    |
| --------- | ---------------------------------------------- | -------------------------------- |
| `success` | The issue is closed in beads.                  | closed                           |
| `partial` | Still open, but `HEAD` moved.                  | re-opened for another attempt    |
| `stalled` | Still open and nothing was committed.          | re-opened for another attempt    |
| `timeout` | The turn did not settle in time.               | re-opened for another attempt    |
| `blocked` | The agent is waiting on a human.               | `blocked` in beads               |
| `error`   | herdr or `bd` failed.                          | re-opened for another attempt    |

Re-opening matters: a claimed issue is `in_progress`, and `bd ready` excludes
those, so an unfinished issue that was simply left alone would never be offered
again.

### How a run ends

The loop stops when `bd ready` offers nothing, and that means one of two
opposite things. milhouse distinguishes them by looking at the epic's children:

```console
$ milhouse run docs/tasks/farewell.md
working on branch milhouse/farewell
using existing epic dogfood-6i2: Add a farewell function
nothing is ready but 3 issue(s) are unfinished (dogfood-6i2.1, dogfood-6i2.2,
dogfood-6i2.3); 2 blocked and needing a human
the herdr workspace wY is still open

stopped after 2 iterations: nothing is ready but 3 issue(s) are unfinished
```

That exits `9`, not `0`. Only "no issues are ready; the epic is finished", with
every child closed, exits `0`. A run that blocks every issue has done no work,
and a script branching on the exit code has to be able to tell.

The workspace is deliberately left open so the panes can be inspected
([ADR 0005](decisions/0005-milhouse-owns-the-loop.md)).

### `--dry-run`

Shows exactly what a run would do, including the prompt it would send, and
starts nothing:

```console
$ milhouse run docs/tasks/hello.md --dry-run
dry run — no agents will be started
task      file:docs/tasks/hello.md
title     Add a hello command
branch    milhouse/hello
agent     claude
caps      50 iterations, 3 attempts per issue, on-blocked wait
run dir   /home/agent/code/github.com/kris-steinhoff/milhouse/.milhouse/runs/hello

not decomposed yet; the planning agent would be sent:

    You are planning one unit of work for the milhouse orchestrator. Your entire job
    this session is to decompose the task below into issues and write them to a
    file. You are not implementing anything.
    …
```

Once the task is decomposed, `--dry-run` prints the issue the next iteration
would claim and the prompt it would get. It is the cheapest way to see the
effect of a prompt or config change.

## `milhouse plan`

Decompose a task and stop. Runs the planning agent, shows what it proposes,
creates the issues once you approve, and does not start the loop.

```
milhouse plan <task> [--yes] [--agent KIND] [--workspace ID] [--repo PATH]
```

Running it against a task that already has an epic prints the existing tree
instead of planning it a second time:

```console
$ milhouse plan docs/tasks/hello.md
file:docs/tasks/hello.md is already decomposed as bd-4rt.
  [x] bd-4rt.1  Add the hello subcommand  (closed)
  [ ] bd-4rt.2  Document the hello subcommand  (open)
```

The planning agent never creates issues itself. It writes
`.milhouse/runs/<task>/plan.json` and milhouse creates them, which is what makes
the approval real rather than advisory
([ADR 0006](decisions/0006-planning-agent-proposes-milhouse-creates.md)). The
format is in [prompts](prompts.md#the-plan-format), and the file is plain JSON,
so editing it and re-running is a reasonable way to fix a bad decomposition.

## `milhouse status`

The issue tree and this run's iteration history. Reads beads and the run state;
starts nothing and changes nothing.

```
milhouse status <task> [--repo PATH]
```

```console
$ milhouse status docs/tasks/hello.md
task    file:docs/tasks/hello.md
epic    (not decomposed yet — run `milhouse plan`)
```

Once a run is under way it also reports the branch, the herdr workspace and
pane, any claim left behind by an unfinished run, and one line per iteration
with its outcome.

## End-to-end check

The manual check that the loop really works. It needs eyes on it, because the
thing being verified — that the context is fresh every iteration — is only
visible in the pane.

```sh
milhouse doctor                                        # all required rows green
milhouse run docs/tasks/hello.md --dry-run             # prompts look right
milhouse plan docs/tasks/hello.md                      # inspect the issue tree
bd list --metadata-field milhouse_task=file:docs/tasks/hello.md --json
milhouse run docs/tasks/hello.md --max-iterations 2 --attach
```

Watch for:

1. A workspace named `milhouse:hello` appears.
2. The pane shows the agent starting and working.
3. **The pane returns to a shell prompt between iterations.** This is the one
   that matters: it is what proves each iteration gets a fresh context window.
4. The issue closes in `bd`.

Then trigger a permission prompt deliberately and confirm herdr reports
`blocked`, milhouse tells you which workspace to attach to, and it waits rather
than counting a failure.

## Exit codes

Stable, and safe to branch on in a script.

| Code  | Error                     | Means                                                               |
| ----- | ------------------------- | ------------------------------------------------------------------- |
| `0`   | —                         | Success.                                                            |
| `1`   | `MilhouseError`           | An expected failure with no more specific category.                 |
| `2`   | `ConfigError`             | `.milhouse/config.toml`, an env var, or a flag is invalid.          |
| `3`   | `SourceError`             | The task definition could not be resolved.                          |
| `4`   | `TrackerError`            | `bd` failed, or the beads database is missing.                      |
| `5`   | `HerdrError`              | `herdr` failed, or the server is unreachable.                       |
| `6`   | `AgentError`              | An agent could not be started, prompted, or exited.                 |
| `7`   | `MissingDependencyError`  | A required tool is not on `PATH`. Also `doctor`'s failure code.     |
| `8`   | `ProcessError`            | A subprocess failed in a way no caller translated.                  |
| `9`   | `LoopAbortedError`        | The loop stopped before finishing the epic. State is left to resume.|
| `130` | `UserAbortError`          | Interrupted, or a confirmation was declined.                        |

Every error prints one line on stderr, plus a remedy line when there is
something specific to try.
