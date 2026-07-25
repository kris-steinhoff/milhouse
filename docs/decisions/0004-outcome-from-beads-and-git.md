# 0004 — Iteration outcome comes from beads and git, not an exit code

**Status:** accepted

## Context

There is no exit status to read from an interactive agent running in a TUI. That
is fine: a process exit code was never a good success signal anyway, since an
agent can exit zero having done nothing.

## Decision

Classify each iteration from observable state after the turn:

| Signal                                | Outcome     | Counts as an attempt |
| ------------------------------------- | ----------- | -------------------- |
| Issue is closed in beads              | `success`   | no                   |
| Agent settled `blocked`               | `blocked`   | no                   |
| Issue open, git `HEAD` moved          | `partial`   | yes                  |
| Issue open, `HEAD` unchanged          | `stalled`   | yes                  |
| `agent prompt` timed out              | `timeout`   | yes                  |
| herdr or `bd` itself failed           | `error`     | yes                  |

Order matters: a closed issue is `success` even if the agent also ended
`blocked`, because the work is done.

`HEAD` is read on the branch the run is committing to, which
[ADR 0007](0007-branch-per-task.md) pins down.

## Consequences

- `partial` and `stalled` are the same instruction to the loop (retry) but not
  the same diagnosis, so they stay distinct in the run history. `partial` means
  the agent is making progress and probably needs another turn; a run full of
  `stalled` means the prompt is wrong.
- `blocked` does not count against `--max-attempts`. An agent waiting on a human
  has not failed at anything.
- Classification is a pure function in `outcome.py` over
  `(issue_before, issue_after, head_before, head_after, agent_state, timed_out)`,
  so every row of that table is a unit test with no subprocess involved.
