"""The seam between something happening and how it looks.

Every progress line in ``milhouse run`` used to go straight from the code that
noticed something to ``typer.echo``, formatted at the call site: ``session.py``
baked in a two-space indent, and ``step.py`` composed its own arrows. There was
nothing between the code that knew what happened and the terminal that drew it
(:doc:`ADR 0026
<../../docs/decisions/0026-the-progress-channel-is-events-and-the-terminal-is-one-renderer>`).

:class:`Event` is the typed record that replaces the pre-formatted string, and
:class:`Renderer` is the one contract a terminal implements over a stream of
them. :class:`PlainRenderer` reproduces today's line-per-event output for every
kind but one, so that redirection and CI logs see no change: a ``heartbeat``
fires on every poll regardless of whether anything changed, so it prints only
when the turn's ``state`` changes or :data:`PLAIN_KEEPALIVE_MS` has passed since
the last one shown for that turn, rather than once per poll.
:class:`LiveRenderer` is the second: a lane table redrawn in place, chosen
instead when stdout is a capable terminal. Both are genuinely two
implementations of one contract rather than one code path threaded through
``step.py``, ``run.py``, and ``parallel.py`` with a flag in it — which is the
point of the seam.

:func:`about` and :func:`arrow` are the two pieces of formatting that used to
live at the call site — ``session.py``'s indent and ``step.py``'s arrows —
moved here because deciding how a line looks is this module's job now, even
though a call site still asks for it by name when it builds an
:class:`Event`'s :attr:`~Event.text`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Protocol, runtime_checkable

import typer
from rich.console import Console
from rich.live import Live

__all__ = [
    "PLAIN_KEEPALIVE_MS",
    "Event",
    "EventKind",
    "LiveRenderer",
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


_TITLE_RE = re.compile(r"^iteration \d+: \S+ (?P<title>.+)$")
_ATTEMPT_SUFFIX_RE = re.compile(r" \(attempt \d+\)$")
_OUTCOME_RE = re.compile(r"→ (?P<outcome>[^:]+):")


def _parse_title(text: str) -> str:
    """The issue's title, out of a ``started`` event's finished sentence.

    ``Event`` has no field for it (:doc:`ADR 0026
    <../../docs/decisions/0026-the-progress-channel-is-events-and-the-terminal-is-one-renderer>`
    names seven kinds and none of them is "the title"), and the table needs one
    for its title column. ``step._prepare`` builds ``started`` text in exactly
    one shape — ``f"iteration {number}: {issue.id} {issue.title}{suffix}"`` —
    so parsing it back out is reading the one sentence that already has it
    rather than growing the event schema for a single column.
    """
    match = _TITLE_RE.match(text)
    if not match:
        return ""
    return _ATTEMPT_SUFFIX_RE.sub("", match.group("title"))


def _parse_outcome(text: str) -> str:
    """The outcome word, out of a ``settled`` event's ``about(id, arrow(...))`` text."""
    match = _OUTCOME_RE.search(text)
    return match.group("outcome") if match else text


def _merge_word(text: str) -> str | None:
    """How a ``merged`` event's text says the branch landed, or ``None`` before it has.

    Only the concluding line — the one with an arrow — says how the merge
    turned out; the "merging X into Y in Z" line that starts it carries no
    verdict yet.
    """
    if "→" not in text:
        return None
    tail = text.split("→", 1)[1].strip()
    if tail.startswith("is red with"):
        return "merged (red)"
    unlanded = "was not merged into" in tail or "conflicts with" in tail
    if unlanded or tail.startswith("could not merge"):
        return "unmerged"
    return "merged"


def _format_elapsed(seconds: float) -> str:
    """A duration as ``4m12s``, or ``45s`` under a minute."""
    total = max(int(seconds), 0)
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


@dataclass
class _Row:
    """One lane's row, built up from whatever events have touched it so far."""

    issue_id: str
    title: str = ""
    mark: str = "*"
    status: str = "started"
    branch: str = ""
    merged: str = ""
    started: float = 0.0
    elapsed_ms: int | None = None


class LiveRenderer:
    """A lane table redrawn in place, with milestones scrolling above it.

    ``rich.Live`` owns the redraw; this class owns turning an :class:`Event`
    stream into the table it draws. That split is what :meth:`frame` is for:
    it renders the table's current text with no ``Live`` involved, so the
    whole state machine — what a lane's row looks like after any sequence of
    events — is tested by feeding events to :meth:`handle` and reading
    :meth:`frame` back, no terminal required.

    Only ``started``, ``dispatched``, and ``heartbeat`` change a row.
    ``settled``, ``merged``, ``halted``, and ``note`` carry a finished
    sentence instead, and are printed through :attr:`console` rather than
    folded into a cell — while a ``Live`` is active, printing through its own
    console inserts that line above the redrawn region and lets it scroll
    normally, which is the epic's "milestones scrolling above it in ordinary
    scrollback." ``settled`` does both: the sentence scrolls, and the row it
    concluded moves above the rule with its outcome.

    ``auto_refresh`` is off. Every :meth:`handle` call redraws once, so the
    table's cadence is the event stream's own — a poll every few seconds, not
    a timer thread painting on a schedule nothing here controls. Before
    ``Live`` is started, that redraw is skipped rather than half-done, which
    is what lets :meth:`frame` be read straight off :meth:`handle` calls with
    no ``Live`` ever entered.

    No alt screen (``screen=False``, ``rich.Live``'s own default) and no
    keybindings, so Ctrl-C stays Ctrl-C. Whatever interrupts a run still runs
    this object's ``__exit__``, by ordinary context-manager semantics, so the
    cursor ``Live.start()`` hid is always restored and the last frame is what
    is left on the screen.
    """

    def __init__(
        self,
        *,
        console: Console | None = None,
        scope: str = "",
        max_iterations: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Set up an empty table.

        Args:
            console: Where the table (and the milestones above it) are drawn.
                Defaults to a console over stdout; a caller that wants to
                force terminal behaviour for a non-terminal file, or capture
                output for a test, passes its own.
            scope: The header's ``scope`` line — a run's target description,
                static for its whole lifetime and not something any event
                carries.
            max_iterations: The ceiling the header's ``turn N/M`` counts
                against. Also static, also not an event field.
            clock: How elapsed time is measured. Injectable so a test can feed
                a sequence of events and know exactly what "12m" is measured
                from, without a real run taking twelve minutes.
        """
        self.console = console or Console()
        self._scope = scope
        self._max_iterations = max_iterations
        self._clock = clock
        self._live = Live(console=self.console, screen=False, transient=False, auto_refresh=False)
        self._active: dict[str, _Row] = {}
        self._settled: list[_Row] = []
        self._used = 0
        self._run_started = clock()
        self._live.update(self.frame())

    def __enter__(self) -> LiveRenderer:
        """Start redrawing in place."""
        self._live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop redrawing, leaving the last frame and the milestones above it on screen."""
        self._live.__exit__(exc_type, exc, tb)

    def handle(self, event: Event) -> None:
        """Update the table, or print a milestone line above it, and redraw."""
        if event.kind == "started" and event.issue_id:
            self._used += 1
            row = self._row(event.issue_id)
            row.title = _parse_title(event.text) or row.title
            row.mark = "*"
            row.status = "started"
            row.started = self._clock()
        elif event.kind == "dispatched" and event.issue_id:
            row = self._row(event.issue_id)
            row.status = "dispatched"
            row.branch = event.lane or row.branch
        elif event.kind == "heartbeat" and event.issue_id:
            row = self._row(event.issue_id)
            row.status = event.state or row.status
            row.branch = event.lane or row.branch
            row.elapsed_ms = event.elapsed_ms
        elif event.kind == "merged":
            row = self._active.get(event.issue_id) if event.issue_id else None
            if row is not None:
                row.branch = event.lane or row.branch
                word = _merge_word(event.text)
                if word:
                    row.merged = word
                else:
                    row.status = "merging"
            self.console.print(event.text)
        elif event.kind == "settled" and event.issue_id:
            row = self._active.pop(event.issue_id, None) or _Row(issue_id=event.issue_id)
            outcome = _parse_outcome(event.text)
            row.mark = "v" if outcome == "success" else "x"
            row.status = outcome
            row.elapsed_ms = None
            self._settled.append(row)
            self.console.print(event.text)
        elif event.kind in ("halted", "note"):
            self.console.print(event.text)
        self._live.update(self.frame(), refresh=self._live.is_started)

    def frame(self) -> str:
        """The table as it stands right now, as plain text — one redraw's worth.

        Pure: reading it back after a sequence of :meth:`handle` calls is the
        whole of this class's test surface, and it touches neither
        :attr:`console` nor ``Live``.
        """
        rows = [*self._settled, *self._active.values()]
        issue_w = max((len(row.issue_id) for row in rows), default=0)
        title_w = max((len(row.title) for row in rows), default=0)
        status_w = max((len(row.status) for row in rows), default=0)

        settled_lines = [
            self._line(row, issue_w, title_w, status_w, row.merged) for row in self._settled
        ]
        active_lines = [
            self._line(row, issue_w, title_w, status_w, self._detail(row))
            for row in self._active.values()
        ]
        body = list(settled_lines)
        if settled_lines and active_lines:
            width = max(len(line) for line in (*settled_lines, *active_lines))
            body.append("-" * width)
        body.extend(active_lines)
        return "\n".join([self._header(), "", *body]) if body else self._header()

    def _row(self, issue_id: str) -> _Row:
        row = self._active.get(issue_id)
        if row is None:
            row = _Row(issue_id=issue_id)
            self._active[issue_id] = row
        return row

    def _detail(self, row: _Row) -> str:
        elapsed = _format_elapsed(
            row.elapsed_ms / 1000 if row.elapsed_ms is not None else self._clock() - row.started
        )
        return f"{elapsed}  {row.branch}" if row.branch else elapsed

    def _header(self) -> str:
        turn = f"turn {self._used}"
        if self._max_iterations:
            turn += f"/{self._max_iterations}"
        elapsed = _format_elapsed(self._clock() - self._run_started)
        scope = f"scope   {self._scope}" if self._scope else "scope"
        return f"{scope}     {turn}   {elapsed}"

    @staticmethod
    def _line(row: _Row, issue_w: int, title_w: int, status_w: int, tail: str) -> str:
        line = (
            f"  {row.mark}  {row.issue_id.ljust(issue_w)}  {row.title.ljust(title_w)}  "
            f"{row.status.ljust(status_w)}   {tail}"
        )
        return line.rstrip()
