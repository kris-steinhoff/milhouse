---
name: commit-messages
description: Use when writing a git commit message, amending one, or squashing commits in this repository. Commit messages follow the Conventional Commits specification. Trigger whenever you are about to run `git commit`, prepare a commit body, or draft a message for someone else to commit.
---

# Commit Messages

This repository uses [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/). Every commit message must parse under that spec.

## Format

```
<type>(<optional scope>): <description>

<optional body>

<optional footers>
```

## Types

| Type       | Use for                                                                 |
| ---------- | ----------------------------------------------------------------------- |
| `feat`     | a new capability visible to a user of the package or CLI                 |
| `fix`      | a bug fix                                                               |
| `docs`     | documentation only, including README, docstrings, and skills             |
| `refactor` | code change that neither fixes a bug nor adds a feature                  |
| `test`     | adding or correcting tests                                              |
| `perf`     | a change made for performance                                           |
| `build`    | packaging, dependencies, `pyproject.toml`, lockfiles                     |
| `ci`       | CI configuration and workflow files                                      |
| `chore`    | maintenance that fits nothing above                                      |
| `revert`   | reverting a previous commit                                              |

## Scope

The scope is optional. Use it when the change is confined to one area, and pick the name from the source tree or the concept, not the file path. Examples: `runner`, `tracker`, `herdr`, `planner`, `cli`, `config`, `doctor`, `prompts`, `sources`.

```
fix(tracker): stop claiming issues that are already in progress
```

## Description

- Imperative mood: "add", not "added" or "adds".
- Lowercase first letter, no trailing period.
- Under 72 characters including the type and scope.
- Say what the change does, not what file it touches.

## Body

Include a body when the reason for the change is not obvious from the description. Explain the motivation and the contrast with previous behavior. Wrap at a reasonable width and separate it from the description with a blank line.

## Breaking changes

Mark a breaking change with `!` before the colon, and describe it in a `BREAKING CHANGE:` footer:

```
feat(cli)!: require an explicit task source argument

BREAKING CHANGE: `milhouse run` no longer defaults to the repository's open
issues. Pass a task source such as `gh:owner/repo#123`.
```

## Footers

- Reference beads issues with a `Refs:` footer, one id per line or comma-separated: `Refs: milhouse-42`.
- Keep any co-author trailers at the very end of the message.

## Examples

```
feat(sources): add gh: task source for GitHub issues
```

```
fix(runner): treat a pane exit code of 130 as an interrupt, not a failure

herdr returns 130 when the human presses ctrl-c in a pane. The loop was
classifying that as an agent failure and retrying the same issue.

Refs: milhouse-17
```

```
docs: replace invented examples with captured output
```

## Rules

- One logical change per commit. If the description needs "and", consider splitting.
- Never invent a type outside the table above.
- Do not commit unless the user asked for it. This skill governs the message, not the decision to commit.
