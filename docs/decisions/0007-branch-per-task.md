# 0007 — One branch per task definition

**Status:** accepted

## Context

[ADR 0004](0004-outcome-from-beads-and-git.md) classifies an iteration partly on "`HEAD` moved", which is meaningless without saying which branch. And fifty unattended iterations committing to `main` is not viable.

The options were: branch per task, branch per issue, or a worktree per run.

## Decision

**Branch per task**, created once at the start of a run:

```sh
git checkout -b milhouse/<task-slug>     # or checkout, if it already exists
```

Controlled by `[git] branch_strategy`, default `task`. Setting it to `current` disables branching entirely and commits wherever the repo already is, which is the right setting when milhouse is itself running inside a worktree someone else created.

The branch name is recorded in `state.json`, so resuming a run returns to the same branch.

## Consequences

- One reviewable branch per task, which maps onto one pull request. Branch per issue would produce a dozen stacked branches per task and a merge problem milhouse has no opinion about.
- `HEAD` in [ADR 0004](0004-outcome-from-beads-and-git.md) is unambiguous: it is the tip of this branch.
- milhouse creates the branch and never merges, pushes, or deletes it. Getting the work to `main` is a human's call.
- A dirty working tree at run start will make the checkout fail, and milhouse reports that rather than stashing. Losing someone's uncommitted work to an unattended loop is the worst possible failure.

## Not chosen: worktree per run

`herdr worktree create` makes this cheap, and it would answer [ADR 0009](0009-permission-posture.md) at the same time by isolating an unattended agent from the working copy the human is using.

It is not the default because it changes where the run happens: `.milhouse/runs/` and the beads database both live in the worktree, so `milhouse status` from the main checkout would not see the run. That is solvable, and worth doing before the first overnight run, but it is not v1. In the meantime, a worktree can be used today by creating one, running milhouse inside it, and setting `branch_strategy = "current"`.
