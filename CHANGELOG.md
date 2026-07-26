# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Package skeleton: `pyproject.toml` (uv/hatchling, Python 3.11+), the `milhouse` console script, ruff (including docstring `D` rules) and pytest configuration.
- `milhouse doctor` — verifies `bd`, `herdr`, `git`, `gh`, and the configured agent, checks the repo has a beads database, and confirms the herdr server is running and protocol-compatible.
- `milhouse --version` and `--verbose`.
- Layered configuration (defaults < `.milhouse/config.toml` < environment < flags), documented in [docs/configuration.md](docs/configuration.md).
- Error hierarchy with documented, stable exit codes.
- Documentation skeleton under `docs/`, and the first ADRs.
- Task definition sources: local markdown files (`docs/tasks/hello.md`, `file:...`) and GitHub issues (`gh:owner/repo#123`, `gh:123`, or an issue URL), each deriving the stable `task_id` that links a task to its epic.
- `tracker/beads.py`, the `bd` wrapper: find or create the epic for a task, create children with their dependencies, claim the next ready issue, and release, block, note, or close one.
- `herdr.py`, a narrow client over the herdr CLI: workspaces, panes, and the agent lifecycle.
- `runner.py`, which runs one iteration's agent from start to exit, capturing the prompt and the pane transcript into `.milhouse/runs/<task>/`.
- `outcome.py`, the pure classification of what an iteration achieved.
- The two prompt templates, `plan.md.j2` and `iterate.md.j2`, documented in [docs/prompts.md](docs/prompts.md).
- `planner.py`: the plan format, its validation (unique keys, resolvable and acyclic dependencies), and the creation pass. The planning agent proposes a file; milhouse creates the issues.
- `loop.py`, the ralph loop and its guardrails: the iteration ceiling, the per-issue attempt cap, stall detection, the `--on-blocked` policy, crash reconciliation on startup, and teardown that reverts the in-flight claim on SIGINT/SIGTERM without closing anyone's pane.
- `milhouse run`, `milhouse plan`, and `milhouse status`, including `--dry-run`, which renders the prompts and prints the plan without starting an agent.
- `docs/tasks/hello.md`, the example task definition the quickstart and the end-to-end check both point at.
- Shell completion: `milhouse --install-completion` (and `--show-completion`) for bash, zsh, fish, and PowerShell, plus value completion for `<task>`, `--repo`, `--agent`, `--on-blocked`, `--branch-strategy`, and `--workspace`. Completion answers from the filesystem — no GitHub call for a `gh:` spec, no herdr call for a workspace id — so pressing tab costs nothing and works with the server down.

### Verified

- The loop was driven end to end against a live `claude` in a herdr pane: a task definition decomposed into three issues, then three consecutive `success` iterations, each with a freshly started agent, each closing its issue in beads and committing with the issue id in the message. Captured in [docs/usage.md](docs/usage.md#what-each-iteration-does).
- That run also confirmed epics grow while they run. An agent filed a fourth issue it had noticed, exactly as the iteration prompt asks, and the loop picked it up. `--max-iterations` is what bounds a run, not the plan.

### Fixed

Everything here was found by dogfooding milhouse against a real repository rather than by testing against fakes.

- A run whose issues all ended up `blocked` reported "the epic is finished" and exited `0`. An empty `bd ready` queue means either "everything is closed" or "everything left is stuck", and the two are opposites. The loop now checks the epic's children, names the unfinished issues, and exits `9`.
- The default `[agent] exit_keys` used `c-d`, which herdr rejects with `invalid_key`. The short forms are inconsistent — `c-c` is accepted but `c-d` is not — so the default is now spelled `ctrl+c`, `ctrl+c`, `ctrl+d`. The symptom was an agent that appeared to refuse to quit, at the end of a turn that had otherwise succeeded.
- Documented, in [troubleshooting](docs/troubleshooting.md), why `--dangerously-skip-permissions` makes an agent produce nothing: it shows a one-time consent screen that an unattended agent cannot answer, and the turn settles normally with no output.

[Unreleased]: https://github.com/kris-steinhoff/milhouse/commits/main
