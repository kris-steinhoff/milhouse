# Architecture decision records

One file per settled decision. Each states the context, the decision, and what
it costs, so the trade is revisitable rather than merely remembered.

| ADR                                                          | Decision                                                        |
| ------------------------------------------------------------ | --------------------------------------------------------------- |
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

## Still open

These were raised in the design and are deliberately not settled yet. They are
tracked here so they do not get lost, not in a commit message.

- **Concurrency.** `bd ready --claim` is already race-free, so N parallel loops
  over one epic is mostly one worktree and one pane each. The awkward part is
  waiting on N panes: over the CLI that is one blocking `herdr agent wait`
  process per pane, where the socket API's `events.subscribe` would watch all of
  them from a single connection. **That is the trigger for revisiting
  [ADR 0001](0001-shell-out-to-bd-and-herdr.md).** Out of scope for v1.
- **Re-planning caps.** An iteration that discovers new work can
  `bd create --parent <epic>` mid-run. That is allowed and bounded only by
  `--max-iterations`. Whether it needs its own cap depends on whether runs are
  observed to grow without bound.
- **Agent portability.** [ADR 0003](0003-agents-run-in-herdr-panes.md) claims a
  second agent backend is a config change. The exit key sequence is already
  configurable for exactly this reason ([ADR 0011](0011-exiting-the-agent.md)),
  but the prompts have only ever been tuned against `claude`. Believe the claim
  after testing one other `--kind`, not before.
