# 0012 — No token or cost caps in v1

**Status:** accepted, and less pressing since [ADR 0014](0014-step-is-the-primitive.md)

Still no cost tracking, for the reason below. It matters less now: a supervised run is bounded by a person's attention rather than by an iteration ceiling.

## Context

`--max-iterations` bounds turns, not spend. The ralph writeups cite figures like $600 for an overnight run, so this is not a theoretical concern.

## Decision

milhouse does not track or cap token spend in v1. `--max-iterations` and `--max-attempts` are the only budget controls, and they are documented as bounding _turns_, not money.

## Consequences

- The honest reason: milhouse drives the agent through a terminal pane. It sees keystrokes and rendered output, not an API response with a usage block. Scraping a token count out of a TUI's status line would be brittle and version-specific, and wrong numbers are worse than no numbers.
- An overnight run's cost is bounded only by `max_iterations × cost-per-turn`, and the second factor is unknown to milhouse. Users setting up an unattended run should set `max_iterations` from what they are willing to spend, and check it against their provider's own usage reporting.
- The place this would change is the agent's own reporting. If the agent can be asked to write per-turn usage somewhere milhouse can read (a file, a bead comment), the loop could enforce a budget. That is a real design, just not one worth building before the loop itself is proven.
