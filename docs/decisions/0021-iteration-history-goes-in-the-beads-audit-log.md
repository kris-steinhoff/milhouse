# 0021 — Iteration history goes in the beads audit log

**Status:** accepted and implemented.

## Context

Every piece of milhouse's own state was tested against the question "does something else already own this," and all of it moved ([ADR 0018](0018-no-task-milhouse-works-the-ready-queue.md), [ADR 0020](0020-a-lane-is-a-herdr-worktree.md)). `state.json` dissolves: the task fields go with the task, the epic goes with the planner, workspace and pane and branch become `herdr worktree list`, and the in-flight claim is `bd`'s, since an issue that is `in_progress` with no live lane carrying its id is an orphaned claim.

That left `events.jsonl`, one `Iteration` per line: the outcome and its one-line detail, plus the evidence it was derived from (`head_before`/`head_after`, `commits`, `attributed`, `dirty_after`, `verified`, `agent_state`) and pointers to the turn's artifacts.

The obvious home is a bd note, and that is wrong. Notes feed straight into the next iteration's prompt, so they are a communication channel to the next agent. Appending git shas and verification tails to that channel makes it worse for its actual reader, against [ADR 0013](0013-iteration-prompt-contract.md).

But bd has a better slot. `bd audit record` appends to `.beads/interactions.jsonl`, one event per line, for exactly the question these records answer: why did the agent do that. The `Entry` schema has `kind`, `created_at`, `actor`, an optional `issue_id`, and a free-form `extra` object, plus `--stdin` for a whole entry. bd already writes a `field_change` entry there on every issue mutation.

## Decision

**Record each iteration with `bd audit record`,** as `kind: "iteration"` with the record in `extra` and the issue in `issue_id`. `events.jsonl` goes.

The file stays gitignored, and that does not weaken the decision. Durability was never the benefit here: one less format to own, shared actor and timestamp conventions, native issue linkage, and a single ordered trail were, and all of those work on a local file. A run is machine-local by construction — the worktrees are one filesystem, the herdr server is one process, the panes are its children — so a trail describing that run has no business being more portable than the run.

The unified trail is the real gain, and it grows with [ADR 0020](0020-a-lane-is-a-herdr-worktree.md): with several agents acting at once, "agent closed `milhouse-5m6`" and "milhouse classified that turn as `rejected`" sitting in one ordered file is a post-mortem that no per-lane file gives you.

**Entries must stay small.** `interactions.jsonl` already has many concurrent writers, because every agent's `bd close` inside its own lane appends from its own process. That is safe today because every line is a few hundred bytes and POSIX guarantees atomic appends only below `PIPE_BUF`. `verification_output` is a tail of test output and clears that easily, so putting it in an entry would introduce the first large lines into a file that has been safe precisely because everything in it was small, and under N lanes that means torn records.

So the entry carries the verdict and `transcript_path`, and not the output. The output already has two homes: the `bd note` that carries the failure reason to the next agent, and the full transcript on disk.

**A second kind, `claim`, turned out to be needed, and implementing this is what found it.** The argument above says the in-flight claim is `bd`'s, because an issue that is `in_progress` with no live lane carrying its id is an orphaned claim. That is true once there are lanes ([ADR 0020](0020-a-lane-is-a-herdr-worktree.md)) and false before them: with no lane registry to check, "every `in_progress` issue" is the only available reading, and it cannot tell a claim milhouse abandoned from one a person made by hand — so reconciling would re-open somebody's work out from under them.

The trail answers it instead. A turn appends `claim` before it starts and `iteration` when it ends, so a `claim` with nothing after it is a run that died mid-turn, and nothing else in the file looks like one. It is three fields, it composes with lanes rather than competing with them, and it is what lets `state.json` go now rather than after [ADR 0020](0020-a-lane-is-a-herdr-worktree.md) lands.

**A third kind, `dispatch`, arrived with the split into dispatch and reap.** A dispatched turn outlives the process that started it, and reaping it needs the lane, the iteration number, and where `HEAD` was before the agent ran. That is the same argument as the `claim` entry and lands in the same place. It is bounded — a path, a branch, a sha, an integer, a timestamp — so the size rule above still holds, and it supersedes nothing: `claim`, `dispatch`, and `iteration` read in order as what happened to one issue.

## Consequences

- **`state.json` and `events.jsonl` both go,** and `RunStore` goes with them. `.milhouse/` stops being a state store.
- **What survives is the turn artifacts**, `<issue-id>/iter-NNN.prompt` and `.term`, which are captured text and have no other home. herdr's scrollback is live and bounded, and gone once a pane is replaced. So `.milhouse/` becomes an artifact directory, and the audit entry's `prompt_path` and `transcript_path` are the join between the structured record and the blobs.
- **milhouse reads a file it does not own.** `bd audit` has `record` and `label` and no query, so `milhouse status` still parses JSONL by hand — now filtering `kind == "iteration"` out of bd's own entries, against a schema milhouse does not control. That is the cost.
- **A tracker failure cannot be recorded.** The outcome `error` covers "could not re-read the issue after the turn," and if bd is the only store, the iterations most worth a record are the ones that cannot be written. The turn artifacts are still on disk.
- **The history stops being deletable in isolation.** Today `rm -rf .milhouse/runs/` loses the history and nothing else. Now it lives with the issue data.

## Revisit when

`interactions.jsonl` is versioned in git. Then this gets durability and dataset generation as well, at the price the existing `.gitignore` entry already names: every concurrent run appends to the same line ending, so every concurrent commit conflicts there.
