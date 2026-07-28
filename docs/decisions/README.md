# Architecture decision records

One file per settled decision. Each states the context, the decision, and what it costs, so the trade is revisitable rather than merely remembered.

| ADR                                                           | Decision                                                         |
| ------------------------------------------------------------- | ---------------------------------------------------------------- |
| [0001](0001-shell-out-to-bd-and-herdr.md)                     | Shell out to `bd` and `herdr` and parse JSON, not the socket API |
| [0002](0002-link-issues-via-bead-metadata.md)                 | Link issues to task definitions with bead metadata               |
| [0003](0003-agents-run-in-herdr-panes.md)                     | Every agent runs in a herdr pane, restarted per iteration        |
| [0004](0004-outcome-from-beads-and-git.md)                    | Iteration outcome comes from beads and git, not an exit code     |
| [0005](0005-milhouse-owns-the-loop.md)                        | milhouse owns the loop, the agent owns one step                  |
| [0006](0006-planning-agent-proposes-milhouse-creates.md)      | The planning agent proposes a plan; milhouse creates the issues  |
| [0007](0007-branch-per-task.md)                               | One branch per task definition                                   |
| [0008](0008-crash-recovery-by-reconciliation.md)              | Recover from crashes by reconciling at startup                   |
| [0009](0009-permission-posture.md)                            | Supervised by default; unattended is opt-in and explicit         |
| [0010](0010-config-file-schema.md)                            | `.milhouse/config.toml` schema                                   |
| [0011](0011-exiting-the-agent.md)                             | Exit the agent with keys, fall back to pane churn                |
| [0012](0012-no-cost-controls-in-v1.md)                        | No token or cost caps in v1                                      |
| [0013](0013-iteration-prompt-contract.md)                     | What the iteration prompt promises and demands                   |
| [0014](0014-step-is-the-primitive.md)                         | The step is the primitive; the loop is a policy over it          |
| [0015](0015-one-run-at-a-time.md)                             | One run per task at a time, enforced by a lock                   |
| [0016](0016-milhouse-verifies.md)                             | milhouse verifies a closed issue rather than trusting it         |
| [0017](0017-no-loop-until-it-is-earned.md)                    | No loop until one is earned                                      |
| [0018](0018-no-task-milhouse-works-the-ready-queue.md)        | There is no task; milhouse works the tracker's ready queue       |
| [0019](0019-beads-is-the-coordination-layer.md)               | Beads is the coordination layer, not GitHub Issues               |
| [0020](0020-a-lane-is-a-herdr-worktree.md)                    | A lane is a herdr worktree, and herdr is the lane registry       |
| [0021](0021-iteration-history-goes-in-the-beads-audit-log.md) | Iteration history goes in the beads audit log                    |

## Retired

Settled, then made moot by [ADR 0018](0018-no-task-milhouse-works-the-ready-queue.md), which removes the task definition. They are kept because they record why the machinery existed, which is what makes it safe to have removed.

- **[ADR 0002](0002-link-issues-via-bead-metadata.md), the metadata link.** It tied a task definition to the epic decomposing it. With no task, nothing to tie.
- **[ADR 0006](0006-planning-agent-proposes-milhouse-creates.md), the approval guardrail.** It answered "how do we stop the planning agent creating issues without approval," structurally rather than politely. There is no planning agent.
- **[ADR 0007](0007-branch-per-task.md), one branch per task.** No task to name a branch after. [ADR 0020](0020-a-lane-is-a-herdr-worktree.md) decides where commits land now.

## Deferred

Settled, then de-scoped by [ADR 0014](0014-step-is-the-primitive.md) and [ADR 0017](0017-no-loop-until-it-is-earned.md). They describe the loop that lands over the step primitive, not what milhouse does today. They are kept because they are the design, not because they are the behaviour.

- **[ADR 0005](0005-milhouse-owns-the-loop.md), the loop and its guardrails.** There is no loop, so there is no attempt cap, blocked-agent policy, or retry ladder either. What survives is the division of labour: milhouse picks the work and bounds the turn, the agent does one issue.
- **[ADR 0009](0009-permission-posture.md), the unattended posture.** Supervised is no longer merely the default, it is the only mode. The `[agent] args` escape hatch still exists and still means what it said.
- **[ADR 0012](0012-no-cost-controls-in-v1.md), cost controls.** Still no cost tracking, and the reason is unchanged. It matters less now: one step is one turn, and a person decides whether there is another.

## Still open

These were raised in the design and are deliberately not settled yet. They are tracked here so they do not get lost, not in a commit message.

- **Joins in the dependency graph.** [ADR 0020](0020-a-lane-is-a-herdr-worktree.md) assigns a lane per independent issue and stacks dependent work in its predecessor's lane. An issue depending on two blockers that ran in separate lanes has two candidate base branches and no rule picking between them. `bd ready` handles the timing; the base is undecided. This is the first thing that will bite.
- **Landing the lanes.** N lanes that each verify green can be red combined, which serial work on one branch could never produce. `bd merge-slot` is the coordination primitive and does not perform the merge. Deliberately out of scope until a parallel run has been watched.
- **The per-lane bootstrap.** A fresh worktree has no `.venv`. `[verify] command` needs something to run against, so lanes need a setup command, and its shape is unknown.
- **Re-planning caps.** An iteration that discovers new work can `bd create` mid-run. Whether that needs a cap depends on whether runs are observed to grow without bound.
- **Agent portability.** [ADR 0003](0003-agents-run-in-herdr-panes.md) claims a second agent backend is a config change. The exit key sequence is already configurable for exactly this reason ([ADR 0011](0011-exiting-the-agent.md)), but the prompts have only ever been tuned against `claude`. Believe the claim after testing one other `--kind`, not before.
