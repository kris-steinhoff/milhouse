# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Package skeleton: `pyproject.toml` (uv/hatchling, Python 3.11+), the
  `milhouse` console script, ruff (including docstring `D` rules) and pytest
  configuration.
- `milhouse doctor` — verifies `bd`, `herdr`, `git`, `gh`, and the configured
  agent, checks the repo has a beads database, and confirms the herdr server is
  running and protocol-compatible.
- `milhouse --version` and `--verbose`.
- Layered configuration (defaults < `.milhouse/config.toml` < environment <
  flags), documented in [docs/configuration.md](docs/configuration.md).
- Error hierarchy with documented, stable exit codes.
- Documentation skeleton under `docs/`, and the first ADRs.
- Task definition sources: local markdown files (`docs/tasks/hello.md`,
  `file:...`) and GitHub issues (`gh:owner/repo#123`, `gh:123`, or an issue
  URL), each deriving the stable `task_id` that links a task to its epic.
- `tracker/beads.py`, the `bd` wrapper: find or create the epic for a task,
  create children with their dependencies, claim the next ready issue, and
  release, block, note, or close one.
- `herdr.py`, a narrow client over the herdr CLI: workspaces, panes, and the
  agent lifecycle.
- `runner.py`, which runs one iteration's agent from start to exit, capturing
  the prompt and the pane transcript into `.milhouse/runs/<task>/`.
- `outcome.py`, the pure classification of what an iteration achieved.

[Unreleased]: https://github.com/kris-steinhoff/milhouse/commits/main
