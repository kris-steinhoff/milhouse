# 0013 — What the iteration prompt promises and demands

**Status:** accepted, and expected to change

## Context

For a ralph loop the prompt _is_ the product, and the methodology is explicitly to tune it by observation. `iterate.md.j2` started as a filename. This ADR pins down what it has to contain so it can be changed deliberately rather than drifting.

Four questions had to be answered: what contract the prompt imposes, whether to inject `bd prime` output or lean on the `AGENTS.md` that `bd init` writes, how much of the task definition to include alongside the issue, and how much repo convention to restate versus trust `CLAUDE.md` for.

## Decision

### The contract on the agent

The prompt states, in the imperative, that the agent must:

1. Work **only** the issue it was given. Not the next one, not the epic.
2. Verify the change — run the tests, run the linter.
3. Update the docs covering the change **in the same commit**. An issue is not done until its documentation is.
4. Commit, referencing the issue id.
5. Close the issue with `bd close <id>`, and only if all of the above happened.
6. If it cannot finish: leave the issue open, append what it learned with `bd note <id>`, and stop. Do **not** close it.

Point 6 matters more than it looks. Without it, the incentive is to close the issue and look successful, which is the one failure milhouse cannot detect — `bd` says closed, so [ADR 0004](0004-outcome-from-beads-and-git.md) says success.

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

- Every prompt change is a behaviour change and gets a `CHANGELOG.md` entry.
- The templates ship in the package rather than being user-configurable ([ADR 0010](0010-config-file-schema.md)), so a run is reproducible from a milhouse version.
- The rendered prompt is saved to `.milhouse/runs/<task>/iter-NNN.prompt` every iteration, so tuning by observation has something to observe.
- This ADR will be revised. That is the expected outcome, not a failure of the decision.
