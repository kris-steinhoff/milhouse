"""Tests for the loop: how many steps it takes, and what makes it stop.

The loop is thin by design, so this file is short. What an iteration achieved is
`test_outcome.py`, what happens next is `test_policy.py`, and one iteration end
to end is `test_step.py`.
"""

from __future__ import annotations

import pytest

from milhouse.config import Config
from milhouse.errors import UserAbortError
from milhouse.loop import RalphLoop
from milhouse.models import Issue, TaskDefinition
from milhouse.runner import TurnResult

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
    """A tracker with an epic and two open issues."""
    tracker = FakeTracker(epic=Issue(id="bd-e", title="Add a hello command", status="open"))
    tracker.issues = [
        Issue(id="bd-e.1", title="Add the subcommand", status="open", parent="bd-e"),
        Issue(id="bd-e.2", title="Document it", status="open", parent="bd-e"),
    ]
    return tracker


def test_a_run_works_every_issue_and_reports_completion(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, _ = build(config, task, tracker=decomposed, script=["close", "close"])

    result = RalphLoop(session).run()

    assert result.completed
    assert result.count == 2
    assert [item.outcome for item in result.iterations] == ["success", "success"]
    assert all(issue.is_closed for issue in decomposed.issues)


def test_the_run_stops_at_the_first_iteration_that_does_not_succeed(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Supervised means handing back to a person, not retrying unattended."""
    session, runner = build(config, task, tracker=decomposed, script=["stall", "close"])

    result = RalphLoop(session).run()

    assert not result.completed
    assert result.count == 1
    assert "bd-e.1 did not finish" in result.reason
    # The second issue was never claimed: the run stopped before it.
    assert decomposed.issues[1].status == "open"
    assert len(runner.turns) == 1


def test_the_iteration_budget_stops_the_run(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    config.loop.max_iterations = 1
    session, _ = build(config, task, tracker=decomposed, script=["close", "close"])

    result = RalphLoop(session).run()

    assert not result.completed
    assert result.count == 1
    assert "1-iteration budget" in result.reason


def test_the_budget_is_per_invocation_not_per_task(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """A lifetime cap would make a resumed run do nothing and exit non-zero."""
    config.loop.max_iterations = 1
    first, _ = build(config, task, tracker=decomposed, script=["close"])
    RalphLoop(first).run()

    second, _ = build(config, task, tracker=decomposed, script=["close"])
    result = RalphLoop(second).run()

    assert result.count == 1
    assert result.iterations[0].number == 2
    assert all(issue.is_closed for issue in decomposed.issues)


def test_a_blocked_agent_stops_the_run_and_says_where_to_look(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, _ = build(config, task, tracker=decomposed, script=["block"])

    result = RalphLoop(session).run()

    assert not result.completed
    assert "attach" in result.reason
    assert decomposed.released == ["bd-e.1"]


def test_an_epic_with_nothing_ready_and_nothing_open_is_finished(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    session, _ = build(config, task, tracker=decomposed, script=[])

    result = RalphLoop(session).run()

    assert result.completed
    assert result.count == 0
    assert result.reason == "no issues are ready; the epic is finished"


def test_an_interrupt_reverts_the_claim_and_leaves_the_workspace(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, runner = build(config, task, tracker=decomposed, script=[])

    def interrupt(prompt: str, *, iteration: int) -> TurnResult:
        raise UserAbortError("interrupted")

    runner.run_turn = interrupt  # ty: ignore[invalid-assignment]

    with pytest.raises(UserAbortError):
        RalphLoop(session).run()

    assert decomposed.released == ["bd-e.1"]
    assert not session.store.lock.path.exists()


# -- decomposition -------------------------------------------------------------


def test_decomposition_runs_when_there_is_no_epic(config: Config, task: TaskDefinition) -> None:
    tracker = FakeTracker()
    session, runner = build(config, task, tracker=tracker, script=[])

    def propose_then_close(prompt: str, *, iteration: int) -> TurnResult:
        runner.turns.append(prompt)
        if iteration == 0:
            session.store.run_dir.mkdir(parents=True, exist_ok=True)
            (session.store.run_dir / "plan.json").write_text(
                '{"issues": [{"key": "a", "title": "Add it"}]}', encoding="utf-8"
            )
            return TurnResult(agent_state="done")
        for issue in tracker.issues:
            if issue.status == "in_progress":
                issue.status = "closed"
        return TurnResult(agent_state="done")

    runner.run_turn = propose_then_close  # ty: ignore[invalid-assignment]

    result = RalphLoop(session).run()

    assert tracker.epic is not None
    assert [issue.title for issue in tracker.issues] == ["Add it"]
    assert result.completed
    assert "Do not run `bd`" in runner.turns[0]


def test_declining_the_decomposition_creates_nothing(config: Config, task: TaskDefinition) -> None:
    tracker = FakeTracker()
    session, runner = build(config, task, tracker=tracker, script=[])

    def propose(prompt: str, *, iteration: int) -> TurnResult:
        session.store.run_dir.mkdir(parents=True, exist_ok=True)
        (session.store.run_dir / "plan.json").write_text(
            '{"issues": [{"key": "a", "title": "Add it"}]}', encoding="utf-8"
        )
        return TurnResult(agent_state="done")

    runner.run_turn = propose  # ty: ignore[invalid-assignment]

    with pytest.raises(UserAbortError):
        RalphLoop(session).run(confirm=lambda plan: False)

    assert tracker.epic is None
