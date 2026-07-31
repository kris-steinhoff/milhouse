"""Tests for the event/renderer seam (ADR 0026).

`Event` carries its structured fields separately from `text`, which is the
whole point of the seam: a renderer that wants more than a line can read
`kind`, `issue_id`, and `lane` without parsing a string back apart.

The `LiveRenderer` tests below never start its `Live` — `frame()` reads the
table back as plain text on its own, which is what lets the whole state
machine be driven by feeding `handle()` an event sequence and asserting on
`frame()`, with no terminal involved. The one test that does start it
(`test_a_live_renderer_redraws_in_place...`) proves the redraw itself, against
a `Console` forced to believe it is a terminal rather than a real one.
"""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from milhouse.renderer import (
    PLAIN_KEEPALIVE_MS,
    Event,
    LiveRenderer,
    PlainRenderer,
    about,
    arrow,
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def heartbeat(issue_id: str, state: str, elapsed_ms: int) -> Event:
    return Event(
        "heartbeat",
        f"{issue_id} is still working",
        issue_id=issue_id,
        lane="w1",
        state=state,
        elapsed_ms=elapsed_ms,
    )


def _plain(text: str) -> str:
    """Strip rich's colour and cursor-control codes, for content assertions."""
    return _ANSI.sub("", text)


class _Clock:
    """A clock a test moves by hand, so "12m" means what the test says it does."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _forced_terminal(buffer: io.StringIO) -> Console:
    """A console that believes it is a real terminal, regardless of ``TERM``.

    The suite's own ``_plain_output`` fixture sets ``TERM=dumb`` so CLI
    assertions do not have to account for rich's styling. ``force_terminal``
    alone does not override that — ``Live`` still checks
    ``Console.is_dumb_terminal`` — so the redraw tests read the environment
    through their own mapping instead of the real one.
    """
    return Console(file=buffer, force_terminal=True, width=100, _environ={"TERM": "xterm"})


def test_an_event_carries_its_fields_separately_from_its_text() -> None:
    event = Event("dispatched", "  bd-e.1  → dispatched to w1", issue_id="bd-e.1", lane="w1")

    assert event.kind == "dispatched"
    assert event.issue_id == "bd-e.1"
    assert event.lane == "w1"
    assert event.text == "  bd-e.1  → dispatched to w1"


def test_an_event_without_an_issue_or_lane_leaves_them_none() -> None:
    event = Event("halted", "stopping: ceiling reached")

    assert event.issue_id is None
    assert event.lane is None
    assert event.state is None
    assert event.elapsed_ms is None


def test_a_heartbeat_carries_state_and_elapsed_time() -> None:
    event = Event(
        "heartbeat",
        "bd-e.1 is still working",
        issue_id="bd-e.1",
        lane="w1",
        state="working",
        elapsed_ms=12_000,
    )

    assert event.state == "working"
    assert event.elapsed_ms == 12_000


def test_about_indents_and_labels_the_line_with_its_issue() -> None:
    assert about("bd-e.1", "dispatched to w1") == "  bd-e.1  dispatched to w1"


def test_arrow_marks_a_line_as_a_conclusion() -> None:
    assert arrow("success: closed") == "→ success: closed"


def test_the_plain_renderer_prints_the_events_text(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = PlainRenderer()

    renderer.handle(Event("note", "took over the lock on bd-e from a dead run"))

    assert capsys.readouterr().out == "took over the lock on bd-e from a dead run\n"


# -- the plain renderer's heartbeat dedup ---------------------------------------


def test_a_repeated_heartbeat_in_the_same_state_is_shown_only_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wall of text this issue exists to stop: one poll's worth, not ten."""
    renderer = PlainRenderer()

    for elapsed in (0, 5_000, 10_000, 15_000, 20_000):
        renderer.handle(heartbeat("bd-e.1", "working", elapsed))

    assert capsys.readouterr().out == "bd-e.1 is still working\n"


def test_a_heartbeat_reprints_when_its_state_changes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainRenderer()

    renderer.handle(heartbeat("bd-e.1", "working", 0))
    renderer.handle(heartbeat("bd-e.1", "working", 5_000))
    renderer.handle(heartbeat("bd-e.1", "reaping", 10_000))

    lines = capsys.readouterr().out.splitlines()
    assert lines == ["bd-e.1 is still working", "bd-e.1 is still working"]


def test_a_heartbeat_reprints_once_the_keepalive_cadence_has_passed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A long turn whose state never changes still says something, on its own cadence."""
    renderer = PlainRenderer()

    renderer.handle(heartbeat("bd-e.1", "working", 0))
    renderer.handle(heartbeat("bd-e.1", "working", PLAIN_KEEPALIVE_MS - 1))
    renderer.handle(heartbeat("bd-e.1", "working", PLAIN_KEEPALIVE_MS))

    lines = capsys.readouterr().out.splitlines()
    assert lines == ["bd-e.1 is still working", "bd-e.1 is still working"]


def test_heartbeats_for_different_turns_are_tracked_independently(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainRenderer()

    renderer.handle(heartbeat("bd-e.1", "working", 0))
    renderer.handle(heartbeat("bd-e.2", "working", 0))
    renderer.handle(heartbeat("bd-e.1", "working", 5_000))
    renderer.handle(heartbeat("bd-e.2", "reaping", 5_000))

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "bd-e.1 is still working",
        "bd-e.2 is still working",
        "bd-e.2 is still working",
    ]


# -- the live renderer: a lane table built from the event stream alone --------


def _started(issue_id: str, title: str, *, number: int = 1) -> Event:
    return Event("started", f"iteration {number}: {issue_id} {title}", issue_id=issue_id)


def _dispatched(issue_id: str, lane: str) -> Event:
    return Event(
        "dispatched", about(issue_id, arrow(f"dispatched to {lane}")), issue_id=issue_id, lane=lane
    )


def _heartbeat(issue_id: str, lane: str, *, state: str, elapsed_ms: int) -> Event:
    text = f"{issue_id} is still working" if state == "working" else f"reaping: {issue_id}"
    return Event(
        "heartbeat", text, issue_id=issue_id, lane=lane, state=state, elapsed_ms=elapsed_ms
    )


def _settled(issue_id: str, outcome: str, detail: str) -> Event:
    return Event("settled", about(issue_id, arrow(f"{outcome}: {detail}")), issue_id=issue_id)


def test_an_empty_table_is_just_the_header() -> None:
    renderer = LiveRenderer(scope="every ready issue", max_iterations=50, clock=_Clock())

    assert renderer.frame() == "scope   every ready issue     turn 0/50   0s"


def test_a_started_event_adds_a_row_with_its_title() -> None:
    renderer = LiveRenderer(clock=_Clock())

    renderer.handle(_started("bd-e.1", "add the greet command"))

    lines = renderer.frame().splitlines()
    assert lines[-1] == "  *  bd-e.1  add the greet command  started   0s"


def test_an_attempt_suffix_is_stripped_from_the_title() -> None:
    renderer = LiveRenderer(clock=_Clock())

    text = "iteration 3: bd-e.1 add the greet command (attempt 2)"
    renderer.handle(Event("started", text, issue_id="bd-e.1"))

    assert "add the greet command (attempt 2)" not in renderer.frame()
    assert "add the greet command" in renderer.frame()


def test_a_dispatched_event_adds_the_branch_and_status() -> None:
    renderer = LiveRenderer(clock=_Clock())
    renderer.handle(_started("bd-e.1", "add the greet command"))

    renderer.handle(_dispatched("bd-e.1", "w1"))

    line = renderer.frame().splitlines()[-1]
    assert "dispatched" in line
    assert line.endswith("w1")


def test_a_heartbeat_updates_status_and_shows_its_own_elapsed_time() -> None:
    clock = _Clock()
    renderer = LiveRenderer(clock=clock)
    renderer.handle(_started("bd-e.1", "add the greet command"))
    renderer.handle(_dispatched("bd-e.1", "w1"))
    clock.advance(252)

    renderer.handle(_heartbeat("bd-e.1", "w1", state="working", elapsed_ms=252_000))

    line = renderer.frame().splitlines()[-1]
    assert "working" in line
    assert "4m12s" in line


def test_a_successful_turn_moves_above_the_rule_marked_and_settled() -> None:
    renderer = LiveRenderer(clock=_Clock())
    renderer.handle(_started("bd-e.1", "add the greet command"))
    renderer.handle(_dispatched("bd-e.1", "w1"))

    renderer.handle(_settled("bd-e.1", "success", "closed"))

    lines = renderer.frame().splitlines()
    assert "  v  bd-e.1  add the greet command  success" in lines
    # The row is gone from the in-flight section: nothing is left below it.
    assert lines[-1] == "  v  bd-e.1  add the greet command  success"


def test_a_failed_turn_is_marked_differently_from_a_successful_one() -> None:
    renderer = LiveRenderer(clock=_Clock())
    renderer.handle(_started("bd-e.1", "add the greet command"))

    renderer.handle(_settled("bd-e.1", "blocked", "waiting on a human"))

    line = renderer.frame().splitlines()[-1]
    assert line.startswith("  x  bd-e.1")


def test_count_one_is_the_same_table_with_one_row() -> None:
    renderer = LiveRenderer(scope="bd-e.1", max_iterations=1, clock=_Clock())

    renderer.handle(_started("bd-e.1", "add the greet command"))
    renderer.handle(_dispatched("bd-e.1", "w1"))

    lines = renderer.frame().splitlines()
    # header, blank, one row — nothing else, and no rule since nothing settled yet.
    assert len(lines) == 3
    assert lines[0].startswith("scope   bd-e.1")


def test_several_lanes_are_separated_from_settled_history_by_a_rule() -> None:
    renderer = LiveRenderer(scope="3 unfinished under bd-e", max_iterations=50, clock=_Clock())
    renderer.handle(_started("bd-e.1", "add the greet command"))
    renderer.handle(_settled("bd-e.1", "success", "closed"))
    renderer.handle(_started("bd-e.3", "document the flag"))
    renderer.handle(_dispatched("bd-e.3", "w1"))
    renderer.handle(_started("bd-e.5", "add the smoke test"))
    renderer.handle(_dispatched("bd-e.5", "w2"))

    lines = renderer.frame().splitlines()
    settled_line = next(line for line in lines if "bd-e.1" in line)
    rule = next(line for line in lines if set(line) == {"-"})
    active_lines = [line for line in lines if "bd-e.3" in line or "bd-e.5" in line]

    assert lines.index(settled_line) < lines.index(rule) < lines.index(active_lines[0])
    assert len(active_lines) == 2


def test_a_landed_merge_is_named_in_the_settled_row() -> None:
    renderer = LiveRenderer(clock=_Clock())
    renderer.handle(_started("bd-e.1", "add the greet command"))
    renderer.handle(_dispatched("bd-e.1", "milhouse/bd-e.1"))
    renderer.handle(
        Event(
            "merged",
            about("bd-e.1", "merging milhouse/bd-e.1 into milhouse/bd-e in /worktrees/bd-e"),
            issue_id="bd-e.1",
            lane="milhouse/bd-e.1",
        )
    )
    renderer.handle(
        Event(
            "merged",
            about("bd-e.1", arrow("milhouse/bd-e fast-forwarded to abc123456789")),
            issue_id="bd-e.1",
            lane="milhouse/bd-e.1",
        )
    )

    renderer.handle(_settled("bd-e.1", "success", "closed"))

    line = renderer.frame().splitlines()[-1]
    assert line.endswith("merged")


def test_a_merge_conflict_is_named_unmerged_rather_than_merged() -> None:
    renderer = LiveRenderer(clock=_Clock())
    renderer.handle(_started("bd-e.1", "add the greet command"))
    renderer.handle(_dispatched("bd-e.1", "milhouse/bd-e.1"))
    renderer.handle(
        Event(
            "merged",
            about(
                "bd-e.1",
                arrow(
                    "milhouse/bd-e.1 conflicts with milhouse/bd-e in 1 file(s): a.py. "
                    "Both branches are intact; land milhouse/bd-e.1 by hand."
                ),
            ),
            issue_id="bd-e.1",
            lane="milhouse/bd-e.1",
        )
    )

    renderer.handle(_settled("bd-e.1", "success", "closed"))

    line = renderer.frame().splitlines()[-1]
    assert line.endswith("unmerged")


def test_the_header_counts_turns_against_the_ceiling() -> None:
    renderer = LiveRenderer(max_iterations=50, clock=_Clock())
    renderer.handle(_started("bd-e.1", "add the greet command"))
    renderer.handle(_settled("bd-e.1", "success", "closed"))

    renderer.handle(_started("bd-e.2", "wire up the config loader"))

    assert "turn 2/50" in renderer.frame().splitlines()[0]


def test_the_header_shows_the_runs_elapsed_time() -> None:
    clock = _Clock()
    renderer = LiveRenderer(clock=clock)
    clock.advance(732)

    assert "12m12s" in renderer.frame()


def test_settled_merged_halted_and_note_scroll_as_milestones() -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, width=100)
    renderer = LiveRenderer(console=console, clock=_Clock())
    renderer.handle(_started("bd-e.1", "add the greet command"))

    renderer.handle(_settled("bd-e.1", "success", "closed"))
    renderer.handle(Event("halted", "stopping: the run hit its ceiling of 50 iteration(s)"))
    renderer.handle(Event("note", "lane w1 is left open (/worktrees/bd-e.1)"))

    printed = buffer.getvalue()
    assert "success: closed" in printed
    assert "stopping: the run hit its ceiling" in printed
    assert "lane w1 is left open" in printed


def test_started_dispatched_and_heartbeat_print_no_milestone() -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, width=100)
    renderer = LiveRenderer(console=console, clock=_Clock())

    renderer.handle(_started("bd-e.1", "add the greet command"))
    renderer.handle(_dispatched("bd-e.1", "w1"))
    renderer.handle(_heartbeat("bd-e.1", "w1", state="working", elapsed_ms=1000))

    assert buffer.getvalue() == ""


def test_a_live_renderer_redraws_in_place_rather_than_appending() -> None:
    """Started against a console forced to believe it is a terminal, without one.

    Each ``handle()`` call redraws the whole table, so a renderer that merely
    appended would print every frame's rows again, over and over. A renderer
    that redraws in place erases the previous frame first — ``rich``'s own
    signature for that is the cursor-up/erase-line pair (``ESC[<n>A`` /
    ``ESC[2K``) between one frame's text and the next.
    """
    buffer = io.StringIO()
    console = _forced_terminal(buffer)
    renderer = LiveRenderer(console=console, scope="bd-e", max_iterations=50, clock=_Clock())

    with renderer:
        renderer.handle(_started("bd-e.1", "add the greet command"))
        renderer.handle(_dispatched("bd-e.1", "w1"))
        renderer.handle(_heartbeat("bd-e.1", "w1", state="working", elapsed_ms=1000))
        renderer.handle(_settled("bd-e.1", "success", "closed"))

    output = buffer.getvalue()
    assert "\x1b[2K" in output  # erase a line of the previous frame
    assert re.search(r"\x1b\[\d*A", output)  # move the cursor back up to redraw over it
    # The milestone still scrolls normally: it appears once, undisturbed.
    assert _plain(output).count("success: closed") == 1


def test_a_live_renderer_against_a_console_that_is_not_a_terminal_writes_no_redraw_codes() -> None:
    buffer = io.StringIO()
    console = Console(file=buffer, width=100)  # no force_terminal: is_terminal is False
    renderer = LiveRenderer(console=console, clock=_Clock())

    with renderer:
        renderer.handle(_started("bd-e.1", "add the greet command"))
        renderer.handle(_dispatched("bd-e.1", "w1"))
        renderer.handle(_settled("bd-e.1", "success", "closed"))

    assert "\x1b[" not in buffer.getvalue()


def test_ctrl_c_still_leaves_a_readable_terminal() -> None:
    """Whatever interrupts a run still runs `__exit__`, restoring the cursor."""
    buffer = io.StringIO()
    console = _forced_terminal(buffer)
    renderer = LiveRenderer(console=console, clock=_Clock())

    with pytest.raises(KeyboardInterrupt), renderer:
        renderer.handle(_started("bd-e.1", "add the greet command"))
        raise KeyboardInterrupt

    output = buffer.getvalue()
    assert "\x1b[?25l" in output  # cursor hidden while live
    assert "\x1b[?25h" in output  # and shown again on the way out
