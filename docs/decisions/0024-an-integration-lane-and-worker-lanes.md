# 0024 — A concurrent run has an integration lane and a worker lane per issue

**Status:** accepted. Amends [ADR 0023](0023-a-run-has-one-lane.md).

## Context

[ADR 0023](0023-a-run-has-one-lane.md) keys a run's lane by its target, and says in its last consequence that `--count N` will have to revisit that. This is the revisit.

One key was enough while a run worked one issue at a time, because the unit of work and the unit of review were the same branch. They stop being the same the moment two turns run at once, and three entries in the [Still open](README.md#still-open) list become load-bearing together:

- **What a concurrent run keys its lanes by.** 0023 left this open by name.
- **Landing the lanes.** Nothing in milhouse merges anything, and 0023 avoided needing to by having one base branch.
- **When to reap.** The serial loop waits on each turn, so reaping never comes up. A concurrent one has to find out that a lane has settled somehow.

The ready queue already supplies the parallelism, since `bd ready` offers an issue only when every blocker is closed, so any two issues it offers are independent by construction. What is missing is not a way to start several agents. It is the answers above.

Nothing here changes what a turn is: one issue, one fresh agent, one context window.

## Decision

### Two levels of lane

**One integration lane, keyed by the target.** `{branch_prefix}{target}`, which is ADR 0023's lane unchanged, and still the one branch a person reviews.

**N worker lanes, keyed by the issue.** `{branch_prefix}{target}/{issue}`, each opened when the issue is dispatched and branched from the integration branch as it stands at that moment. Namespacing under the target is what stops two runs of different targets colliding on a branch name, and what lets `milhouse status` tell a run's worker lane from a `dispatch` lane by looking at it.

`milhouse dispatch` is untouched. Its lane is keyed by the issue, and [ADR 0020](0020-a-lane-is-a-herdr-worktree.md)'s four assignment rules still apply to it. They do not apply to a worker lane, for the reason 0023 gave: a worker lane's base is the integration branch by construction, so two blockers in two lanes never produce two candidate bases and the join refusal cannot fire on the run path. The join question stays open for `dispatch` and is untouched here.

### The run process merges, one at a time

A turn that ends `success` in a worker lane has its branch merged into the integration branch, in the integration lane, by the run process. Merges are serialized: one merge at a time, in the order turns settle, never two at once and never inside a worker lane.

That buys three things. There is one branch to review at the end rather than N. A worker lane opened later already contains its predecessors' work, because it branches from an integration branch that has been moving. And there is exactly one merge order per run, so a failure is reproducible from the report rather than from a race.

**A conflict halts the run and names both branches.** The worker branch survives, the issue stays closed, and the integration lane is left exactly where it was, because the merge is aborted before the halt. Only a person can land it from there, which is why it is a halt rather than a deferral.

An unsuccessful turn is not merged. Its commits stay on its worker branch, where the next attempt at the same issue finds them.

### Fast-forward is allowed, and not forced into a merge commit

When the integration branch has not moved since a worker lane branched from it, the merge fast-forwards. milhouse does not pass `--no-ff`.

The reason is that whether a merge joined anything is exactly the thing that decides whether the integration branch has to be verified again, and git already knows it. After a fast-forward the integration branch has the tree the worker lane already ran `[verify] command` against, so running the gate a second time tests nothing new. After a real merge it has a tree nobody has tested, which is the "N green branches can be red combined" case and the whole reason to test it.

Forcing `--no-ff` would erase that signal. Every issue would then get a gate run it did not need, including in a `--count 1` run, which is supposed to cost what ADR 0023 costs. The alternative would be for milhouse to keep its own private record of whether a merge really joined two histories, which is a worse copy of what `git merge` returns.

History is not a reason to force it either. The mapping from an issue to the shas its turn produced lives in the audit log ([ADR 0021](0021-iteration-history-goes-in-the-beads-audit-log.md)), not in the shape of the commit graph. That is also why the run merges rather than rebases: a rebase would rewrite the shas the audit log names.

### A red integration branch halts, and reverts nothing

If the gate fails on the integration branch after a merge that joined something, the run halts. The merge stays, the issue stays closed, and the failing output goes on the issue as a note.

Reverting would hide work that was genuinely done and leave the branch looking clean when it is not. Re-opening the issue would ask the next agent to fix a combination rather than its own work, which is not what its acceptance criteria describe, and it is not the kind of judgement an unattended agent should be handed.

### Polling, at `[run] poll_ms`

A concurrent run polls its dispatched lanes at `[run] poll_ms` and reaps whatever has settled. This answers "when to reap" for `milhouse run` and for nothing else. `milhouse reap` is unchanged: it still collects whatever is done, is still safe to run at any time, and `until milhouse reap; do sleep 60; done` still works for the dispatch workflow.

herdr's `events.subscribe` is still the better mechanism, and this does not consume the [ADR 0001](0001-shell-out-to-bd-and-herdr.md) revisit. A run watching lanes it started itself, at an interval it configured, is not enough to earn a socket client. Watching lanes somebody else started still is.

### `--count 1` is ADR 0023 exactly

At `--count 1` there are no worker lanes. The run opens its integration lane, works in it, and merges nothing, because there is nothing to merge. Every consequence 0023 lists still holds at that count, including the dirty-tree halt and its reason.

The concurrent path is opt-in so that the serial one is not re-tested by accident.

### `--count N` is the flag, `[run] max_parallel` is the key

The flag is `--count N`, because [ADR 0022](0022-the-loop-is-earned.md), ADR 0023, `run.py` and `milhouse dispatch` already promise that name.

The config key is `[run] max_parallel`, with `MILHOUSE_RUN_MAX_PARALLEL` beside it, plus `[run] poll_ms`. `[run]` is a section of ceilings, `max_iterations` and `max_attempts`, and a key in a config file says what any run of this repository may not exceed rather than what one invocation is doing. `max_parallel = 4` reads that way. `count = 1` reads like a count of something, and does not say of what.

The cost is that the flag and the key no longer share a name the way `--max-iterations` and `max_iterations` do, so both places that document configuration have to carry the mapping explicitly rather than leaving it to be inferred.

## Rationale

The lane key that worked serially was "the unit a person will review". Under concurrency there are two units and they nest: the target is what a person reviews, and the issue is what an agent is given. Two levels of lane is that nesting made literal, which is why this is an amendment to 0023 rather than a contradiction of it. 0023's key is still the integration lane's key, and its argument for why a run needs one reviewable branch is what makes the integration lane the thing worker lanes land in.

Merging as turns settle, rather than at the end, is what keeps 0023's other property. The expensive thing 0023 bought was that a run never has two candidate base branches. Merging preserves it by construction: the integration branch is the only base a worker lane is ever given, and it already contains everything that landed before.

## Alternatives considered

**Merge every worker branch at the end of the run.** Rejected. It leaves every conflict until the point where the run has nothing left to do about it, and a worker lane dispatched late would branch from a base missing all of its predecessors' work, which is the same stale-base problem in a slower disguise.

**Rebase worker branches instead of merging.** Rejected. The audit log records the short shas a turn produced ([ADR 0021](0021-iteration-history-goes-in-the-beads-audit-log.md)) and a rebase rewrites them, so the history would stop matching the record of it.

**Force `--no-ff` so every issue gets a merge commit.** Rejected above. It costs a gate run per issue and destroys the only signal that says which merges need one.

**Keep per-issue lanes under `run`, keyed by the issue alone.** Rejected. This is what ADR 0023 rejected, and namespacing under the target is what makes the difference: without it, two runs of different targets that happen to reach the same issue collide on a branch name, and nothing in `milhouse status` distinguishes a run's lane from a dispatch lane.

**Branch a worker lane from its blocker's worker lane.** Rejected. It reintroduces the two-candidate-base join that 0023 spent a whole ADR removing from the run path, and it makes a worker lane's contents depend on which lanes happened to be open.

**Revert the merge when the integration gate goes red.** Rejected above. Halting with the output on the issue leaves a person the same information and does not throw away work.

**Subscribe to herdr events rather than polling.** The better mechanism, and deferred deliberately. Polling one process's own lanes is a small enough need that adopting a socket client for it would be paying the [ADR 0001](0001-shell-out-to-bd-and-herdr.md) revisit price for the least interesting case.

## Consequences

- **The per-lane bootstrap tax is per issue again, multiplied by the count.** ADR 0023 counted paying it once per run as a real improvement, and this gives that back. A fresh worktree still has no `.venv`, `[verify] command` still runs in the lane, and a `--count 4` run now pays for four of them at once. The underlying problem is unchanged and still [open](README.md#still-open), and it is worse than it was.
- **A dirty tree after a closed issue means something new.** Under ADR 0023 the next iteration inherited the mess, which is why the run halted. With a worker lane per issue nobody inherits it, but uncommitted work is not merged either, so the issue lands less than its close claims. Still a halt, for the new reason.
- **With a gate configured, a concurrent run pays for it twice per merged issue.** Once in the worker lane and once on the integration branch, for every merge that joined anything. That is the price of finding out that two green branches are red together, and it is the only way to find out.
- **A conflict is a mess a serial run could not leave.** A closed issue, a live branch, and an integration branch without its work. The report has to name both branches precisely, because the recovery is entirely by hand.
- **Three lanes can now exist for the same issue**: one from `dispatch` keyed by the issue, one worker lane keyed by the issue under a target, and the integration lane its work lands in. ADR 0023 already said nothing detects the first two and nothing needs to. The lane listing naming what each lane is keyed by matters more now than it did.
- **A crashed concurrent run reconciles better than a serial one.** A worker lane carries the issue id, so `Lanes.locate` finds it and reconciliation can tell an in-flight turn from an orphaned claim. Under ADR 0023 the run's lane carried the target, so the claim was always re-opened.
- **`max_iterations` now bounds turns that are in flight rather than finished.** A dispatched turn is spent whether or not it has been reaped, so the count has to include what is running or a `--count 4` run overshoots its ceiling by three.

## What the first concurrent runs taught

Not yet written. ADR 0022 has a section like this because four watched runs changed what was believed about the serial loop, and the same is owed here before the concurrency work is called done. What to record is the same shape: whether the merges conflicted and on what, whether the integration gate ever caught something the worker lanes did not, what the drain did when a halt fired with turns still in flight, and whether N agents at once produced a failure mode one at a time never did.

Until that is written, every number and every table above is reasoning rather than observation, which is the distinction [ADR 0017](0017-no-loop-until-it-is-earned.md) exists to keep visible.

## Revisit when

The integration gate is observed either catching something real or never catching anything, since the first justifies its doubled cost and the second retires it. Or when the per-lane bootstrap gets an answer, which changes what a count above two is actually worth.
