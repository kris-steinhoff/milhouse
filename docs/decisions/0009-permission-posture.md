# 0009 — Supervised by default; unattended is opt-in and explicit

**Status:** accepted, and deferred by [ADR 0014](0014-step-is-the-primitive.md)

Supervised is no longer the default, it is the only mode. Everything below about making a run unattended describes what the ralph policy has to answer, not a setting that does anything today.

## Context

An interactive agent that hits a permission prompt stops and waits. herdr surfaces that as `blocked`, which milhouse can act on. A supervised run is therefore genuinely fine: a human attaches to the pane and approves.

A long unattended run is different. Every permission prompt costs the loop `blocked_timeout_ms` of wall clock and then a skipped issue. In practice unattended runs need `--dangerously-skip-permissions`, which is exactly as alarming as it sounds.

## Decision

milhouse passes **no permission flags of its own**. The agent runs with whatever `[agent] args` says, which is empty by default, so an out-of-the-box run is supervised and safe.

Making a run unattended is a deliberate, visible act in the repo's config:

```toml
[agent]
args = ["--dangerously-skip-permissions"]
```

`--on-blocked wait` (the default) is what makes the supervised posture work: milhouse stops, prints which workspace to attach to, and waits up to `blocked_timeout_ms` for the agent to leave `blocked`.

### The middle ground does not exist

The obvious compromise — grant a _scoped_ tool allowlist instead of skipping permissions wholesale — does not work, and a dogfood run is what established that. Given

```toml
args = ["--allowedTools", "Write,Edit,Bash(git:*),Bash(bd:*),Bash(python3:*)"]
```

every issue still blocked, on this:

```
ls docs && echo "---" && which ruff flake8 pytest black; python -m pytest --version
```

An agent writing its own shell commands composes them freely, and a composed command matches no single prefix pattern. Prefix allowlists work for known call sites, not for an agent authoring commands as it goes. Widening the list until it stops blocking arrives at `Bash` unscoped, which is the unattended posture with extra steps.

Note also that `--dangerously-skip-permissions` shows a one-time consent screen. An unattended agent cannot answer it, so the first turn settles normally having produced nothing at all — see [troubleshooting](../troubleshooting.md#the-agent-produced-nothing-and-the-turn-looked-fine). Accept it once interactively before relying on it in a loop.

## Consequences

- The dangerous setting is in a committed file with a name that shows up in code review, rather than a flag someone typed once at 2am.
- The default posture cannot silently become the unattended one.
- Before running with permissions skipped, isolate the working copy. Today that means running milhouse inside a `herdr worktree create` worktree with `branch_strategy = "current"` ([ADR 0007](0007-branch-per-task.md)), or a container. milhouse does not do this for you, and will not warn you.
- `--on-blocked skip` exists for a middle posture: keep permission prompts on, but do not stall the run on them. The issue is left for a human and the loop moves to the next one.
- The real choice is binary: supervise the run, or isolate the working copy and let the agent off the leash. There is no configuration that is both unattended and meaningfully restricted, so do not spend time looking for one.
