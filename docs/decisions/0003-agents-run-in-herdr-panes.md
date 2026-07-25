# 0003 — Every agent runs in a herdr pane, restarted per iteration

**Status:** accepted

## Context

The defining property of a ralph loop is a fresh context window every iteration.
A long-lived agent session accumulates context and stops behaving like ralph.

Separately, an interactive agent that hits a permission prompt just sits there.
Something has to notice and tell a human.

## Decision

One workspace per task, one pane, one fresh agent per iteration:

```sh
# once per run
herdr workspace create --cwd <repo> --label "milhouse:<slug>" --no-focus
#   → workspace_id, pane_id (the pane sits at a shell prompt)

# per iteration
herdr agent start milhouse-<slug> --kind claude --pane <pane_id> \
    --timeout 60000 -- <agent args>
herdr agent prompt milhouse-<slug> "<rendered prompt>" \
    --wait --until idle --until done --until blocked --timeout <ms>
herdr agent read milhouse-<slug> --source visible --lines 400 --format text
herdr pane send-keys <pane_id> ctrl+c ctrl+c ctrl+d          # back to the shell
```

Two corrections the design's version of this needed, both found by driving the
real server:

- **`--until done`, not just `--until idle`.** claude settles in herdr's `done`
  state at the end of a turn, not `idle`. Waiting on `idle` alone times out on
  every successful turn. `done` is also what `agent prompt --wait` matches by
  default when no `--until` is given.
- **`ctrl+c`, not `ctrl-c`.** herdr rejects the hyphenated form with
  `invalid_key`. The short forms `c-c` and `C-c` are accepted, but not for every
  key (`c-d` is not), so `ctrl+` is the spelling to use. See
  [ADR 0011](0011-exiting-the-agent.md).

## Consequences

- `herdr agent start` requires the pane to be at an interactive shell prompt and
  returns only once the agent is detected and ready. Startup is a synchronous,
  checkable step rather than a sleep.
- Because the agent is freshly started and therefore `idle`, the
  `idle → working → idle` transition around a prompt is unambiguous.
  (`agent prompt --wait` warns it cannot distinguish turns if the agent is
  *already* working, which never applies here.)
- `--until idle --until done --until blocked` is the whole turn-completion
  mechanism: one blocking subprocess per iteration, which is exactly what a
  sequential loop wants.
- herdr reports a distinct `blocked` state when the agent is waiting on a human.
  That is the main payoff of panes over a headless runner: milhouse can stop and
  ask, instead of failing the iteration. See
  [ADR 0009](0009-permission-posture.md).
- A human can attach to the workspace and watch, or intervene, at any point.
- The `--kind` enum already covers `codex`, `amp`, `opencode`, `gemini` and
  others, so a second agent backend should be a config change. That claim is not
  yet tested; see [the open list](README.md#still-open).

The cost is a pane and an agent startup per iteration, roughly a few seconds
each, and a dependency on herdr's agent detection being right about the
configured `--kind`.
