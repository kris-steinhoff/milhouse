# 0004 — Iteration outcome comes from beads and git, not an exit code

**Status:** accepted

## Context

There is no exit status to read from an interactive agent running in a TUI. That is fine: a process exit code was never a good success signal anyway, since an agent can exit zero having done nothing.

## Decision

Classify each iteration from observable state after the turn:

| Signal                       | Outcome   |
| ---------------------------- | --------- |
| Issue is closed in beads     | `success` |
| Agent settled `blocked`      | `blocked` |
| Issue open, git `HEAD` moved | `partial` |
| Issue open, `HEAD` unchanged | `stalled` |
| `agent prompt` timed out     | `timeout` |
| herdr or `bd` itself failed  | `error`   |

Order matters: a closed issue is `success` even if the agent also ended `blocked`, because the work is done.

Classifying is all this does. What _happens_ as a result is a separate pure function, `policy.decide()` ([ADR 0014](0014-step-is-the-primitive.md)).

`HEAD` is read on the branch the run is committing to, which [ADR 0007](0007-branch-per-task.md) pins down.

## Consequences

- `partial` and `stalled` get the same treatment from today's policy but are not the same diagnosis, so they stay distinct in the run history. `partial` means the agent is making progress and probably needs another turn; a run full of `stalled` means the prompt is wrong.
- `blocked` is not a failure of the agent's, and a policy that retries should not count it as one. An agent waiting on a human has not failed at anything.
- Classification is a pure function in `outcome.py` over `(issue_after, head_before, head_after, agent_state, timed_out, error)`, so every row of that table is a unit test with no subprocess involved.
