"""The seam between something happening and how it looks.

Every progress line in ``milhouse run`` used to go straight from the code that
noticed something to ``typer.echo``, formatted at the call site: ``session.py``
baked in a two-space indent, and ``step.py`` composed its own arrows. There was
nothing between the code that knew what happened and the terminal that drew it
(:doc:`ADR 0026
<../../docs/decisions/0026-the-progress-channel-is-events-and-the-terminal-is-one-renderer>`).

:class:`Event` is the typed record that replaces the pre-formatted string, and
:class:`Renderer` is the one contract a terminal implements over a stream of
them. :class:`PlainRenderer` is the only implementation this issue ships: it
reproduces today's line-per-event output, byte for byte, so that redirection
and CI logs see no change. A live, redrawn-in-place renderer is a later issue
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

__all__ = ["Event", "EventKind", "PlainRenderer", "Renderer", "about", "arrow"]

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


class PlainRenderer:
    """One line per event, in order — today's output, unchanged.

    The only renderer this issue ships. It reproduces ``milhouse run``'s
    output byte for byte, because every call site already builds
    :attr:`Event.text` to be exactly what it echoed before the seam existed;
    this renderer's whole job is to print it.
    """

    def handle(self, event: Event) -> None:
        """Print ``event``'s text, the way ``typer.echo`` always did."""
        typer.echo(event.text)
