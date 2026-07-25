# 0011 — Exit the agent with keys, fall back to pane churn

**Status:** accepted

## Context

[ADR 0003](0003-agents-run-in-herdr-panes.md) needs the pane back at an
interactive shell prompt at the end of every iteration, because that is what
`herdr agent start` requires for the next one. The agent is a TUI, so "exit" is a
key sequence, and the right sequence is agent-specific.

## Decision

Send the configured `[agent] exit_keys` (default `["ctrl-c", "ctrl-c", "ctrl-d"]`
for `claude`) with `herdr agent send-keys`, then confirm the pane is back at a
shell prompt by checking `herdr pane get` no longer reports an agent.

If it is not back within a few seconds, fall back to closing the pane and
splitting a fresh one:

```sh
herdr pane close <pane_id>
herdr pane split <other_pane_id> --cwd <repo>
```

The new pane id is recorded in `state.json`, so the next iteration uses it.

Two `ctrl-c` rather than one: the first interrupts whatever the agent is doing,
the second dismisses its confirmation, and `ctrl-d` exits the now-idle prompt.

## Consequences

- The common path costs nothing: three keystrokes and a status check.
- The fallback is unambiguous but costs a pane churn, and it loses the pane's
  scrollback. The transcript is captured *before* the exit sequence for exactly
  this reason, so a post-mortem still has it.
- `exit_keys` being configurable is what makes a second agent backend plausible
  ([ADR 0003](0003-agents-run-in-herdr-panes.md)) without new code. It is also
  the setting most likely to be wrong for a kind nobody has tried.
- The pane is never closed while an agent is still `working`. milhouse only
  reaches teardown after the turn has settled or timed out, and a timed-out turn
  gets the same interrupt sequence first.
