"""Tests for the post-iteration policy.

`decide` is pure, so this is a table rather than a set of scenarios. That is the
whole reason the decision was pulled out of the loop.
"""

from __future__ import annotations

import pytest

from milhouse.models import Iteration, Outcome
from milhouse.policy import decide


def iteration(outcome: Outcome, *, detail: str = "because", dirty_after: bool = False) -> Iteration:
    """One classified iteration, with only the fields the policy reads."""
    return Iteration(
        number=4,
        issue_id="bd-e.1",
        outcome=outcome,
        detail=detail,
        dirty_after=dirty_after,
    )


def test_success_leaves_the_issue_closed_and_says_nothing() -> None:
    decision = decide(iteration("success"))

    assert decision.issue == "none"
    assert not decision.reason


@pytest.mark.parametrize(
    "outcome", ["blocked", "rejected", "partial", "stalled", "timeout", "error"]
)
def test_anything_but_success_reopens_the_issue(outcome: Outcome) -> None:
    """A claimed issue is in_progress, which `bd ready` excludes.

    Leaving it alone would mean it is never offered again and the epic reads as
    finished with the work undone.
    """
    decision = decide(iteration(outcome))

    assert decision.issue == "release"
    assert decision.reason


def test_a_blocked_agent_says_where_to_go_and_what_to_do() -> None:
    decision = decide(iteration("blocked"))

    assert "attach" in decision.reason
    assert decision.note is not None


def test_a_rejected_close_says_verification_failed() -> None:
    decision = decide(iteration("rejected", detail="pytest exited 1"))

    assert "verification failed" in decision.reason


def test_a_success_that_leaves_the_tree_dirty_says_so() -> None:
    """The next agent would inherit changes it did not make and cannot explain."""
    decision = decide(iteration("success", dirty_after=True))

    assert decision.issue == "none"
    assert "dirty" in decision.reason


def test_a_failure_that_leaves_the_tree_dirty_says_so() -> None:
    decision = decide(iteration("stalled", dirty_after=True))

    assert "uncommitted changes" in decision.reason


def test_a_failure_carries_what_happened_into_the_issue_note() -> None:
    """The note is the only memory the next fresh context window gets."""
    decision = decide(iteration("stalled", detail="nothing was committed"))

    assert decision.note is not None
    assert "nothing was committed" in decision.note
