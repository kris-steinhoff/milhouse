# 0015 — One run per task at a time, enforced by a lock

**Status:** amended by [ADR 0020](0020-a-lane-is-a-herdr-worktree.md). The lock survives; its scope is now one lane rather than the repository, because concurrent lanes are the point. The argument below is unchanged: reconciliation is destructive, and the lock is what stops it running over live work.

## Context

[ADR 0008](0008-crash-recovery-by-reconciliation.md) makes re-running the recovery mechanism: `milhouse run` re-opens whatever claim the last run left behind, because `bd` has no lease expiry and an issue left `in_progress` would never be offered again.

Reconciliation is therefore destructive by design, and it had no way to tell a crashed run from a running one. Starting a second `milhouse run` against a task already being worked would re-open the claim the first run was in the middle of, hand the same issue to a second agent, and drive the same pane recorded in `state.json`. Both runs would then write `state.json` and append to the event log.

This matters more under supervision, not less. The whole point of `milhouse step` is that a person is at the keyboard, and a person with two terminals open is the normal case.

## Decision

A run holds a lock on its task's run directory for as long as it is open.

`.milhouse/runs/<task_slug>/lock.json` records the pid, the hostname, and when the lock was taken. It is created with `O_CREAT | O_EXCL`, so taking it is atomic. `Session.__enter__` takes it before anything else happens, and drops it on the way out, including when opening fails partway through.

Meeting an existing lock:

| Holder                               | Result                                       |
| ------------------------------------ | -------------------------------------------- |
| A live pid on this host              | Refuse, naming the process. Exit code `10`.  |
| A pid nothing is running under       | Steal it, and say so.                        |
| Any pid on another host              | Refuse. A pid means nothing off its machine. |
| An unreadable or truncated lock file | Replace it.                                  |

Reconciliation then happens with the lock held, so the claim being re-opened cannot belong to a run that is still working it.

## Consequences

- **The failure this prevents is silent.** Two runs over one epic corrupt each other's bookkeeping without either noticing, and the symptom shows up later as an issue that was worked twice or a history with interleaved iterations.
- **Advisory, in the usual sense.** It stops milhouse from tripping over itself. It does not stop anything else from writing to the run directory, and it is not a distributed lock: the host check is conservative because a pid from another machine cannot be checked at all.
- **A recycled pid reads as live.** The refusal names the process, so a person can see it is unrelated and delete the lock. Erring this way costs a re-run; erring the other way corrupts one.
- **`milhouse status` reports the holder**, so "why does it say another run has it" is answerable without going looking for the file.
- This does not make concurrent runs over _different_ tasks any harder. They have different run directories and different locks, which is what parallelism would want anyway. Concurrency over **one** epic is still out of scope, and still the trigger for revisiting [ADR 0001](0001-shell-out-to-bd-and-herdr.md).
