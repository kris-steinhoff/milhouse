"""Tests for the iteration decision table.

One test per row of the table in
[ADR 0004](../docs/decisions/0004-outcome-from-beads-and-git.md), plus the
precedence cases where two signals disagree.
"""

from __future__ import annotations

import pytest

from milhouse.models import Issue
from milhouse.outcome import classify


def issue(status: str = "in_progress") -> Issue:
    return Issue(id="bd-1", title="Add it", status=status)


def test_a_closed_issue_is_success() -> None:
    verdict = classify(issue_after=issue("closed"), commits=["abc1234"], agent_state="done")

    assert verdict.outcome == "success"


def test_a_blocked_agent_needs_a_human() -> None:
    verdict = classify(issue_after=issue(), commits=[], agent_state="blocked")

    assert verdict.outcome == "blocked"


def test_an_open_issue_with_a_commit_for_it_is_partial() -> None:
    verdict = classify(
        issue_after=issue(), commits=["abc1234"], attributed=True, agent_state="done"
    )

    assert verdict.outcome == "partial"
    assert "1 commit landed for it" in verdict.detail


def test_a_commit_that_does_not_name_the_issue_says_so() -> None:
    """HEAD moving is weak evidence: a hook or another terminal moves it too."""
    verdict = classify(
        issue_after=issue(), commits=["abc1234", "def5678"], attributed=False, agent_state="done"
    )

    assert verdict.outcome == "partial"
    assert "2 commits landed, none naming it" in verdict.detail


def test_an_open_issue_with_no_commit_is_stalled() -> None:
    verdict = classify(issue_after=issue(), commits=[], agent_state="done")

    assert verdict.outcome == "stalled"


def test_a_timed_out_turn_is_a_timeout() -> None:
    verdict = classify(
        issue_after=issue(), commits=["abc1234"], agent_state="working", timed_out=True
    )

    assert verdict.outcome == "timeout"


def test_a_milhouse_side_failure_is_an_error() -> None:
    verdict = classify(
        issue_after=issue(),
        commits=[],
        agent_state="unknown",
        error="could not start the agent",
    )

    assert verdict.outcome == "error"
    assert verdict.detail == "could not start the agent"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"agent_state": "blocked"},
        {"agent_state": "working", "timed_out": True},
        {"agent_state": "unknown", "error": "herdr fell over"},
    ],
)
def test_a_closed_issue_wins_over_every_other_signal(kwargs: dict) -> None:
    """If the work is done, it is done, whatever else the turn looked like."""
    verdict = classify(issue_after=issue("closed"), commits=[], **kwargs)

    assert verdict.outcome == "success"


def test_an_empty_repository_counts_as_stalled_not_partial() -> None:
    """No commits landed means no commit was made, not that one was."""
    verdict = classify(issue_after=issue(), commits=[], agent_state="done")

    assert verdict.outcome == "stalled"
