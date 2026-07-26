# milhouse

An agentic AI orchestrator. Point it at a task definition and it decomposes the task into tracked issues, then works through them one issue at a time, each with a fresh agent.

The pieces already existed. milhouse is the thing that wires them together:

- **[beads](https://github.com/steveyegge/beads) (`bd`)** — git-backed, dependency-aware issue tracker. Durable memory, plus `bd ready --claim` for race-free "what do I work on next".
- **[herdr](https://herdr.dev)** — terminal workspace manager. Every agent runs in a herdr pane, so a human can watch it and intervene.
- **`claude`** (or any agent herdr supports) — does one unit of work per iteration.

The defining property of a [ralph loop](https://ghuntley.com/ralph/) is a **fresh context window every iteration**. milhouse starts a new agent in the pane each time and exits it when the turn ends. State lives in beads and git, never in an accumulating chat session.

**One iteration is the unit, and you type each one.** `milhouse step` claims an issue, gives it to a fresh agent, and hands straight back to you. There is deliberately no command that repeats it: the policy a loop needs is the open question, and the way to answer it is to watch real iterations rather than reason about them ([ADR 0017](docs/decisions/0017-no-loop-until-it-is-earned.md)). The seam for one is already there ([ADR 0014](docs/decisions/0014-step-is-the-primitive.md)).

## Prerequisites

| Tool     | Required for                     | Install                                                      |
| -------- | -------------------------------- | ------------------------------------------------------------ |
| `bd`     | everything                       | `brew install beads`, then `bd init` in your repo            |
| `herdr`  | everything (server must be up)   | see [herdr.dev](https://herdr.dev)                           |
| `git`    | everything                       | —                                                            |
| `gh`     | `gh:owner/repo#123` task sources | see [cli.github.com](https://cli.github.com)                 |
| `claude` | real steps (not `--dry-run`)     | see [claude.com/claude-code](https://claude.com/claude-code) |

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

# 2. Write a task definition. Any markdown file will do.
cat > docs/tasks/hello.md <<'EOF'
# Add a hello command

Add a `hello` subcommand that prints a greeting, with a test and a docs entry.
EOF

# 3. See what would happen, without starting an agent.
milhouse step docs/tasks/hello.md --dry-run

# 4. Decompose it into issues and inspect the tree.
milhouse plan docs/tasks/hello.md

# 5. Work one issue, watching the pane. Run it again for the next one.
milhouse step docs/tasks/hello.md --attach
```

`milhouse status docs/tasks/hello.md` shows the issue tree and every iteration so far, at any point.

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
