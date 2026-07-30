"""Tests for the loop: how many turns happen, and what stops them.

Three halves, matching the module. `should_halt` is pure, so it is a table. The
loop itself runs against the doubles, so a run of five iterations starts no
agent and spawns no subprocess. And a body with turns of its own is faked, so
what a halt does about them is testable without a second agent either.
"""

from __future__ import annotations

import pytest

from milhouse.config import Config
from milhouse.models import Issue, Iteration, MergeRecord, Outcome
from milhouse.policy import Decision, Policy, unattended
from milhouse.run import Body, RunResult, run, should_halt
from milhouse.runner import TurnResult
from milhouse.session import Session
from milhouse.step import StepResult

from .doubles import FakeRepo, FakeRunner, FakeTracker, build

TARGET = Issue(id="bd-e", title="Add a hello command", status="open", issue_type="epic")

WORKER = "milhouse/bd-e--bd-e.1"
INTEGRATION = "milhouse/bd-e"


def iteration(
    outcome: Outcome,
    *,
    dirty_after: bool = False,
    merge: MergeRecord | None = None,
    issue_id: str = "bd-e.1",
    integration_verified: bool | None = None,
) -> Iteration:
    return Iteration(
        number=1,
        issue_id=issue_id,
        outcome=outcome,
        detail="because",
        dirty_after=dirty_after,
        merge=merge,
        integration_verified=integration_verified,
    )


def landed() -> MergeRecord:
    """A worker branch that reached the integration branch."""
    return MergeRecord(source=WORKER, target=INTEGRATION, sha="a" * 40)


def conflicted() -> MergeRecord:
    """A worker branch git could not combine with the integration branch."""
    return MergeRecord(source=WORKER, target=INTEGRATION, conflicts=["src/a.py", "src/b.py"])


def refused() -> MergeRecord:
    """A merge git would not even attempt."""
    return MergeRecord(source=WORKER, target=INTEGRATION, error="the index is locked")


class FakeBody:
    """A loop body with turns of its own, which is what a concurrent one is.

    It hands back what it was scripted with, one call at a time, and keeps
    ``drains`` for whoever asks it to drain — the turns that were in flight when
    the run stopped. ``lost`` is what even a drain could not collect.
    """

    def __init__(
        self,
        *,
        hands_back: list[StepResult],
        drains: list[StepResult] | None = None,
        lost: list[str] | None = None,
    ) -> None:
        """Script what it hands back, what a drain would collect, and what is lost."""
        self._queue = list(hands_back)
        self._drains = list(drains or [])
        self._lost = list(lost or [])
        self.calls = 0
        self.drained = 0

    @property
    def in_flight(self) -> list[str]:
        return [result.iteration.issue_id for result in self._drains] + self._lost

    def drain(self, session: Session, policy: Policy) -> list[StepResult]:
        self.drained += 1
        collected, self._drains = self._drains, []
        return collected

    def __call__(self, session: Session, policy: Policy) -> StepResult | None:
        self.calls += 1
        return self._queue.pop(0) if self._queue else None


def settled(
    outcome: Outcome,
    *,
    issue_id: str = "bd-e.1",
    merge: MergeRecord | None = None,
    decision: Decision | None = None,
    integration_verified: bool | None = None,
) -> StepResult:
    """One finished turn, as a body hands it back."""
    return StepResult(
        iteration=iteration(
            outcome, issue_id=issue_id, merge=merge, integration_verified=integration_verified
        ),
        decision=decision or Decision(issue="release"),
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


def test_a_branch_that_conflicted_stops_the_run() -> None:
    """A halt and not a deferral: the work is done, closed, and only a person can land it."""
    halt = should_halt(iteration("success", merge=conflicted()), used=1, max_iterations=50)

    assert halt is not None
    assert halt.reason == "conflict"
    assert not halt.finished
    # Both branches and every path, because the recovery is entirely by hand.
    assert WORKER in halt.detail
    assert INTEGRATION in halt.detail
    assert "src/a.py" in halt.detail


def test_a_merge_git_refused_stops_the_run_for_the_same_reason() -> None:
    """`landed` is false for an error too, and it leaves exactly the same mess."""
    halt = should_halt(iteration("success", merge=refused()), used=1, max_iterations=50)

    assert halt is not None
    assert halt.reason == "conflict"
    # One reason, and a detail that says which of the two happened.
    assert "the index is locked" in halt.detail


def test_a_branch_that_landed_does_not_stop_the_run() -> None:
    assert should_halt(iteration("success", merge=landed()), used=1, max_iterations=50) is None


def test_a_conflict_outranks_the_ceiling() -> None:
    """Same argument as a blocked agent: the ceiling is not why the run is over."""
    halt = should_halt(iteration("success", merge=conflicted()), used=7, max_iterations=7)

    assert halt is not None
    assert halt.reason == "conflict"


def test_a_red_integration_branch_stops_the_run() -> None:
    """Two branches that were green apart are red together, and nothing is undone."""
    halt = should_halt(
        iteration("success", merge=landed(), integration_verified=False),
        used=1,
        max_iterations=50,
    )

    assert halt is not None
    assert halt.reason == "integration"
    assert not halt.finished
    assert INTEGRATION in halt.detail
    # The report has to say that the close and the merge both stand.
    assert "stays closed" in halt.detail


def test_a_green_integration_branch_does_not_stop_the_run() -> None:
    """The second gate run is only interesting when it fails."""
    halt = should_halt(
        iteration("success", merge=landed(), integration_verified=True), used=1, max_iterations=50
    )

    assert halt is None


def test_a_conflict_outranks_a_red_integration_branch() -> None:
    """A branch that never landed cannot also have been verified where it landed."""
    halt = should_halt(
        iteration("success", merge=conflicted(), integration_verified=False),
        used=1,
        max_iterations=50,
    )

    assert halt is not None
    assert halt.reason == "conflict"


def test_a_dirty_tree_outranks_a_conflict_on_the_same_turn() -> None:
    """The close claimed work that is not committed, so the branch is the smaller problem."""
    halt = should_halt(
        iteration("success", dirty_after=True, merge=conflicted()), used=1, max_iterations=50
    )

    assert halt is not None
    assert halt.reason == "dirty"


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


def test_a_serial_run_has_nothing_in_flight_and_nothing_to_merge(
    config: Config, decomposed: FakeTracker
) -> None:
    """`step` waits for its agent, so when it returns there is nothing left running."""
    session, _ = build(config, tracker=decomposed, script=["close", "close"])

    with session as opened:
        result = go(opened)

    assert result.still_running == []
    assert result.merged() == []
    assert result.unmerged() == []


def test_the_result_names_what_landed_and_what_did_not(
    config: Config, decomposed: FakeTracker
) -> None:
    """Closed and on the branch you are about to review stop being the same thing."""
    body = FakeBody(
        hands_back=[
            settled("success", issue_id="bd-e.1", merge=landed()),
            settled("success", issue_id="bd-e.2", merge=conflicted()),
        ]
    )
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened, body=body)

    assert result.halt.reason == "conflict"
    assert [item.issue_id for item in result.merged()] == ["bd-e.1"]
    assert [item.issue_id for item in result.unmerged()] == ["bd-e.2"]


def test_a_run_stops_when_a_merge_makes_the_integration_branch_red(
    config: Config, decomposed: FakeTracker
) -> None:
    """And the merge is still reported as landed, because it is: nothing was reverted."""
    body = FakeBody(
        hands_back=[
            settled("success", issue_id="bd-e.1", merge=landed(), integration_verified=False),
            settled("success", issue_id="bd-e.2", merge=landed()),
        ]
    )
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened, body=body)

    assert result.halt.reason == "integration"
    assert not result.finished
    assert body.calls == 1
    assert [item.issue_id for item in result.merged()] == ["bd-e.1"]
    assert result.unmerged() == []


# -- draining ------------------------------------------------------------------


def test_a_halt_finishes_the_turns_already_in_flight(
    config: Config, decomposed: FakeTracker
) -> None:
    """Abandoning them would leave claimed issues with live agents and unmerged branches."""
    body = FakeBody(
        hands_back=[settled("blocked")],
        drains=[
            settled("success", issue_id="bd-e.2", merge=landed()),
            settled("success", issue_id="bd-e.3", merge=landed()),
        ],
    )
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened, body=body)

    assert result.halt.reason == "blocked"
    assert body.drained == 1
    # The body was asked for work once, and never again after the halt.
    assert body.calls == 1
    assert [item.issue_id for item in result.iterations] == ["bd-e.1", "bd-e.2", "bd-e.3"]
    assert [item.issue_id for item in result.merged()] == ["bd-e.2", "bd-e.3"]
    assert result.still_running == []


def test_a_second_halt_reason_during_the_drain_does_not_change_the_outcome(
    config: Config, decomposed: FakeTracker
) -> None:
    """The first reason is why the run stopped, however the rest of the turns end."""
    body = FakeBody(
        hands_back=[settled("blocked")],
        drains=[settled("success", issue_id="bd-e.2", merge=conflicted()), settled("error")],
    )
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened, body=body)

    assert result.halt.reason == "blocked"
    assert len(result.iterations) == 3
    # And the conflict is still reported, because somebody has to land it.
    assert [item.issue_id for item in result.unmerged()] == ["bd-e.2"]


def test_an_issue_the_drain_gave_up_on_is_deferred_in_the_report(
    config: Config, decomposed: FakeTracker
) -> None:
    """A drained turn settles its issue like any other, and the report says so."""
    body = FakeBody(
        hands_back=[settled("blocked")],
        drains=[
            settled(
                "stalled",
                issue_id="bd-e.2",
                decision=Decision(issue="defer", reason="3 attempts"),
            )
        ],
    )
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened, body=body)

    assert result.deferred == [("bd-e.2", "3 attempts")]


def test_a_turn_the_drain_could_not_collect_is_named_as_still_running(
    config: Config, decomposed: FakeTracker
) -> None:
    """A report whose numbers look complete while an agent is still working is the worse lie."""
    body = FakeBody(hands_back=[settled("blocked")], lost=["bd-e.4", "bd-e.5"])
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened, body=body)

    assert result.still_running == ["bd-e.4", "bd-e.5"]
    assert len(result.iterations) == 1


def test_an_empty_queue_drains_too(config: Config, decomposed: FakeTracker) -> None:
    """Nothing ready is a halt like any other, and a body may still be holding turns."""
    body = FakeBody(hands_back=[], drains=[settled("success", issue_id="bd-e.2", merge=landed())])
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        result = go(opened, body=body)

    assert body.drained == 1
    assert [item.issue_id for item in result.iterations] == ["bd-e.2"]


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
