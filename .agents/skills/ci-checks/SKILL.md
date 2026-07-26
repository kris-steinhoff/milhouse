---
name: ci-checks
description: Use when changing what this repository checks — adding or removing a linter, formatter, type checker, or test gate, editing .github/workflows/ci.yml or .pre-commit-config.yaml, or bumping a tool version. GitHub Actions is the canonical suite and pre-commit is an optional local mirror, so the two have rules about how they stay in step.
---

# CI Checks

`.github/workflows/ci.yml` is the canonical check suite. `.pre-commit-config.yaml` is an optional local mirror of it.

That ordering decides everything else in this file. A check that only runs in pre-commit does not really run: contributors are not required to install the hooks, and CI never sees the result. A check that only runs in CI is fine, just slower to discover.

## The Rules

1. **Every check exists as a CI job.** If a check is worth enforcing, it belongs in `ci.yml` first. Adding it to `.pre-commit-config.yaml` alone is not enough.
2. **CI does not run pre-commit.** No `pre-commit run --all-files` job. Each check is its own job, running the tool directly, so the GitHub UI names the failure and reruns it in isolation.
3. **Every CI job annotates.** A failure must land on the file and line that caused it, not only in the log. See [Annotations](#annotations).
4. **pre-commit mirrors CI where mirroring is easy.** If a check is awkward to express as a hook, leave it out of pre-commit and note the gap in the table below. A missing local hook costs a slower feedback loop. A wrong one costs trust.
5. **pre-commit stays optional.** Nothing in the contributor workflow may require it. It is still worth installing, because `uv run pre-commit install` also wires the beads hooks.

## The Mapping

| CI job            | Local hook                                                                 | Version comes from              |
| ----------------- | -------------------------------------------------------------------------- | ------------------------------- |
| `ruff`            | `ruff-check`, `ruff-format` (local hooks calling `uv run ruff`)            | `uv.lock`                       |
| `ty`              | `ty` (pre-push)                                                            | `uv.lock`                       |
| `test (py3.x)`    | `pytest` (pre-push, one interpreter instead of three)                      | `uv.lock`                       |
| `prettier`        | `prettier` (pre-commit provisions node itself)                             | pinned in two places, see below |
| `hygiene`         | the `pre-commit/pre-commit-hooks` block                                    | independent, no shared version  |
| `commit-messages` | `conventional-pre-commit` (commit-msg)                                     | independent, no shared version  |
| `package`         | none — building a wheel and installing it clean is too slow for a git hook | n/a                             |
| none              | the `beads-*` hooks — they sync issue data, they do not check the tree     | n/a                             |

## Versions

Python tooling has one source of truth: `uv.lock`. CI runs `uv sync --locked` and then `uv run <tool>`, and the local hooks are `language: system` hooks that also run `uv run <tool>`. There is no version to copy, and `pre-commit autoupdate` must not take ruff, ty, or pytest back over from a pinned rev. Bump them with `uv lock --upgrade-package <name>`.

Prettier is the one exception, because it comes from npm and cannot be resolved out of `uv.lock`. It is pinned twice, and both must move together:

- `PRETTIER_VERSION` in the `prettier` job in `ci.yml`
- `additional_dependencies: [prettier@…]` in `.pre-commit-config.yaml`

Which files prettier touches is not duplicated: `.prettierignore` is read by prettier itself, in CI and locally alike. Put exclusions there, never in the hook's `exclude:` key.

The remaining pinned revs (`pre-commit-hooks`, `conventional-pre-commit`) exist only locally. CI implements those checks itself, so `pre-commit autoupdate` can move them freely.

## Annotations

In order of preference:

1. **The tool's own GitHub output format**, when it has one. `ruff check --output-format=github`, `ruff format --check --output-format=github`, `ty check --output-format=github`.
2. **A plugin that emits workflow commands**, when the tool has no such flag. `pytest-github-actions-annotate-failures` does this for pytest, and activates only when `GITHUB_ACTIONS=true`.
3. **Echoing the workflow command from the step**, when neither exists:

   ```bash
   echo "::error file=$file,line=$line,title=short label::What is wrong and how to fix it."
   ```

   Parse the tool's output for paths and line numbers. Set `FORCE_COLOR: "0"` on any step whose output gets parsed, since the workflow sets `FORCE_COLOR: "1"` globally and ANSI escapes break the parsing.

A step that annotates must still exit non-zero. Annotations decorate a failure, they do not report it.

## Adding A Check

1. Add the job to `ci.yml`. Give it a short lowercase name that reads well in the checks list.
2. Make it annotate, by the order above.
3. Let independent checks in one job keep running after the first failure with `if: ${{ !cancelled() }}`, so one push surfaces every problem.
4. Run it locally the way CI runs it, and confirm it passes on a clean tree before pushing.
5. Mirror it in `.pre-commit-config.yaml` if that is straightforward. If it is not, skip the hook.
6. Add the row to the mapping table above.

## Removing A Check

Remove the CI job first, then the local hook. A hook with no job behind it is exactly the drift this file exists to prevent.
