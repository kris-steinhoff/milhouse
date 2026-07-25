"""Tests for the shared data types, mostly the run-state bookkeeping."""

from __future__ import annotations

from pathlib import Path

import pytest

from milhouse.models import Issue, Iteration, RunState, slugify


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello", "hello"),
        ("Add a Hello Command", "add-a-hello-command"),
        ("docs/tasks/hello.md", "docs-tasks-hello-md"),
        ("  --weird__name--  ", "weird-name"),
        ("!!!", "task"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_issue_reports_closed_status() -> None:
    assert Issue(id="bd-1", title="t", status="closed").is_closed
    assert not Issue(id="bd-1", title="t", status="in_progress").is_closed


def test_iteration_detects_a_commit() -> None:
    moved = Iteration(number=1, issue_id="bd-1", outcome="partial", head_before="a", head_after="b")
    still = Iteration(number=1, issue_id="bd-1", outcome="stalled", head_before="a", head_after="a")

    assert moved.made_commit
    assert not still.made_commit


def test_failed_outcomes_count_against_the_attempt_cap() -> None:
    state = RunState(task_id="file:t.md", task_slug="t")

    for outcome in ("stalled", "partial", "timeout", "error"):
        state.record(Iteration(number=1, issue_id="bd-1", outcome=outcome))  # type: ignore[arg-type]

    assert state.attempts_for("bd-1") == 4


def test_success_and_blocked_do_not_count_against_the_attempt_cap() -> None:
    state = RunState(task_id="file:t.md", task_slug="t")

    state.record(Iteration(number=1, issue_id="bd-1", outcome="blocked"))
    state.record(Iteration(number=2, issue_id="bd-1", outcome="success"))

    assert state.attempts_for("bd-1") == 0
    assert len(state.iterations) == 2


def test_state_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "hello" / "state.json"
    state = RunState(task_id="file:docs/tasks/hello.md", task_slug="hello", epic_id="bd-9")
    state.record(Iteration(number=1, issue_id="bd-9.1", outcome="success", detail="closed it"))

    state.save(path)
    loaded = RunState.load(path)

    assert loaded is not None
    assert loaded.epic_id == "bd-9"
    assert loaded.iterations[0].detail == "closed it"
    assert not path.with_suffix(".json.tmp").exists()


def test_loading_a_missing_state_file_returns_none(tmp_path: Path) -> None:
    assert RunState.load(tmp_path / "state.json") is None
