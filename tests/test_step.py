"""Tests for one iteration: what it sends, what it records, what it settles."""

from __future__ import annotations

import pytest

from milhouse.config import Config
from milhouse.errors import MilhouseError
from milhouse.models import Issue, TaskDefinition
from milhouse.policy import Decision
from milhouse.step import nothing_ready, step

from .doubles import FakeTracker, build


@pytest.fixture
def task() -> TaskDefinition:
    return TaskDefinition(
        task_id="file:docs/tasks/hello.md",
        title="Add a hello command",
        body="It should greet.",
        kind="file",
        slug="hello",
    )


@pytest.fixture
def decomposed() -> FakeTracker:
    tracker = FakeTracker(epic=Issue(id="bd-e", title="Add a hello command", status="open"))
    tracker.issues = [
        Issue(id="bd-e.1", title="Add the subcommand", status="open", parent="bd-e"),
        Issue(id="bd-e.2", title="Document it", status="open", parent="bd-e"),
    ]
    return tracker


def test_a_step_claims_works_and_records_one_issue(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, _ = build(config, task, tracker=decomposed, script=["close"])

    with session as opened:
        result = step(opened, decomposed.epic)  # ty: ignore[invalid-argument-type]

    assert result is not None
    assert result.iteration.issue_id == "bd-e.1"
    assert result.iteration.outcome == "success"
    assert [item.outcome for item in session.store.history()] == ["success"]
    assert decomposed.issues[0].is_closed
    assert decomposed.issues[1].status == "open"


def test_a_step_returns_nothing_when_no_issue_is_ready(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    session, runner = build(config, task, tracker=decomposed, script=[])

    with session as opened:
        assert step(opened, decomposed.epic) is None  # ty: ignore[invalid-argument-type]

    assert runner.turns == []


def test_an_unfinished_issue_is_reopened_so_it_can_be_claimed_again(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, _ = build(config, task, tracker=decomposed, script=["stall"])

    with session as opened:
        step(opened, decomposed.epic)  # ty: ignore[invalid-argument-type]

    assert decomposed.released == ["bd-e.1"]
    assert decomposed.issues[0].status == "open"


def test_a_tracker_failure_is_an_error_rather_than_a_work_outcome(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Reading the issue back can fail; that is not the agent stalling."""
    session, _ = build(config, task, tracker=decomposed, script=["commit"])

    def explode(issue_id: str) -> Issue:
        raise MilhouseError("dolt is having a moment")

    decomposed.get = explode  # ty: ignore[invalid-assignment]

    with session as opened:
        result = step(opened, decomposed.epic)  # ty: ignore[invalid-argument-type]

    assert result is not None
    assert result.iteration.outcome == "error"
    assert "dolt is having a moment" in result.iteration.detail


def test_the_policy_is_injectable(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Swapping what happens after an iteration is swapping a function."""
    session, _ = build(config, task, tracker=decomposed, script=["stall"])

    with session as opened:
        result = step(
            opened,
            decomposed.epic,  # ty: ignore[invalid-argument-type]
            policy=lambda iteration: Decision(issue="none", stop=False),
        )

    assert result is not None
    assert not result.decision.stop
    assert decomposed.released == []


# -- the prompt a step builds --------------------------------------------------


def test_the_prompt_carries_the_issue_and_the_branch(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, runner = build(config, task, tracker=decomposed, script=["close"])

    with session as opened:
        step(opened, decomposed.epic)  # ty: ignore[invalid-argument-type]

    assert "bd-e.1" in runner.turns[0]
    assert "milhouse/hello" in runner.turns[0]
    assert "attempt" not in runner.turns[0]


def test_a_second_attempt_is_told_what_the_first_one_did(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """The event log is the only memory a fresh context window gets."""
    decomposed.issues = decomposed.issues[:1]
    session, runner = build(config, task, tracker=decomposed, script=["stall"])
    with session as opened:
        step(opened, decomposed.epic)  # ty: ignore[invalid-argument-type]

    resumed, runner = build(config, task, tracker=decomposed, script=["close"])
    with resumed as opened:
        result = step(opened, decomposed.epic)  # ty: ignore[invalid-argument-type]

    assert result is not None
    assert result.iteration.number == 2
    assert "attempt 2" in runner.turns[0]
    assert "stalled" in runner.turns[0]


# -- reporting an empty queue --------------------------------------------------


def test_an_epic_with_every_issue_closed_is_finished(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    session, _ = build(config, task, tracker=decomposed, script=[])

    with session as opened:
        reason, completed = nothing_ready(opened, decomposed.epic)  # ty: ignore[invalid-argument-type]

    assert completed
    assert reason == "no issues are ready; the epic is finished"


def test_an_epic_that_is_merely_stuck_is_not_reported_as_finished(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """An empty ready queue means "finished" or "stuck", and they are opposites.

    A dogfood run hit a permission prompt on every issue and then reported "the
    epic is finished" and exited 0 with nothing done.
    """
    for issue in decomposed.issues:
        issue.status = "blocked"
    session, _ = build(config, task, tracker=decomposed, script=[])

    with session as opened:
        reason, completed = nothing_ready(opened, decomposed.epic)  # ty: ignore[invalid-argument-type]

    assert not completed
    assert "unfinished" in reason
    for issue in decomposed.issues:
        assert issue.id in reason
