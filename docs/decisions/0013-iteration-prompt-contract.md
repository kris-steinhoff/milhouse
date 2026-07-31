# 0013 — What the iteration prompt promises and demands

**Status:** accepted, and expected to change

## Context

For a ralph loop the prompt _is_ the product, and the methodology is explicitly to tune it by observation. `iterate.md.j2` started as a filename. This ADR pins down what it has to contain so it can be changed deliberately rather than drifting.

Four questions had to be answered: what contract the prompt imposes, whether to inject `bd prime` output or lean on the `AGENTS.md` that `bd init` writes, how much of the task definition to include alongside the issue, and how much repo convention to restate versus trust `CLAUDE.md` for.

## Decision

### The contract on the agent

The prompt states, in the imperative, that the agent must:

1. Work **only** the issue it was given. Not the next one, not the epic.
2. **Prepare the lane if it is not built yet**, before judging whether the tests pass.
3. Verify the change — run the tests, run the linter.
4. Update the docs covering the change **in the same commit**. An issue is not done until its documentation is.
5. Commit, referencing the issue id.
6. Close the issue with `bd close <id>`, and only if all of the above happened.
7. If it cannot finish: leave the issue open, append what it learned with `bd note <id>`, and stop. Do **not** close it.

Point 2 is the newest and belongs to the agent rather than to milhouse for a reason worth stating: bootstrapping varies per project, and the agent is the only party standing in the tree with the repository's own instructions in front of it. milhouse knowing how to build every kind of project is a losing proposition; the agent reading `AGENTS.md` and running what it says is not. It is stated before verification because the failure it prevents is an agent concluding the tests fail when they were never able to run. See [ADR 0024](0024-an-integration-lane-and-worker-lanes.md) for the one lane this does not cover — a concurrent run's integration lane has no agent in it, so its gate command must be able to bootstrap itself.

Point 7 matters more than it looks. Without it, the incentive is to close the issue and look successful — `bd` says closed, so [ADR 0004](0004-outcome-from-beads-and-git.md) says success. That was the one failure milhouse could not detect until [ADR 0016](0016-milhouse-verifies.md) gave it a way to check. The prompt still asks, because it is cheaper for an agent to find its own failure mid-turn than for milhouse to find it afterwards.

### What goes in

- **The issue**: id, title, description, acceptance criteria, and any notes previous attempts left. Notes are how a fresh context window learns from the attempt before it, which is the only memory the loop has.
- **The task definition**, in full, under a heading that marks it as background. It is usually short, and without it the agent has no idea what the issue is _for_. This is the part most likely to change once runs are observed.
- **The attempt number**, when it is not the first, with the previous outcome. An agent on attempt 3 of 3 should know it is on attempt 3 of 3.
- **The branch** it is committing to.

### What stays out

- **`bd prime` output.** `bd init` writes an `AGENTS.md` that already teaches the agent the beads workflow, and every agent kind herdr supports reads either `AGENTS.md` or `CLAUDE.md` on startup. Restating it burns context on something the agent already has.
- **Repo conventions.** Same reasoning: that is what `CLAUDE.md` is for. The prompt says "follow the conventions in CLAUDE.md/AGENTS.md" and leaves it.
- **The issue tree.** The agent does not choose its work ([ADR 0005](0005-milhouse-owns-the-loop.md)), so showing it what else is pending only invites it to start.

## Consequences

- Every prompt change is a behaviour change, so it lands with the doc change that describes it rather than as a silent edit.
- The templates ship in the package rather than being user-configurable ([ADR 0010](0010-config-file-schema.md)), so a run is reproducible from a milhouse version.
- The rendered prompt is saved to `.milhouse/runs/<task>/iter-NNN.prompt` every iteration, so tuning by observation has something to observe.
- This ADR will be revised. That is the expected outcome, not a failure of the decision.
