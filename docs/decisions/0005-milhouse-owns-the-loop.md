# 0005 — milhouse owns the loop, the agent owns one step

**Status:** accepted

## Context

An agent given "work through this epic until it is done" will decide for itself what to do next and when to stop. Both of those decisions are where unattended runs go wrong, and neither can be bounded from inside the agent's own context.

## Decision

The iteration prompt is deliberately narrow: here is one issue, do it, verify it, commit it, close it. The agent does not pick its own work and does not decide when the run is over.

milhouse keeps:

- **Iteration ceiling** — stop at `--max-iterations`, report cleanly.
- **Per-issue attempt cap** — `--max-attempts` failures on one issue marks it `blocked` with a note and moves on rather than spinning.
- **Stall detection** — an iteration producing no commit _and_ leaving the issue open is a failed attempt ([ADR 0004](0004-outcome-from-beads-and-git.md)).
- **Turn timeout** — `herdr agent prompt --wait --timeout` bounds a single turn, so a wedged agent cannot hang the run.
- **Clean teardown** — SIGINT/SIGTERM reverts the in-flight claim (`bd update <id> --status open --assignee ""`), exits the agent, and leaves the workspace open. Panes are never closed out from under a human.
- **Decomposition confirmation** — proposed issues are shown for approval before creation unless `--yes` is passed ([ADR 0006](0006-planning-agent-proposes-milhouse-creates.md)).

## Consequences

The guardrails actually bind, because they live outside the thing being guarded.

The prompt has to say all of this out loud, which makes [ADR 0013](0013-iteration-prompt-contract.md) load-bearing: an agent that closes an issue it did not finish defeats the whole scheme, and the only defence is the prompt plus the fact that a human can watch the pane.

An agent _may_ still create new issues under the epic when it discovers work. That is allowed, and bounded only by `--max-iterations`; see [the open list](README.md#still-open).
