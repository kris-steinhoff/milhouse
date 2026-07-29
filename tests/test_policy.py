"""Tests for the post-iteration policies.

Both are pure, so this is a table rather than a set of scenarios. That is the
whole reason the decision was pulled out of the loop.

`decide` is what `milhouse step` uses. `unattended` is what `milhouse run` uses,
and it differs from `decide` in exactly one place: the last attempt.
"""

from __future__ import annotations

import pytest

from milhouse.models import Iteration, Outcome
from milhouse.policy import counts_as_attempt, decide, unattended

FAILURES: list[Outcome] = ["rejected", "partial", "stalled", "timeout", "error"]
"""Every outcome that burns an attempt. `success` and `blocked` do not."""


def iteration(
    outcome: Outcome,
    *,
    detail: str = "because",
    dirty_after: bool = False,
    attempt: int = 1,
) -> Iteration:
    """One classified iteration, with only the fields the policy reads."""
    return Iteration(
        number=4,
        issue_id="bd-e.1",
        outcome=outcome,
        detail=detail,
        dirty_after=dirty_after,
        attempt=attempt,
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


# -- what burns an attempt -----------------------------------------------------


@pytest.mark.parametrize("outcome", FAILURES)
def test_a_failing_outcome_burns_an_attempt(outcome: Outcome) -> None:
    assert counts_as_attempt(outcome)


@pytest.mark.parametrize("outcome", ["success", "blocked"])
def test_finishing_or_waiting_on_a_human_does_not(outcome: Outcome) -> None:
    """`success` did the work, and `blocked` is about to stop the run anyway."""
    assert not counts_as_attempt(outcome)


# -- the unattended policy -----------------------------------------------------


@pytest.mark.parametrize("outcome", ["success", "blocked", *FAILURES])
@pytest.mark.parametrize("attempt", [1, 2])
def test_below_the_cap_it_is_the_supervised_policy(outcome: Outcome, attempt: int) -> None:
    """One policy, one addition. Everything before the last attempt is `decide`."""
    turn = iteration(outcome, attempt=attempt)

    assert unattended(max_attempts=3)(turn) == decide(turn)


@pytest.mark.parametrize("outcome", FAILURES)
def test_the_last_attempt_defers_the_issue(outcome: Outcome) -> None:
    decision = unattended(max_attempts=3)(iteration(outcome, attempt=3))

    assert decision.issue == "defer"
    assert "3 attempt" in decision.reason
    assert outcome in decision.reason


@pytest.mark.parametrize("outcome", ["success", "blocked"])
def test_the_cap_does_not_apply_to_outcomes_that_are_not_attempts(outcome: Outcome) -> None:
    """An issue is not set aside for finishing, or for waiting on a person."""
    turn = iteration(outcome, attempt=9)

    assert unattended(max_attempts=3)(turn) == decide(turn)


def test_a_deferred_issue_is_told_why_and_how_to_come_back() -> None:
    """The note is what whoever picks the issue up finds waiting for them."""
    decision = unattended(max_attempts=2)(
        iteration("stalled", detail="nothing was committed", attempt=2)
    )

    assert decision.note is not None
    assert "nothing was committed" in decision.note
    assert "bd undefer bd-e.1" in decision.note


def test_the_cap_is_configurable() -> None:
    once = unattended(max_attempts=1)

    assert once(iteration("stalled", attempt=1)).issue == "defer"
    assert unattended(max_attempts=5)(iteration("stalled", attempt=1)).issue == "release"
