"""Tests for the shared data types."""

from __future__ import annotations

from milhouse.models import Issue, Iteration, RunState


def test_issue_reports_closed_status() -> None:
    assert Issue(id="bd-1", title="t", status="closed").is_closed
    assert not Issue(id="bd-1", title="t", status="in_progress").is_closed


def test_iteration_detects_a_commit() -> None:
    committed = Iteration(number=1, issue_id="bd-1", outcome="partial", commits=["abc1234"])
    still = Iteration(number=1, issue_id="bd-1", outcome="stalled", head_before="a", head_after="a")

    assert committed.made_commit
    assert not still.made_commit


def test_an_older_run_state_still_loads() -> None:
    """The task fields went with the task (ADR 0018); a version 2 file is not an error."""
    state = RunState.model_validate_json(
        '{"version": 2, "task_id": "file:hello.md", "task_slug": "hello", '
        '"epic_id": "bd-e", "branch": "milhouse/hello"}'
    )

    assert state.branch == "milhouse/hello"
