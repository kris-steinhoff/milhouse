# 0019 — Beads is the coordination layer, not GitHub Issues

**Status:** accepted

## Context

[ADR 0018](0018-no-task-milhouse-works-the-ready-queue.md) shrinks what milhouse asks of a tracker to five methods: `ready(claim=True)`, `get`, `note`, `release`, `children`. That is small enough to ask seriously whether GitHub Issues could be the persistence and coordination layer instead, with a real case for it.

GitHub would collapse two sync operations into one. Today a unit of work ends with `git push` **and** `bd dolt push`, two systems that can drift. Lanes would become pull requests, `Fixes #123` would close issues natively, and CI-green would replace a local `uv run pytest` as the verification signal — which is what `AGENTS.md` already declares the real gate to be. Every coding agent already knows `gh`, so `bd prime`, the beads skill, and the git hooks would stop being setup burden. And the `gh:owner/repo#123` source would stop being an import problem, because the issue would just be the issue.

GitHub can serve four of the five methods. `get`, `note` (as a comment, arguably better than a bd note), `release`, and `children` (as sub-issues) all map. A blocker-aware ready query is not a GitHub primitive, but the graph can be fetched and readiness computed client-side in tens of lines.

## Decision

**Stay on beads.** The deciding argument is not the API gap, and it is worth writing down which argument actually decided it, because the obvious one is weaker than it looks.

**Agent-generated coordination state does not belong in a human communication medium.** GitHub Issues has watchers, notifications, and search. A decomposition that produces a dozen sub-issues spams everyone subscribed and buries the human backlog under machine scratch work. Beads is a coordination medium with no audience, and that is what this workload needs.

Supporting, in order:

- **The atomic claim.** `bd ready --claim` is the one primitive GitHub cannot serve. There is no compare-and-swap on assignment, so two dispatchers both assign and both succeed. This matters exactly as much as concurrency does, and less than it first appears while milhouse is the sole dispatcher on one machine, where a local lock is the same guarantee.
- **Local, fast, offline, unmetered.** Milliseconds against hundreds, no rate limit, and it works in a repository with no remote at all — which is what the scratch-repo testing recipe depends on.
- **`merge-slot`, `gate`, and `swarm`** are purpose-built multi-agent coordination primitives with no GitHub equivalent. [ADR 0020](0020-a-lane-is-a-herdr-worktree.md) leans on the first of them.

## Consequences

- **Two sync operations stay.** `git push` and `bd dolt push` are both required to publish a unit of work, and forgetting the second leaves issue data on one machine. That cost is real and is accepted.
- **CI does not verify milhouse's work.** `[verify] command` runs a local approximation of the gate ([ADR 0016](0016-milhouse-verifies.md)) rather than the gate itself. Moving to PRs would have fixed that and does not.
- **Setup burden stays.** A repository needs `bd init`, and agents need the beads instructions in `AGENTS.md`.
- **`Tracker` stays a Protocol,** but this is not a free hedge. A GitHub implementation would have to redefine `claim` as advisory-plus-local-lock rather than atomic, which is a contract change, not a swap. Anything built on claim atomicity is built on beads.

## Revisit when

The issues stop being machine coordination state and become durable backlog you would defend to a collaborator. That is the condition that flips it, and it flips it decisively: at that point the visibility, the PR linkage, and the single sync all start earning, and beads becomes a second database maintained for no audience.
