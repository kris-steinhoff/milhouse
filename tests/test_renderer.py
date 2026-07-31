"""Tests for the event/renderer seam (ADR 0026).

`Event` carries its structured fields separately from `text`, which is the
whole point of the seam: a renderer that wants more than a line can read
`kind`, `issue_id`, and `lane` without parsing a string back apart.
"""

from __future__ import annotations

import pytest

from milhouse.errors import ConfigError
from milhouse.renderer import Event, NullRenderer, PlainRenderer, about, arrow, select_renderer


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


def test_the_null_renderer_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = NullRenderer()

    renderer.handle(Event("note", "took over the lock on bd-e from a dead run"))

    assert capsys.readouterr().out == ""


class TestSelectRenderer:
    """`select_renderer`'s cases, from milhouse-lyq.5's acceptance criteria."""

    def test_a_tty_with_no_flags_or_env_gets_live(self) -> None:
        mode = select_renderer(isatty=True, verbose=False, quiet=False, environ={})

        assert mode == "live"

    def test_a_run_piped_to_a_file_gets_plain(self) -> None:
        mode = select_renderer(isatty=False, verbose=False, quiet=False, environ={})

        assert mode == "plain"

    def test_verbose_never_redraws_even_on_a_tty(self) -> None:
        mode = select_renderer(isatty=True, verbose=True, quiet=False, environ={})

        assert mode == "plain"

    def test_quiet_wins_on_a_tty(self) -> None:
        mode = select_renderer(isatty=True, verbose=False, quiet=True, environ={})

        assert mode == "quiet"

    def test_quiet_beats_verbose(self) -> None:
        mode = select_renderer(isatty=True, verbose=True, quiet=True, environ={})

        assert mode == "quiet"

    def test_no_color_forces_plain_on_a_tty(self) -> None:
        mode = select_renderer(isatty=True, verbose=False, quiet=False, environ={"NO_COLOR": "1"})

        assert mode == "plain"

    def test_no_color_counts_even_when_empty(self) -> None:
        mode = select_renderer(isatty=True, verbose=False, quiet=False, environ={"NO_COLOR": ""})

        assert mode == "plain"

    @pytest.mark.parametrize("value", ["live", "plain", "quiet"])
    def test_milhouse_output_overrides_the_tty_default(self, value: str) -> None:
        mode = select_renderer(
            isatty=value == "plain", verbose=False, quiet=False, environ={"MILHOUSE_OUTPUT": value}
        )

        assert mode == value

    def test_milhouse_output_beats_no_color(self) -> None:
        mode = select_renderer(
            isatty=True,
            verbose=False,
            quiet=False,
            environ={"MILHOUSE_OUTPUT": "live", "NO_COLOR": "1"},
        )

        assert mode == "live"

    def test_an_empty_milhouse_output_is_treated_as_unset(self) -> None:
        mode = select_renderer(
            isatty=False, verbose=False, quiet=False, environ={"MILHOUSE_OUTPUT": ""}
        )

        assert mode == "plain"

    def test_an_unrecognised_milhouse_output_is_a_config_error(self) -> None:
        with pytest.raises(ConfigError, match="MILHOUSE_OUTPUT"):
            select_renderer(
                isatty=True, verbose=False, quiet=False, environ={"MILHOUSE_OUTPUT": "json"}
            )

    def test_verbose_beats_an_env_asking_for_live(self) -> None:
        mode = select_renderer(
            isatty=True, verbose=True, quiet=False, environ={"MILHOUSE_OUTPUT": "live"}
        )

        assert mode == "plain"

    def test_quiet_beats_an_env_asking_for_live(self) -> None:
        mode = select_renderer(
            isatty=True, verbose=False, quiet=True, environ={"MILHOUSE_OUTPUT": "live"}
        )

        assert mode == "quiet"
