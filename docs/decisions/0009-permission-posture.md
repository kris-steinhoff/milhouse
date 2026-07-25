# 0009 — Supervised by default; unattended is opt-in and explicit

**Status:** accepted

## Context

An interactive agent that hits a permission prompt stops and waits. herdr
surfaces that as `blocked`, which milhouse can act on. A supervised run is
therefore genuinely fine: a human attaches to the pane and approves.

A long unattended run is different. Every permission prompt costs the loop
`blocked_timeout_ms` of wall clock and then a skipped issue. In practice
unattended runs need `--dangerously-skip-permissions`, which is exactly as
alarming as it sounds.

## Decision

milhouse passes **no permission flags of its own**. The agent runs with whatever
`[agent] args` says, which is empty by default, so an out-of-the-box run is
supervised and safe.

Making a run unattended is a deliberate, visible act in the repo's config:

```toml
[agent]
args = ["--dangerously-skip-permissions"]
```

`--on-blocked wait` (the default) is what makes the supervised posture work:
milhouse stops, prints which workspace to attach to, and waits up to
`blocked_timeout_ms` for the agent to leave `blocked`.

## Consequences

- The dangerous setting is in a committed file with a name that shows up in code
  review, rather than a flag someone typed once at 2am.
- The default posture cannot silently become the unattended one.
- Before running with permissions skipped, isolate the working copy. Today that
  means running milhouse inside a `herdr worktree create` worktree with
  `branch_strategy = "current"` ([ADR 0007](0007-branch-per-task.md)), or a
  container. milhouse does not do this for you, and will not warn you.
- `--on-blocked skip` exists for a middle posture: keep permission prompts on,
  but do not stall the run on them. The issue is left for a human and the loop
  moves to the next one.
