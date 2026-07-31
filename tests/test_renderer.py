"""Tests for the event/renderer seam (ADR 0026).

`Event` carries its structured fields separately from `text`, which is the
whole point of the seam: a renderer that wants more than a line can read
`kind`, `issue_id`, and `lane` without parsing a string back apart.
"""

from __future__ import annotations

import pytest

from milhouse.renderer import Event, PlainRenderer, about, arrow


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
