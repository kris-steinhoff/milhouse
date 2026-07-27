"""milhouse — decompose a task into tracked issues, then drive a ralph loop over them.

milhouse wires three existing tools together and keeps the loop running
unattended: `beads <https://github.com/steveyegge/beads>`_ for durable,
dependency-aware issue tracking, `herdr <https://herdr.dev>`_ for the terminal
panes the agents run in, and an interactive coding agent that does one unit of
work per iteration.

The defining property of a `ralph loop <https://ghuntley.com/ralph/>`_ is a fresh
context window every iteration. milhouse gets that by starting a new agent in the
pane each time and exiting it when the turn ends. State lives in beads and git,
never in an accumulating chat session.

See ``docs/architecture.md`` for the module boundaries and the data flow.
"""
