# 0023 — A run has one lane, keyed by its target

**Status:** accepted. Amends [ADR 0020](0020-a-lane-is-a-herdr-worktree.md).

## Context

[ADR 0020](0020-a-lane-is-a-herdr-worktree.md) makes a lane a herdr worktree labelled with the issue id, and assigns lanes with four rules over the dependency graph. Two of those rules are a problem once a loop exists ([ADR 0022](0022-the-loop-is-earned.md)).

**Independent siblings fork.** An issue with no blocker in a live lane gets a worktree of its own, branched from the primary checkout. Under `dispatch` that is exactly right, because the unit a person reviews is the issue. Under a run it means a finished epic exists as several unmerged branches with nothing joining them. Landing them is [deliberately undecided](0020-a-lane-is-a-herdr-worktree.md#deliberately-not-decided), so a run would produce a result nobody has a procedure for assembling. An epic that is "done" in nine places is not done.

**Joins are refused.** An issue depending on two blockers that ran in separate lanes has two candidate base branches and no rule picking between them, so milhouse raises rather than guessing. 0020 called that the first thing that will bite. A refusal that fires while somebody is watching is a stop. The same refusal at three in the morning is a wasted night, and it is reachable from an ordinary diamond in an epic.

Both follow from keying a lane by the issue. Neither is a reason to change what `dispatch` does.

## Decision

**A lane is keyed by the unit a person will review.** For `dispatch` that is the issue, unchanged. For `run` it is the target.

`milhouse run <target>` opens one lane on `{branch_prefix}{target-id}` and every iteration in the run happens in it, with a freshly started agent each time. The run lock is keyed by the target too, so one run holds one lock rather than accumulating one per issue worked.

The fresh context window is untouched by this. It comes from starting a new agent and exiting it at the end of the turn, not from the worktree, so reusing the checkout costs nothing that makes a run ralph.

What it buys:

- **One branch to review**, which is the target.
- **The join refusal cannot fire on the run path**, because there is only ever one base branch. The question stays open for `dispatch` and is now genuinely off the critical path rather than merely unreached.
- **Resume is free.** Re-running the same target finds the existing lane by its label and continues on its branch, which is what `Lanes.find` and `Lanes.dormant` already do for an issue.
- **The bootstrap tax is paid once per run** rather than once per issue. That is a real improvement and it does not solve the underlying problem, which is still [open](README.md#still-open).

**A dirty tree after a closed issue stops the run.** Successive issues share one worktree, so leftovers are inherited by the next iteration, which is then classified against a tree it did not dirty. `policy.decide` already detects this and says so, and under `milhouse step` a person reads that line and deals with it. Under a run nobody does, so the run halts instead ([ADR 0022](0022-the-loop-is-earned.md)).

## Rationale

The useful key for a lane is not the unit of work, it is the unit of review, and the two differ between the two ways of driving milhouse. This is why the change is an amendment rather than a contradiction: 0020's four assignment rules are unchanged and still run inside a lane, they just run inside a lane the run named.

It also converts the join question from a landmine into a known gap, which is worth more than answering it would be right now. Answering it means picking a base for two candidate branches, and that choice is only correctly made by watching a real diamond go through.

## Alternatives considered

**Keep 0020 as written, per-issue lanes under `run` too.** Rejected. It leaves a finished epic unmerged across branches with nothing joining them, and it lets the join refusal fire unattended.

**Work the primary checkout instead of a worktree.** Simplest to reason about, simplest to review, and rejected because an unattended overnight run would then be committing into the checkout the person is sitting in. It also reintroduces the untracked-files problem that put lanes outside the repository in the first place.

**Merge the per-issue lanes at the end of the run.** This is the landing question, and it is unanswered on purpose: N lanes that each verify green can be red combined. Choosing one base avoids needing the answer rather than pretending to have it.

## Consequences

- **Two lanes can exist for the same issue**, one opened by `dispatch` and labelled with the issue, one opened by a run and labelled with the target. Nothing detects it, and nothing needs to: the lock is what stops two processes driving the same pane, and the two lanes are separate checkouts on separate branches. It is confusing to look at in `milhouse status`, which is why the lane listing names what each lane is keyed by.
- **A crashed run leaves its worktree and re-opens its claim.** `Lanes.locate` looks a lane up by issue id and a run's lane carries the target, so reconciliation finds no live lane for the in-flight issue and re-opens it, which is the wanted behaviour. The surviving worktree is what makes the next run resume on the same branch.
- **The branch name is a target id**, so `milhouse/<epic-id>` rather than `milhouse/<issue-id>`. Anything reading branch names to find the issue is now wrong, and the audit log is where that mapping actually lives.
- **`--count N` will have to revisit this.** A concurrent run wants several lanes again, and the key it wants is not obviously either the issue or the target. This ADR does not decide it.
