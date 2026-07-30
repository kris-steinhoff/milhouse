# Architecture decision records

One file per settled decision. Each states the context, the decision, and what it costs, so the trade is revisitable rather than merely remembered.

| ADR                                                           | Decision                                                             |
| ------------------------------------------------------------- | -------------------------------------------------------------------- |
| [0001](0001-shell-out-to-bd-and-herdr.md)                     | Shell out to `bd` and `herdr` and parse JSON, not the socket API     |
| [0002](0002-link-issues-via-bead-metadata.md)                 | Link issues to task definitions with bead metadata                   |
| [0003](0003-agents-run-in-herdr-panes.md)                     | Every agent runs in a herdr pane, restarted per iteration            |
| [0004](0004-outcome-from-beads-and-git.md)                    | Iteration outcome comes from beads and git, not an exit code         |
| [0005](0005-milhouse-owns-the-loop.md)                        | milhouse owns the loop, the agent owns one step                      |
| [0006](0006-planning-agent-proposes-milhouse-creates.md)      | The planning agent proposes a plan; milhouse creates the issues      |
| [0007](0007-branch-per-task.md)                               | One branch per task definition                                       |
| [0008](0008-crash-recovery-by-reconciliation.md)              | Recover from crashes by reconciling at startup                       |
| [0009](0009-permission-posture.md)                            | Supervised by default; unattended is opt-in and explicit             |
| [0010](0010-config-file-schema.md)                            | `.milhouse/config.toml` schema                                       |
| [0011](0011-exiting-the-agent.md)                             | Exit the agent with keys, fall back to pane churn                    |
| [0012](0012-no-cost-controls-in-v1.md)                        | No token or cost caps in v1                                          |
| [0013](0013-iteration-prompt-contract.md)                     | What the iteration prompt promises and demands                       |
| [0014](0014-step-is-the-primitive.md)                         | The step is the primitive; the loop is a policy over it              |
| [0015](0015-one-run-at-a-time.md)                             | One run per task at a time, enforced by a lock                       |
| [0016](0016-milhouse-verifies.md)                             | milhouse verifies a closed issue rather than trusting it             |
| [0017](0017-no-loop-until-it-is-earned.md)                    | No loop until one is earned                                          |
| [0018](0018-no-task-milhouse-works-the-ready-queue.md)        | There is no task; milhouse works the tracker's ready queue           |
| [0019](0019-beads-is-the-coordination-layer.md)               | Beads is the coordination layer, not GitHub Issues                   |
| [0020](0020-a-lane-is-a-herdr-worktree.md)                    | A lane is a herdr worktree, and herdr is the lane registry           |
| [0021](0021-iteration-history-goes-in-the-beads-audit-log.md) | Iteration history goes in the beads audit log                        |
| [0022](0022-the-loop-is-earned.md)                            | The loop is earned, as `milhouse run <target>`                       |
| [0023](0023-a-run-has-one-lane.md)                            | A run has one lane, keyed by its target                              |
| [0024](0024-an-integration-lane-and-worker-lanes.md)          | A concurrent run has an integration lane and a worker lane per issue |

## Superseded

- **[ADR 0017](0017-no-loop-until-it-is-earned.md), no loop until one is earned.** Its condition was met, and [ADR 0022](0022-the-loop-is-earned.md) is the loop it was waiting for. It is kept because it is the reason the loop's policy was written from watched iterations rather than guessed, and because the failure mode it named is still the one to watch for.

## Retired

Settled, then made moot by [ADR 0018](0018-no-task-milhouse-works-the-ready-queue.md), which removes the task definition. They are kept because they record why the machinery existed, which is what makes it safe to have removed.

- **[ADR 0002](0002-link-issues-via-bead-metadata.md), the metadata link.** It tied a task definition to the epic decomposing it. With no task, nothing to tie.
- **[ADR 0006](0006-planning-agent-proposes-milhouse-creates.md), the approval guardrail.** It answered "how do we stop the planning agent creating issues without approval," structurally rather than politely. There is no planning agent.
- **[ADR 0007](0007-branch-per-task.md), one branch per task.** No task to name a branch after. [ADR 0020](0020-a-lane-is-a-herdr-worktree.md) decides where commits land now.

## Deferred

De-scoped by [ADR 0014](0014-step-is-the-primitive.md) and [ADR 0017](0017-no-loop-until-it-is-earned.md), then partly reinstated by [ADR 0022](0022-the-loop-is-earned.md) now that there is a loop again. What each one is worth today:

- **[ADR 0005](0005-milhouse-owns-the-loop.md), the loop and its guardrails.** Back in force for `milhouse run`, with the numbers observed rather than guessed: an attempt cap that defers, a blocked-agent policy that halts, and an iteration ceiling. [ADR 0022](0022-the-loop-is-earned.md) supplies the table. The division of labour it describes never went away.
- **[ADR 0009](0009-permission-posture.md), the unattended posture.** `run` is the first thing that makes unattended meaningful, and nothing about the posture changed to meet it. Supervised is still what you get, `[agent] args` is still the only escape hatch, and the consent screen is still accepted by hand.
- **[ADR 0012](0012-no-cost-controls-in-v1.md), cost controls.** Still no cost tracking, and the reason is unchanged. It matters more now rather than less: `--max-iterations` bounds turns, and turns are not the same size.

## Still open

These were raised in the design and are deliberately not settled yet. They are tracked here so they do not get lost, not in a commit message.

- **Joins in the dependency graph.** [ADR 0020](0020-a-lane-is-a-herdr-worktree.md) assigns a lane per independent issue and stacks dependent work in its predecessor's lane. An issue depending on two blockers that ran in separate lanes has two candidate base branches and no rule picking between them. `bd ready` handles the timing, and the base is undecided. milhouse refuses the step and names both lanes rather than guessing, so the symptom is loud, but that is a stop rather than an answer. [ADR 0023](0023-a-run-has-one-lane.md) takes this off the critical path for `milhouse run`, and [ADR 0024](0024-an-integration-lane-and-worker-lanes.md) keeps it off: a worker lane's base is always the integration branch and never another worker lane. It is still the first thing that will bite `dispatch`.
- **Landing the lanes, for `dispatch`.** N lanes that each verify green can be red combined, which serial work on one branch could never produce. [ADR 0024](0024-an-integration-lane-and-worker-lanes.md) answers it for `milhouse run`: worker branches are merged into the integration branch one at a time, and a merge that joined anything re-runs the gate on the result. That answer has now been watched end to end, and it holds in the sense that matters — the `conflict` halt fired exactly as written, with both branches intact and the issue left closed — but it is not free of open ends. Of the eight merges those runs attempted, four conflicted, so a run's report naming a branch only a person can land is the common case rather than the corner. What those runs then did about it has since been decided: a merge that did not land is the last merge of the run, so the turns still in flight are finished and settled but nothing else is merged, and the report lists the unlanded branches in the order they have to be landed (`milhouse-amd.13`, closed; ADR 0024 has the argument, which is about the merge order rather than the cost). `dispatch` still merges nothing, `bd merge-slot` is still a coordination primitive that does not perform the merge, and whether the run's answer generalises to lanes another process opened is unanswered.
- **When to reap.** `milhouse reap` collects whatever has settled and is safe to run at any time, so `until milhouse reap; do sleep 60; done` works. [ADR 0022](0022-the-loop-is-earned.md) answers this for the serial case, where the loop waits on each turn, and [ADR 0024](0024-an-integration-lane-and-worker-lanes.md) answers it for a concurrent run, which polls its own lanes at `[run] poll_ms`. Watching that run added a question, and it has since been answered: polling could collect a turn whose agent never started, because herdr reports the agent as not working while the prompt is still sitting unsubmitted in the pane, and an agent that never began reports the same `idle` as one that finished. `milhouse step` could not reach that state, since `herdr agent prompt --wait` requires an observed state change before it waits on anything. `dispatch` now asks for the same confirmation, bounded to the submission rather than to the turn, so there is no unstarted turn left for a poller to misread (`milhouse-amd.12`, closed; ADR 0024 records the probe). The mechanism question is unchanged: for lanes milhouse did not start, the [ADR 0001](0001-shell-out-to-bd-and-herdr.md) revisit and the socket API's `events.subscribe` is still the better answer, and polling a run's own lanes was not enough to earn it.
- **The per-lane bootstrap.** A fresh worktree has no `.venv`, and `[verify] command` now runs in the lane rather than the primary checkout, so a gate that assumes one fails for environmental reasons rather than real ones. Lanes need a setup command, and its shape is unknown. **This got worse, and it is the entry this epic damaged rather than narrowed.** [ADR 0023](0023-a-run-has-one-lane.md) paid the tax once per run and counted that as a real improvement; [ADR 0024](0024-an-integration-lane-and-worker-lanes.md) gives it straight back, once per issue and now several at once, so a `--count 4` run bootstraps four worktrees simultaneously. How much that costs is still unmeasured: the watched runs used a scratch repository whose gate is standard-library only, so a fresh worktree needed no install and the tax was never paid. It also decides what a count above two is actually worth, which is why it is now on ADR 0024's revisit condition as well.
- **Re-planning caps.** An iteration that discovers new work can `bd create` mid-run. Whether that needs a cap depends on whether runs are observed to grow without bound.
- **Agent portability.** [ADR 0003](0003-agents-run-in-herdr-panes.md) claims a second agent backend is a config change. The exit key sequence is already configurable for exactly this reason ([ADR 0011](0011-exiting-the-agent.md)), but the prompts have only ever been tuned against `claude`. Believe the claim after testing one other `--kind`, not before.
