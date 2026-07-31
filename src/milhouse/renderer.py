"""The seam between something happening and how it looks.

Every progress line in ``milhouse run`` used to go straight from the code that
noticed something to ``typer.echo``, formatted at the call site: ``session.py``
baked in a two-space indent, and ``step.py`` composed its own arrows. There was
nothing between the code that knew what happened and the terminal that drew it
(:doc:`ADR 0026
<../../docs/decisions/0026-the-progress-channel-is-events-and-the-terminal-is-one-renderer>`).

:class:`Event` is the typed record that replaces the pre-formatted string, and
:class:`Renderer` is the one contract a terminal implements over a stream of
them. :class:`PlainRenderer` reproduces today's line-per-event output, byte
for byte, so that redirection and CI logs see no change; :class:`NullRenderer`
discards everything, for ``--quiet``. A live, redrawn-in-place renderer is a
later issue in the same epic, and the point of the seam is that it is a
second :class:`Renderer` rather than a second code path threaded through
``step.py``, ``run.py``, and ``parallel.py``.

:func:`select_renderer` is the pure function that decides which of them a
command gets, from ``isatty``, the flags, and the environment — never from
globals — so the decision is a unit test rather than a subprocess test.

:func:`about` and :func:`arrow` are the two pieces of formatting that used to
live at the call site — ``session.py``'s indent and ``step.py``'s arrows —
moved here because deciding how a line looks is this module's job now, even
though a call site still asks for it by name when it builds an
:class:`Event`'s :attr:`~Event.text`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast, runtime_checkable

import typer

from .errors import ConfigError

__all__ = [
    "Event",
    "EventKind",
    "NullRenderer",
    "OutputMode",
    "PlainRenderer",
    "Renderer",
    "about",
    "arrow",
    "select_renderer",
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


class PlainRenderer:
    """One line per event, in order — today's output, unchanged.

    Reproduces ``milhouse run``'s output byte for byte, because every call
    site already builds :attr:`Event.text` to be exactly what it echoed
    before the seam existed; this renderer's whole job is to print it.
    """

    def handle(self, event: Event) -> None:
        """Print ``event``'s text, the way ``typer.echo`` always did."""
        typer.echo(event.text)


class NullRenderer:
    """Discards every event. What ``--quiet`` wires up.

    The end-of-run report is not an event and does not go through a renderer
    (:doc:`../../docs/decisions/0026-the-progress-channel-is-events-and-the-terminal-is-one-renderer`),
    so a command still prints it after the run; this is what makes that report
    the whole output.
    """

    def handle(self, event: Event) -> None:
        """Do nothing."""


OutputMode = Literal["live", "plain", "quiet"]
"""Which renderer a command gets, named the way :func:`select_renderer` returns it.

``live`` is the lane table redrawn in place (a later issue in this epic;
nothing builds it yet). ``plain`` is :class:`PlainRenderer`, today's
line-per-event output. ``quiet`` is :class:`NullRenderer`: nothing prints
until the end-of-run report.
"""

_OUTPUT_MODES: frozenset[str] = frozenset(("live", "plain", "quiet"))


def select_renderer(
    *, isatty: bool, verbose: bool, quiet: bool, environ: Mapping[str, str]
) -> OutputMode:
    """Decide which renderer a command gets.

    A pure function over exactly what the decision needs, rather than a
    reading of ``sys.stdout`` or ``os.environ`` buried in ``cli.py`` — the
    same shape :func:`milhouse.config.load` uses for its own layering, so
    this is a unit test rather than a subprocess test.

    Precedence, highest first:

    1. ``quiet`` — the whole point of asking for it is silence, so nothing
       below this line gets to argue.
    2. ``verbose`` — implies ``plain``. Its whole point is a greppable
       transcript alongside the DEBUG logging it already turns on, and a
       region that redraws would fight it.
    3. ``MILHOUSE_OUTPUT`` in ``environ`` — an explicit ``live``, ``plain``,
       or ``quiet``, following :mod:`milhouse.config`'s own env precedence:
       env beats the auto-detected default, and flags still beat env.
    4. ``NO_COLOR`` in ``environ`` — present at all, any value, per
       `no-color.org <https://no-color.org>`_. ``live`` redraws in colour, so
       this forces ``plain``.
    5. ``isatty`` — ``live`` when stdout is a TTY, ``plain`` otherwise, so
       redirection, CI logs, and ``2>&1 | tee`` behave exactly as they do
       today.

    Args:
        isatty: Whether stdout is a terminal (``sys.stdout.isatty()``).
        verbose: The ``--verbose`` flag.
        quiet: The ``--quiet`` flag.
        environ: The process environment (``os.environ``).

    Returns:
        Which renderer to build.

    Raises:
        ConfigError: ``MILHOUSE_OUTPUT`` is set to something other than
            ``live``, ``plain``, or ``quiet``.
    """
    if quiet:
        return "quiet"
    if verbose:
        return "plain"

    output = environ.get("MILHOUSE_OUTPUT", "")
    if output:
        if output not in _OUTPUT_MODES:
            raise ConfigError(f"MILHOUSE_OUTPUT must be one of live, plain, quiet, got {output!r}")
        return cast(OutputMode, output)

    if "NO_COLOR" in environ:
        return "plain"

    return "live" if isatty else "plain"
