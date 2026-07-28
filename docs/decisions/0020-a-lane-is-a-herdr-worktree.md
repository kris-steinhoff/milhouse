# 0020 — A lane is a herdr worktree, and herdr is the lane registry

**Status:** accepted and implemented, except for the joins and the landing it leaves open.

## Context

With no task and no epic ([ADR 0018](0018-no-task-milhouse-works-the-ready-queue.md)), the job milhouse is left with is sharp: **take ready issues and hand them to fresh agents, isolated enough to run at the same time.** That needs a working directory per concurrent issue, which means git worktrees.

Three tools could own them, and they are not alternatives. They own different layers:

- **git** owns the worktree. `git worktree add -b <branch> <path> <base>` is the mechanism, stacking included. The other two wrap it.
- **`bd worktree create`** adds a `.gitignore` entry and an assurance the beads database is shared. Its own help says the sharing happens automatically via git common-directory discovery, so the wrapper is not what makes it work. No `--base`, so no stacking.
- **`herdr worktree create --workspace --branch --base --label`** creates the worktree _and_ registers it inside a workspace with its own set of tabs.

herdr's model is containment: a workspace holds many worktrees, the default one being the primary checkout, and each worktree has its own tabs, each tab its own panes. That is already the shape wanted — one workspace per repository, one lane per worktree — rather than something to be built around.

## Decision

**A lane is a herdr worktree, labelled with the issue id.** One workspace per repository holds all of them.

```sh
herdr worktree create --workspace <ws> --branch <b> --base <ref> --label <issue-id>
```

**herdr's worktree, not raw git and not `bd worktree`.** A herdr worktree is not a git worktree, it is a git worktree plus a tab set plus workspace membership, and that container is exactly a lane. Using raw git would mean rebuilding "which tabs and panes belong to which lane" as milhouse bookkeeping, which is the only part herdr was already doing. `bd worktree` is out because it would create worktrees herdr does not know about, which defeats the registry below, and because it ties working-directory layout to the tracker that [ADR 0019](0019-beads-is-the-coordination-layer.md) deliberately kept swappable.

**`herdr worktree list` is the lane registry.** It returns `path`, `branch`, `label`, and the owning workspace per worktree, so milhouse stores no lane state of its own. This is the same rule already applied to issues: milhouse does not keep a copy, it asks the tool that owns it.

- `bd ready --claim` answers _what work_
- `herdr worktree list` answers _what lanes exist, on what branches_
- `herdr tab list` reports `agent_status` per tab, which answers _which lanes are still running_

**Lane assignment follows the dependency graph onto that hierarchy.** An issue with no predecessor among the live lanes gets a new worktree. An issue whose blocker ran in an existing lane gets a **new tab in that lane**, continuing on the same branch, rather than a new worktree branched from it. milhouse finds the predecessor's lane by looking up its issue id among the worktree labels.

Assignment and the base-branch decision are the only steps needing the dependency graph, and they are milhouse's. What is left to milhouse after this is a short list: assign the lane, decide the base, classify the outcome scoped to the lane, and verify in it.

**Concurrency splits the primitive rather than introducing a loop.** `runner.run_turn` blocks until the agent settles, and the run lock is repo-wide ([ADR 0015](0015-one-run-at-a-time.md)). Both go. In their place:

- `milhouse dispatch` claims up to N ready issues, sets up their lanes, starts the agents, and returns
- `milhouse reap` checks each lane's agent status and runs classify, verify, and settle for the ones that have finished
- `milhouse step` stays as dispatch-one-and-wait

This keeps every pure function intact and does not require a repetition policy, so [ADR 0017](0017-no-loop-until-it-is-earned.md) still holds. The run lock becomes per-lane, and `bd ready --claim` is what makes two dispatchers safe.

## What implementation corrected

The decision held. Two things said about herdr above did not, and both made the result better rather than worse.

**A worktree gets a workspace of its own, not a tab set inside the repository's.** The context describes "one workspace per repository, one lane per worktree", which is not herdr's containment: `herdr worktree create --workspace <ws>` reads `--workspace` as _which repository to branch from_ and opens the new checkout in a **new** workspace, labelled with `--label`. So the repository's workspace is the source, no agent ever runs in it, and a lane is a workspace of its own. `herdr worktree list` returns `path`, `branch`, and `open_workspace_id` but carries the repository's name as its `label`, so the registry is that list joined to `herdr workspace list` — the issue id is on the workspace. A stacked issue is still a tab, labelled with its own id, inside its predecessor's lane workspace.

**Lanes did not need ignoring.** herdr checks linked worktrees out under `~/.herdr/worktrees/<repo>/<branch>`, outside the repository, so the untracked-files problem never arises. milhouse still writes a `.git/info/exclude` entry for any lane that lands inside the repo, because the failure it prevents is silent and the guard is three lines.

A third thing the split needed and this ADR did not name: **a dispatched turn has to survive the process that started it.** `dispatch` returns while the agent is working, so `reap` — possibly in another process, possibly much later — has to know which lane, which iteration number, and where `HEAD` was before it began. That goes in the audit log as a `dispatch` entry, next to the `claim` it belongs to ([ADR 0021](0021-iteration-history-goes-in-the-beads-audit-log.md)). It is also what makes teardown safe: a claim that has been dispatched is no longer the dispatcher's to re-open.

And **reconciliation gets the test this ADR promised**. "An issue that is `in_progress` with no live lane carrying its id is an orphaned claim" is now decidable, so a claim whose lane is live is left for `reap` and one whose lane is gone is re-opened.

## Consequences

- **`GitRepo` must become path-bound.** `head()`, `commits_between()`, and `is_dirty()` are repo-root-bound today, which under concurrency would attribute one lane's commits to another and corrupt `outcome.classify` ([ADR 0004](0004-outcome-from-beads-and-git.md)). This is mechanical, it touches the whole classification path, and it is the first safe piece of work. It also makes attribution strictly better than today, where a human committing in another terminal pollutes the reading.
- **Lanes must be ignored by git.** A worktree checked out inside the repository and not ignored appears as untracked files in every other lane's `is_dirty()` check.
- **Every lane pays a bootstrap tax.** A fresh worktree has no `.venv` and no `node_modules`, so `[verify] command` needs a per-lane setup command to run against. Without one, verification fails for environmental reasons rather than real ones.
- **Integration becomes a problem milhouse did not previously have.** Serial work on one branch could not produce two green changes that are red together. N lanes can. `bd merge-slot` is the coordination primitive for it, but it does not perform the merge.
- **[ADR 0001](0001-shell-out-to-bd-and-herdr.md) is now at its stated revisit condition.** Waiting on N lanes over the CLI is N blocking processes where the socket API's `events.subscribe` would watch all of them from one connection. That ADR named concurrency as the trigger, and this is it.

## Deliberately not decided

- **Joins.** `--base` takes one ref, and an issue depending on two blockers that ran in separate lanes has two. `bd ready` handles the timing, since the issue does not surface until both close, but the base branch is an open decision. This is the first thing that will bite.
- **Landing the lanes.** Lanes stay as branches. milhouse reports them, a human merges them. Building a merge queue before watching a single parallel run is the guessing [ADR 0017](0017-no-loop-until-it-is-earned.md) exists to prevent. If a landing iteration turns out to be needed, it is a second policy over the same primitive.
