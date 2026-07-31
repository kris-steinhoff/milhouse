"""The seam between something happening and how it looks.

Every progress line in ``milhouse run`` used to go straight from the code that
noticed something to ``typer.echo``, formatted at the call site: ``session.py``
baked in a two-space indent, and ``step.py`` composed its own arrows. There was
nothing between the code that knew what happened and the terminal that drew it
(:doc:`ADR 0026
<../../docs/decisions/0026-the-progress-channel-is-events-and-the-terminal-is-one-renderer>`).

:class:`Event` is the typed record that replaces the pre-formatted string, and
:class:`Renderer` is the one contract a terminal implements over a stream of
them. :class:`PlainRenderer` reproduces today's line-per-event output for
every kind but one: a ``heartbeat`` fires on every poll regardless of whether
anything changed, so it prints only when the turn's ``state`` changes or
:data:`PLAIN_KEEPALIVE_MS` has passed since the last one shown for that turn,
rather than once per poll. A live, redrawn-in-place renderer is a later issue
in the same epic, and the point of the seam is that it is a second
:class:`Renderer` rather than a second code path threaded through
``step.py``, ``run.py``, and ``parallel.py``.

:func:`about` and :func:`arrow` are the two pieces of formatting that used to
live at the call site — ``session.py``'s indent and ``step.py``'s arrows —
moved here because deciding how a line looks is this module's job now, even
though a call site still asks for it by name when it builds an
:class:`Event`'s :attr:`~Event.text`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import typer

__all__ = [
    "PLAIN_KEEPALIVE_MS",
    "Event",
    "EventKind",
    "PlainRenderer",
    "Renderer",
    "about",
    "arrow",
]

EventKind = Literal["started", "dispatched", "heartbeat", "settled", "merged", "halted", "note"]
"""What kind of thing happened, named concretely by ADR 0026's table.

Enough for a renderer to rebuild a lane table from the stream alone:
``started`` and ``dispatched`` open a turn, ``heartbeat`` observes one already
running, ``settled`` and ``merged`` close it out, ``halted`` says why the run
stopped, and ``note`` is bookkeeping none of the others fit.
"""


@dataclass(frozen=True)
class Event(str):
    """One thing that happened, for a renderer to show however it shows things.

    A subclass of ``str`` rather than a plain record, so that code written
    against the old ``Reporter = Callable[[str], None]`` — most of the test
    suite's ``session.report = lines.append`` doubles among them — keeps
    working against :attr:`text` without having to know an event stopped
    being a string. The structured fields are what a renderer that wants more
    than a line of text reads instead.

    Attributes:
        kind: Which row of ADR 0026's table this is.
        text: The finished sentence. Every call site already has the right
            words — merge outcomes, verify commands, and error text are each
            formatted in ways specific to their call site — so this is what
            :class:`PlainRenderer` prints verbatim.
        issue_id: Whose turn this is about, when there is one.
        lane: The workspace id or branch the call site had, when there is one.
        state: Heartbeat only: what the turn is doing right now.
        elapsed_ms: Heartbeat only: time since dispatch, in milliseconds.
    """

    kind: EventKind
    text: str
    issue_id: str | None = None
    lane: str | None = None
    state: str | None = None
    elapsed_ms: int | None = None

    def __new__(
        cls,
        kind: EventKind,
        text: str,
        issue_id: str | None = None,
        lane: str | None = None,
        state: str | None = None,
        elapsed_ms: int | None = None,
    ) -> Event:
        """Build the underlying ``str`` from ``text``; the dataclass fields attach after."""
        return str.__new__(cls, text)


@runtime_checkable
class Renderer(Protocol):
    """A place a stream of events becomes something on a screen.

    Structural rather than declared, so a future renderer — the live table,
    or a JSONL one — needs no import of this module to satisfy the contract.
    """

    def handle(self, event: Event) -> None:
        """Show one event, however this renderer shows things."""


def about(issue_id: str, text: str) -> str:
    """A line indented under its turn and labelled with whose turn it is.

    Serially the label is redundant, because the ``iteration N: <issue>`` line
    above it is the only turn in progress. Concurrently it is the whole line:
    several turns interleave their progress on one terminal, and an
    unlabelled ``→ success`` says nothing about which of them succeeded
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

    Args:
        issue_id: The turn's issue.
        text: What to say about it.

    Returns:
        The line to report.
    """
    return f"  {issue_id}  {text}"


def arrow(text: str) -> str:
    """A line reporting how something already announced turned out.

    The arrow is what tells a ``started``/``dispatched`` announcement apart
    from the ``settled``/``merged`` line that concludes it, in a run where
    several turns interleave their progress on one terminal.
    """
    return f"→ {text}"


PLAIN_KEEPALIVE_MS = 60_000
"""How long a turn's heartbeat may stay silent in :class:`PlainRenderer`.

Deliberately not ``[run] poll_ms``: how often milhouse asks herdr about a
lane and how often a human wants to be told a long turn is still alive are
unrelated, and coupling the two is what produced the wall of identical
``"... is still working"`` lines this constant exists to stop
(:doc:`ADR 0026
<../../docs/decisions/0026-the-progress-channel-is-events-and-the-terminal-is-one-renderer>`).
"""


class PlainRenderer:
    """One line per event, in order, with a repeating heartbeat collapsed.

    Every other kind prints :attr:`Event.text` verbatim, unchanged from
    before the seam existed. A ``heartbeat`` is different: :func:`reap
    <milhouse.step.reap>` emits one for every turn still in flight on every
    poll, whether or not anything about it changed, so this renderer shows
    one only when its :attr:`~Event.state` differs from the last one shown
    for that :attr:`~Event.issue_id`, or when :data:`PLAIN_KEEPALIVE_MS` has
    passed since the last one shown — whichever comes first, so a long turn
    whose state never changes still says something occasionally instead of
    going silent, in a piped log, for its whole duration.
    """

    def __init__(self) -> None:
        """Start with no turn's heartbeat shown yet."""
        self._last_heartbeat: dict[str | None, tuple[str | None, int]] = {}

    def handle(self, event: Event) -> None:
        """Print ``event``'s text, unless it's a heartbeat repeating too soon."""
        if event.kind == "heartbeat" and not self._due(event):
            return
        typer.echo(event.text)

    def _due(self, event: Event) -> bool:
        """Whether this heartbeat is new, or old, enough to be worth showing.

        Recorded by ``elapsed_ms`` rather than wall-clock time read here, so
        the cadence follows the turn's own clock and a test can drive it
        without mocking time.
        """
        elapsed = event.elapsed_ms or 0
        last = self._last_heartbeat.get(event.issue_id)
        if last is not None and last[0] == event.state and elapsed - last[1] < PLAIN_KEEPALIVE_MS:
            return False
        self._last_heartbeat[event.issue_id] = (event.state, elapsed)
        return True
