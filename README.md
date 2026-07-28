# milhouse

An agentic AI orchestrator. It takes your tracker's ready queue and works through it one issue at a time, each with a fresh agent.

The pieces already existed. milhouse is the thing that wires them together:

- **[beads](https://github.com/steveyegge/beads) (`bd`)** — git-backed, dependency-aware issue tracker. Durable memory, plus `bd ready --claim` for race-free "what do I work on next".
- **[herdr](https://herdr.dev)** — terminal workspace manager. Every agent runs in a herdr pane, so a human can watch it and intervene.
- **`claude`** (or any agent herdr supports) — does one unit of work per iteration.

The defining property of a [ralph loop](https://ghuntley.com/ralph/) is a **fresh context window every iteration**. milhouse starts a new agent in the pane each time and exits it when the turn ends. State lives in beads and git, never in an accumulating chat session.

**One iteration is the unit, and you type each one.** `milhouse step` claims an issue, gives it to a fresh agent, and hands straight back to you. There is deliberately no command that repeats it: the policy a loop needs is the open question, and the way to answer it is to watch real iterations rather than reason about them ([ADR 0017](docs/decisions/0017-no-loop-until-it-is-earned.md)). The seam for one is already there ([ADR 0014](docs/decisions/0014-step-is-the-primitive.md)).

**Getting work into the tracker is your job.** milhouse does not decompose anything and has no planning agent. Issues arrive in beads however you like — by hand, from `bd create --graph`, or from an agent session driven by a prompt you own — and milhouse works whatever `bd ready` offers ([ADR 0018](docs/decisions/0018-no-task-milhouse-works-the-ready-queue.md)).

## Prerequisites

| Tool     | Required for                   | Install                                                      |
| -------- | ------------------------------ | ------------------------------------------------------------ |
| `bd`     | everything                     | `brew install beads`, then `bd init` in your repo            |
| `herdr`  | everything (server must be up) | see [herdr.dev](https://herdr.dev)                           |
| `git`    | everything                     | —                                                            |
| `claude` | real steps (not `--dry-run`)   | see [claude.com/claude-code](https://claude.com/claude-code) |

`milhouse doctor` checks all of them.

## Install

```sh
uv tool install --editable .
milhouse --install-completion   # optional: tab completion for bash, zsh, fish, PowerShell
```

## Quickstart

```sh
# 1. Confirm the tools are there and the herdr server is running.
milhouse doctor

# 2. Put some work in the tracker. Descriptions are written for an agent
#    with no context, because that is exactly what will read them.
bd create "Add a hello command" --type epic
bd create "Add the hello subcommand" --parent bd-1 \
  --description "Add \`hello\` to cli.py." \
  --acceptance "\`milhouse hello\` prints a greeting."

# 3. See which issue is next and the prompt it would get, without starting
#    an agent.
milhouse step --dry-run

# 4. Work one issue, watching the pane. Run it again for the next one.
milhouse step --attach
```

`milhouse status` shows what is in scope and every iteration so far, at any point.

## Documentation

- [Usage](docs/usage.md) — every command and flag, with worked examples
- [Configuration](docs/configuration.md) — `.milhouse/config.toml` reference
- [Architecture](docs/architecture.md) — the loop, module boundaries, data flow
- [Prompts](docs/prompts.md) — what each template promises the agent, and why
- [Troubleshooting](docs/troubleshooting.md) — blocked agents, stale claims, run artifacts
- [Decisions](docs/decisions/README.md) — one ADR per settled design decision

## Status

Alpha, and installed locally rather than published to PyPI. The step works and is meant to be typed by hand. The prompts, and the policy a loop over it would need, are still being learned by observation, as the ralph methodology expects.

## License

[MIT](LICENSE)
