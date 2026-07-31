"""Tests for the event/renderer seam (ADR 0026).

`Event` carries its structured fields separately from `text`, which is the
whole point of the seam: a renderer that wants more than a line can read
`kind`, `issue_id`, and `lane` without parsing a string back apart.
"""

from __future__ import annotations

import pytest

from milhouse.renderer import PLAIN_KEEPALIVE_MS, Event, PlainRenderer, about, arrow


def heartbeat(issue_id: str, state: str, elapsed_ms: int) -> Event:
    return Event(
        "heartbeat",
        f"{issue_id} is still working",
        issue_id=issue_id,
        lane="w1",
        state=state,
        elapsed_ms=elapsed_ms,
    )


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
