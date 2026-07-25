# 0001 — Shell out to `bd` and `herdr` and parse JSON

**Status:** accepted

## Context

milhouse drives two external tools. `bd` emits JSON under `--json`. herdr's
`api`, `agent`, `pane`, and `workspace` subcommands emit JSON by default.

herdr also has a socket API, and it is genuinely good: newline-delimited JSON
over `$HERDR_SOCKET_PATH`, 89 methods, a versioned protocol (17), and a
machine-readable contract via `herdr api schema --json`. There is no official SDK
in any language, but the protocol is small enough that a client would be one
file.

## Decision

Shell out to both CLIs and parse their JSON. Every subprocess call goes through
one audited helper, `proc.run()` / `proc.run_json()`, so it can be faked in
tests. `herdr.py` stays a narrow client so swapping the transport later is one
file, not a refactor.

## Consequences

What the CLI buys: one dependency instead of two (transport plus schema), no
pinned protocol version to bump when herdr does, and a small surface while the
interesting problems are elsewhere.

What it costs, stated plainly so the trade is revisitable:

- **No `events.subscribe`.** The socket can push `pane.agent_status_changed`;
  the CLI cannot. For a sequential v1 this costs nothing, because
  `herdr agent prompt --wait --until idle --until blocked` already blocks until
  the turn settles. It starts to bite at concurrency, where the CLI needs one
  blocking `herdr agent wait` process per pane against the socket's single
  connection watching all of them.
- **No event log** for post-mortems on a run that went sideways. The per-turn
  transcript in `.milhouse/runs/<task>/iter-NNN.term` is the substitute.
- **Coarser errors.** Parsing stderr rather than a structured `error.code`.

## Revisit when

Concurrency lands. Waiting on N panes is the point where one connection watching
everything beats N blocking processes, and it is the reason to write the socket
client.
