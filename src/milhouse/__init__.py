"""milhouse — take a tracker's ready queue and drive a ralph loop over it.

milhouse wires three existing tools together: `beads
<https://github.com/steveyegge/beads>`_ for durable, dependency-aware issue
tracking, `herdr <https://herdr.dev>`_ for the terminal panes the agents run in,
and an interactive coding agent that does one unit of work per iteration.

The defining property of a `ralph loop <https://ghuntley.com/ralph/>`_ is a fresh
context window every iteration. milhouse gets that by starting a new agent in the
pane each time and exiting it when the turn ends. State lives in beads and git,
never in an accumulating chat session.

See ``docs/architecture.md`` for the module boundaries and the data flow.
"""
