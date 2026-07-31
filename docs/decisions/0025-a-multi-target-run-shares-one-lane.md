# 0025 — A multi-target run shares one lane, keyed by every target

**Status:** accepted. Amends [ADR 0023](0023-a-run-has-one-lane.md).

## Context

`milhouse run` took exactly one target, and [ADR 0023](0023-a-run-has-one-lane.md) named the integration lane after it. That is the wrong shape once two epics are being worked at the same time and the dependency graph crosses between them: a child of epic A blocked by a child of epic B. Two sequential runs cannot finish either one — run A stops with "nothing ready but work is left," because the issue that would unblock the rest of A is under B and out of A's fence.

`milhouse-mhs`, the issue that asked for this, put the framing question up front: is this really "several targets" or "one target that is a set"? The second reading is the lighter answer, and it is the one this ADR takes: resolving several targets produces one `Scope`, and everything below `milhouse.scope` — `Session`, `step`, `Parallel`, `run.run` — keeps taking a single fenced tracker and a single lane key, with no idea how many targets built either one. [ADR 0024](0024-an-integration-lane-and-worker-lanes.md)'s two-level lane scheme is unaffected: a worker lane is still keyed by the issue, branched from the run's one integration lane.

What is left to decide is the one thing that changes shape: the fence, and the name of the lane it produces.

## Decision

**The fence is a union, computed once.** `milhouse.scope.resolve_many` takes several target ids, resolves each one's own membership the way a single target already would — an epic's descendants via `bd`'s `--parent` (which already returns them transitively), a leaf issue's blockers via the existing closure walk — and unions the results in the order the targets were given, keeping the first occurrence of a duplicate. The tracker is then fenced to that explicit membership, the same mechanism a single closure target already uses. `bd ready --claim` cannot express a union of parents, so this gives up the atomicity a single epic target has today; a serial run holding one lock is still safe, and this is the same trade a single closure target already makes.

One target is `resolve_many`'s first branch, and it is `resolve` unchanged: same `bd` calls, same `Scope`, same lane. Nothing about the common case moves.

**The lane is keyed by every target's id, sorted and joined with `+`.** `Scope.key` is `targets[0].id` at one target, and `"+".join(sorted(t.id for t in targets))` above that. Sorting makes the key independent of the order the targets were typed in, so `milhouse run b a` and `milhouse run a b` find the same lane — the same resume property [ADR 0023](0023-a-run-has-one-lane.md) gives a single target. `+` is not `WORKER_SEPARATOR` (`--`, [ADR 0024](0024-an-integration-lane-and-worker-lanes.md)) and is a legal git ref character, so `{branch_prefix}a+b` and a worker lane inside it, `{branch_prefix}a+b--a.1`, coexist the same way a single target's branches already do.

**The report names every target, not the key.** `RunResult.targets` is a tuple, never the joined string, and the CLI prints one line per target before the run and lists every target's id in the closing summary. The joined key is a lane label a person never has to type by hand; the ids are what they typed and what they are waiting on.

## Rationale

The useful unit for a lane was already established by [ADR 0023](0023-a-run-has-one-lane.md): the branch a person reviews. Several targets still review as one branch — that is the whole point of working them in one run rather than two — so the key just needs to be a name, not a decomposition. Sorting and joining ids is the smallest thing that is both stable across argument order and legal as a git ref, and it costs nothing downstream: `Lanes.worker_branch` and `Lanes.open_for` already take the key as an opaque string, so nothing in `milhouse.lanes` changed to accommodate this.

Computing the union once in `milhouse.scope` rather than threading "how many targets" through `run.py`, `step.py`, and `parallel.py` is what keeps this a small change. Those modules already only know a scope through the tracker it hands them; they never asked how the fence was built, and they still don't.

## Alternatives considered

**Give the multi-target lane its own kind of key**, such as a hash or a run-supplied name. Rejected: a hash is not resumable by a human re-typing the same targets, and a required `--name` is a second thing to remember that the single-target path never needed. The sorted-and-joined ids are exactly as legible as a single target's id and need no new flag.

**Keep the union order-sensitive**, keying by the targets as given rather than sorted. Rejected: it would make `run a b` and `run b a` open two different lanes for what is, semantically, the same work, which is a worse surprise than the one line of sorting avoids.

**Give `BeadsTracker` several parents** instead of falling back to explicit membership for a multi-target epic scope. Rejected for this pass: `bd list --parent` and `bd ready --parent` each take one parent, so several would mean one `bd` call per parent plus merging the results in `BeadsTracker` itself, which is more surface than resolving each target's membership once, in `milhouse.scope`, and handing the tracker a set it already knows how to take. It is also not free of the same trade: a merged-parent tracker still cannot offer `bd ready --claim`'s atomicity across parents. If per-parent scans turn out to be too slow against a large tracker, this is the door to open next.

## Consequences

- **A multi-target epic scope gives up `bd ready`'s atomic claim**, the same way a single closure target already does (`BeadsTracker._ready_among`). Two milhouse processes racing the same multi-target run could both see an issue as ready before either claims it. [ADR 0022](0022-the-loop-is-earned.md) already restricts a run to one at a time via its lock, so this is a real constraint on ever relaxing that, not a new hole in what is enforced today.
- **The lane key is not always the branch name a target's own id would suggest.** `milhouse status` shows `a+b` as a lane's label, and a person has to read the `+` as "this lane is several targets" rather than one target with a strange id. The lane listing already documents that the label is an issue id for `dispatch` and a target id for a run's lane; it now has to say the same for a joined key.
- **A target that appears in two separate runs' scopes is not detected or refused.** Running `a` alone and `a b` together at the same time opens two lanes with different keys, each fenced to `a`'s members, and nothing stops both from claiming into the same issue's dependency chain at once beyond `bd ready --claim`'s own atomicity. This was already possible with two single-target runs sharing a blocker outside either fence; a union scope makes the overlap more likely to matter, not new in kind.
