# 0018 — There is no task; milhouse works the tracker's ready queue

**Status:** accepted, not yet implemented. The docs describe today's behaviour until it lands.

## Context

milhouse was built around a `TaskDefinition`: resolved from a markdown file or a GitHub issue, decomposed by a one-shot planning agent into an epic and its children, linked to that epic by `milhouse_task` metadata ([ADR 0002](0002-link-issues-via-bead-metadata.md)), and worked on a branch named after it ([ADR 0007](0007-branch-per-task.md)).

Three things about that stopped holding.

**Planning is the most opinionated thing milhouse does, and the prompt is a guess.** `plan.md.j2` dictates issue granularity, insists documentation belongs inside each issue rather than after them, and rules on how `blocked_by` should be used. None of that was arrived at by watching runs. It is exactly the objection [ADR 0017](0017-no-loop-until-it-is-earned.md) raised against shipping a loop: a policy nobody has earned, baked in where it is expensive to change.

**The task exists only to scope the ready query.** `bd ready` already takes `--parent`, `--label`, `--label-any`, and `--exclude-type`, and already excludes blocked, deferred, and in-progress issues. The epic was a fence that the tracker can build itself, and `milhouse_task` metadata was the string tying the fence to a file.

**Once planning is manual, the task definition is a second copy of the tracker.** A markdown file describing work that also exists as beads issues is two records that drift, and the only thing reconciling them was a metadata key.

## Decision

**Remove the task.** `milhouse step` takes no task argument. It claims the next ready issue in the repository and works it.

Deleted: `models.TaskDefinition`, `sources/` (`base.py`, `file.py`, `github.py`), `planner.py`, `prompts/plan.md.j2`, `prompts.render_plan`, the `plan` command, `--yes` on `step`, and the epic handling in `session.py`.

What replaces each thing the task provided:

| Was                              | Now                                                                             |
| -------------------------------- | ------------------------------------------------------------------------------- |
| `bd ready --parent <epic>`       | `bd ready`, with an optional configured filter                                  |
| `find_epic` via `milhouse_task`  | nothing — there is no task to link                                              |
| `task.body` as prompt background | the issue's parent description, read from the tracker                           |
| branch `milhouse/<slug>`         | the current branch, then lanes ([ADR 0020](0020-a-lane-is-a-herdr-worktree.md)) |
| `gh:owner/repo#123` as a source  | `bd create --external-ref gh-123`, at planning time                             |

Issues arrive in beads by whatever process the human wants: by hand, from `bd create --graph`, or from an agent session driven by a prompt the human owns rather than one milhouse ships.

The repo-wide ready queue will surface issues that were never meant for an agent, so `[tracker]` gains a filter (a label or a parent) that a repository sets once. It defaults to unfiltered, because a repository whose beads database is only agent work needs no fence.

This retires three ADRs, which stay in the directory as the record of why they existed:

- **[ADR 0002](0002-link-issues-via-bead-metadata.md)**, the metadata link. Nothing left to link.
- **[ADR 0006](0006-planning-agent-proposes-milhouse-creates.md)**, the structural approval guardrail. It answered "how do we stop the planning agent creating issues," and there is no planning agent.
- **[ADR 0007](0007-branch-per-task.md)**, one branch per task. No task to name a branch after.

## Consequences

- **Around 1,200 to 1,500 lines leave**, counting tests and the docs that describe them. `Tracker` drops to five methods: `ready`, `get`, `note`, `release`, `children`.
- **milhouse can no longer bootstrap itself from a markdown file or a GitHub issue.** Getting work into the tracker is now entirely someone else's job. That is the point, and it is also the capability being given up.
- **The discipline `plan.md.j2` enforced becomes the human's.** It required issue descriptions written for an agent with no context, and `iterate.md.j2` still assumes exactly that ([ADR 0013](0013-iteration-prompt-contract.md)). Nothing warns you when descriptions get thin. Iterations just quietly get worse.
- **"Is this finished?" gets weaker.** `nothing_ready` could distinguish "the epic is done" from "everything left is blocked" by looking under one epic. Repo-wide, that becomes "nothing is ready" plus whatever `bd blocked` says.
- **The supervised posture is unchanged.** A step still claims one issue, works it once, and hands back.

## Revisit when

Planning has been done by hand often enough that the prompt would be observed rather than guessed. A planning command can come back, and it should come back with its prompt and its format decided together, the way this one did not.
