# 0008 — Recover from crashes by reconciling at startup

**Status:** accepted

## Context

Clean teardown on SIGINT/SIGTERM reverts the in-flight claim. `SIGKILL`, a lost
SSH session, or a closed laptop does not. `bd` has no lease expiry, so an issue
left `in_progress` and assigned stays that way forever, and `bd ready` will never
return it again. The loop would then report the epic finished with issues still
open.

Related: re-running `milhouse run` against an in-flight task had undefined
semantics.

## Decision

`milhouse run` reconciles before it does anything else, and re-running is the
supported way to resume:

1. Load `state.json`, if it exists.
2. If `state.claimed_issue` is set, that issue was claimed by a run that did not
   finish. Re-open it: `bd update <id> --status open --assignee ""`, append a
   note recording the interrupted run, and clear `claimed_issue`.
3. Keep `attempts`, `iterations`, and `branch`. A resumed run continues the
   attempt counts rather than granting three fresh attempts on an issue that has
   already failed three times.
4. Check the recorded `workspace_id` still exists (`herdr workspace get`). If it
   does not, create a new one and record it.

No reaper, no daemon, no lease. The next `milhouse run` is the recovery
mechanism.

## Consequences

- Resume is well defined and is simply "run it again". There is no separate
  `milhouse resume` command to keep working.
- The recovery is scoped to what *this* milhouse claimed, recorded in its own
  state file. milhouse never re-opens issues it does not have a record of
  claiming, so it cannot stomp on a human or another agent working the same epic.
- The failure mode this does not cover: a machine that never runs milhouse again
  leaves the claim in place. `bd update <id> --status open --assignee ""` by hand
  is the fix, and it is in [troubleshooting](../troubleshooting.md).
- Two milhouse processes on the same task at the same time are still not
  supported. `bd ready --claim` keeps them from working the same issue, but they
  would fight over `state.json`. See [the open list](README.md#still-open).
