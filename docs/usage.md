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
