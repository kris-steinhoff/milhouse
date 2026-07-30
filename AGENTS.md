# Agent Instructions

This is the definitive instruction file for AI agents working on this project. There is no root `CLAUDE.md`: `.claude/CLAUDE.md` imports this file, so every tool reads the same text. Edit this file, not a copy of it.

## Issue Tracking

This project tracks work in **beads** (`bd`). Use the `beads` skill (`.agents/skills/beads/SKILL.md`) for the workflow and `bd prime` for the command reference. Hooks load both in most sessions.

The short version:

```bash
bd ready                # find available work
bd show <id>            # read it before touching it
bd update <id> --claim  # claim it
bd close <id>           # complete it
```

Beads is the source of truth for project work. Do not keep task state in markdown TODO lists, and use `bd remember` rather than memory files.

## Git And Sync Policy

This project is pre-launch with a single developer, so it moves fast. Ignore the conservative git policy that tool-generated blocks assume:

- Commit directly to `main`. No feature branches, no pull requests.
- Committing, `git push`, and `bd dolt push` are all fine without asking. Push both after finishing a unit of work, so issue data does not sit unpublished on one machine.
- Commit messages follow Conventional Commits. See the `commit-messages` skill (`.agents/skills/commit-messages/SKILL.md`), checked by the `commit-messages` CI job and, locally, by `conventional-pre-commit` on `commit-msg`.
- CI is the gate. After pushing, watch the run, and fix what it reports rather than working around it.

## Build And Test

```bash
uv sync                                      # install, including the dev group
uv run pre-commit install                    # optional; also wires the beads hooks
uv run pytest -m "not herdr and not beads"   # the fast suite
uv run ruff check --fix && uv run ruff format
uv run ty check
```

Two pytest markers cover integration tests that need live services. They are excluded from every gate, so run them deliberately:

- `-m herdr` drives a running herdr server (no agents spawned).
- `-m beads` drives a real scratch `bd` database.

## CI

`.github/workflows/ci.yml` is the canonical check suite, and it runs on every push to `main`, on pull requests, and on demand. Each check is its own job, reporting failures as annotations on the offending lines: `ruff` (check and format), `ty`, `prettier`, `hygiene` (whitespace, newlines, conflict markers, file size, TOML syntax), `commit-messages`, `test` on Python 3.11, 3.12, and 3.13, and `package`, which builds the wheel, installs it into a clean environment, and runs the console script.

Watch a run with `gh run watch`, and read a failure with `gh run view --log-failed`.

`.pre-commit-config.yaml` mirrors those checks locally and is optional: it moves CI's answer earlier, it does not decide anything on its own. Installing it is still worth it, because the same command wires the beads hooks. The `ci-checks` skill (`.agents/skills/ci-checks/SKILL.md`) covers how the two stay in step. Read it before touching either file.

## Non-Interactive Shell Commands

`cp`, `mv`, and `rm` may be aliased to `-i` on some systems, which hangs an agent waiting for input that never comes. Always pass the non-interactive form: `cp -f`, `mv -f`, `rm -f`, `rm -rf`, `cp -rf`. Likewise `scp` and `ssh` with `-o BatchMode=yes`, `apt-get -y`, and `HOMEBREW_NO_AUTO_UPDATE=1` for `brew`.

## Architecture And Conventions

`milhouse` works its tracker's ready queue, each issue with a fresh agent in a herdr pane ([ADR 0018](docs/decisions/0018-no-task-milhouse-works-the-ready-queue.md)). One iteration is the primitive and `milhouse step` runs exactly one ([ADR 0014](docs/decisions/0014-step-is-the-primitive.md)). `milhouse run <target>` is the loop over it, which [ADR 0022](docs/decisions/0022-the-loop-is-earned.md) earned once there were watched iterations to write its policy from, and `--count N` works several issues at once, each in a worker lane branched from the run's integration branch and merged back into it ([ADR 0024](docs/decisions/0024-an-integration-lane-and-worker-lanes.md)). Source lives in `src/milhouse/`, and tests mirror it in `tests/`.

The docs are the long form, and are kept current:

- [docs/architecture.md](docs/architecture.md) — the loop, module boundaries, data flow
- [docs/usage.md](docs/usage.md) — every command and flag
- [docs/configuration.md](docs/configuration.md) — `.milhouse/config.toml`
- [docs/prompts.md](docs/prompts.md) — what each template promises the agent
- [docs/decisions/](docs/decisions/README.md) — one ADR per settled decision

Record a settled design decision as an ADR rather than as prose in this file.

<!--
Note for future `bd setup` runs: the managed BEADS INTEGRATION blocks were removed on purpose. Their content duplicates the beads skill, which every session already loads. If a setup command re-injects one here, or recreates a root CLAUDE.md, delete it again rather than letting the guidance fork.
-->
