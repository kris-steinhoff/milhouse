"""Tests for the loop: how many turns happen, and what stops them.

Two halves, matching the module. `should_halt` is pure, so it is a table. The
loop itself runs against the doubles, so a run of five iterations starts no
agent and spawns no subprocess.
"""

from __future__ import annotations

import pytest

from milhouse.config import Config
from milhouse.models import Issue, Iteration, Outcome
from milhouse.policy import Policy, unattended
from milhouse.run import Body, RunResult, run, should_halt
from milhouse.runner import TurnResult
from milhouse.session import Session
from milhouse.step import StepResult

from .doubles import FakeRepo, FakeRunner, FakeTracker, build

TARGET = Issue(id="bd-e", title="Add a hello command", status="open", issue_type="epic")


def iteration(outcome: Outcome, *, dirty_after: bool = False) -> Iteration:
    return Iteration(
        number=1,
        issue_id="bd-e.1",
        outcome=outcome,
        detail="because",
        dirty_after=dirty_after,
    )


@pytest.fixture
def decomposed() -> FakeTracker:
    """An epic with two open issues under it."""
    tracker = FakeTracker(epic=TARGET)
    tracker.issues = [
        Issue(id="bd-e.1", title="Add the subcommand", status="open", parent="bd-e"),
        Issue(id="bd-e.2", title="Document it", status="open", parent="bd-e"),
    ]
    return tracker


def go(
    session: Session,
    *,
    max_iterations: int = 50,
    max_attempts: int = 3,
    body: Body | None = None,
) -> RunResult:
    """Run with the unattended policy, which is what the CLI passes."""
    kwargs = {"body": body} if body is not None else {}
    return run(
        session,
        TARGET,
        policy=unattended(max_attempts=max_attempts),
        max_iterations=max_iterations,
        **kwargs,
    )


# -- the halt table ------------------------------------------------------------


def test_a_blocked_agent_stops_the_run() -> None:
    """Nobody is there to approve, and the next turn meets the same prompt."""
    halt = should_halt(iteration("blocked"), used=1, max_iterations=50)

    assert halt is not None
    assert halt.reason == "blocked"
    assert not halt.finished


def test_a_milhouse_side_failure_stops_the_run() -> None:
    halt = should_halt(iteration("error"), used=1, max_iterations=50)

    assert halt is not None
    assert halt.reason == "error"


def test_a_closed_issue_that_left_a_dirty_tree_stops_the_run() -> None:
    """The next iteration shares this lane and would inherit the leftovers."""
    halt = should_halt(iteration("success", dirty_after=True), used=1, max_iterations=50)

    assert halt is not None
    assert halt.reason == "dirty"


def test_a_failed_turn_that_left_a_dirty_tree_does_not() -> None:
    """It is going to be retried anyway, and one untidy agent should not end a run."""
    assert should_halt(iteration("stalled", dirty_after=True), used=1, max_iterations=50) is None


@pytest.mark.parametrize("outcome", ["success", "rejected", "partial", "stalled", "timeout"])
def test_an_ordinary_turn_keeps_going(outcome: Outcome) -> None:
    assert should_halt(iteration(outcome), used=1, max_iterations=50) is None


def test_the_ceiling_stops_the_run() -> None:
    halt = should_halt(iteration("stalled"), used=7, max_iterations=7)

    assert halt is not None
    assert halt.reason == "ceiling"
    assert "7" in halt.detail


def test_a_blocked_agent_outranks_the_ceiling() -> None:
    """The ceiling is not why the run is over, and the report should say so."""
    halt = should_halt(iteration("blocked"), used=7, max_iterations=7)

    assert halt is not None
    assert halt.reason == "blocked"


# -- the loop ------------------------------------------------------------------


def test_a_run_works_the_queue_until_the_target_is_finished(
    config: Config, decomposed: FakeTracker
) -> None:
    session, runner = build(config, tracker=decomposed, script=["close", "close"])

    with session as opened:
        result = go(opened)

    assert result.finished
    assert result.halt.reason == "finished"
    assert [item.issue_id for item in result.iterations] == ["bd-e.1", "bd-e.2"]
    assert len(runner.turns) == 2
    assert all(issue.is_closed for issue in decomposed.issues)


def test_a_run_that_finds_nothing_ready_stops_without_a_turn(
    config: Config, decomposed: FakeTracker
) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    session, runner = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened)

    assert result.finished
    assert result.iterations == []
    assert runner.turns == []


def test_a_run_retries_an_issue_before_giving_up_on_it(
    config: Config, decomposed: FakeTracker
) -> None:
    """A fresh agent plus the note from the last attempt is the whole retry."""
    decomposed.issues = decomposed.issues[:1]
    session, _ = build(config, tracker=decomposed, script=["stall", "stall", "close"])

    with session as opened:
        result = go(opened)

    assert result.finished
    assert [item.attempt for item in result.iterations] == [1, 2, 3]
    assert result.deferred == []


def test_an_issue_that_uses_up_its_attempts_is_deferred_and_the_run_goes_on(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=["stall", "stall", "stall", "close"])

    with session as opened:
        result = go(opened)

    # Three attempts at the first issue, then the second one, which closes.
    assert [item.issue_id for item in result.iterations] == ["bd-e.1"] * 3 + ["bd-e.2"]
    assert [issue_id for issue_id, _ in result.deferred] == ["bd-e.1"]
    assert decomposed.deferred[0][0] == "bd-e.1"


def test_a_run_that_deferred_something_did_not_finish(
    config: Config, decomposed: FakeTracker
) -> None:
    """A deferred issue is still unfinished, so exiting zero would be a lie."""
    decomposed.issues = decomposed.issues[:1]
    session, _ = build(config, tracker=decomposed, script=["stall", "stall", "stall"])

    with session as opened:
        result = go(opened)

    assert not result.finished
    assert result.halt.reason == "deadlocked"
    assert "deferred 1 of them" in result.halt.detail


def test_a_run_stops_at_its_ceiling(config: Config, decomposed: FakeTracker) -> None:
    session, runner = build(config, tracker=decomposed, script=["stall"] * 10)

    with session as opened:
        result = go(opened, max_iterations=2)

    assert not result.finished
    assert result.halt.reason == "ceiling"
    assert len(result.iterations) == 2
    assert len(runner.turns) == 2


def test_a_run_stops_when_an_agent_blocks(config: Config, decomposed: FakeTracker) -> None:
    session, _ = build(config, tracker=decomposed, script=["block", "close"])

    with session as opened:
        result = go(opened)

    assert not result.finished
    assert result.halt.reason == "blocked"
    assert len(result.iterations) == 1
    # The issue is handed back open, so a person who unblocks it can re-run.
    assert decomposed.issues[0].status == "open"


def test_a_run_stops_when_a_closed_issue_leaves_the_tree_dirty(
    config: Config, decomposed: FakeTracker
) -> None:
    repo = FakeRepo()
    session, runner = build(config, tracker=decomposed, script=["close"], repo=repo)

    def close_then_leave_a_mess(
        prompt: str, *, iteration: int, issue_id: str | None = None
    ) -> TurnResult:
        result = FakeRunner.run_turn(runner, prompt, iteration=iteration, issue_id=issue_id)
        repo.dirty = True
        return result

    runner.run_turn = close_then_leave_a_mess  # ty: ignore[invalid-assignment]
    runner.script = ["close", "close"]

    with session as opened:
        result = go(opened)

    assert not result.finished
    assert result.halt.reason == "dirty"
    assert len(result.iterations) == 1


def test_a_run_stops_on_a_queue_that_is_merely_stuck(
    config: Config, decomposed: FakeTracker
) -> None:
    """Not everything closed.

    Reporting this as finished is how a run exits zero having done nothing.
    """
    decomposed.issues[0].status = "blocked"
    decomposed.issues[1].status = "blocked"
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened)

    assert not result.finished
    assert result.halt.reason == "deadlocked"
    assert "bd-e.1" in result.halt.detail


# -- what the result carries ---------------------------------------------------


def test_the_result_reports_the_turns_that_closed_something(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=["stall", "close", "close"])

    with session as opened:
        result = go(opened)

    assert [item.issue_id for item in result.closed()] == ["bd-e.1", "bd-e.2"]
    assert result.target is TARGET
    assert result.elapsed >= 0


def test_the_loop_body_is_swappable(config: Config, decomposed: FakeTracker) -> None:
    """Which is what keeps a later `--count N` from touching this module."""
    session, runner = build(config, tracker=decomposed, script=[])
    calls: list[Policy] = []

    def nothing(session: Session, policy: Policy) -> StepResult | None:
        calls.append(policy)
        return None

    with session as opened:
        result = go(opened, body=nothing)

    assert len(calls) == 1
    assert runner.turns == []
    assert result.halt.reason == "deadlocked"
