"""Tests for the concurrent loop body: dispatch up to N, poll, reap, hand back one.

Everything here drives the body with a scripted fake runner, so a run of five
turns at a width of two starts no agent and spawns no subprocess. The waiting is
injected too, so the suite never sleeps.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from milhouse import parallel
from milhouse.config import Config
from milhouse.herdr import Worktree
from milhouse.models import Issue
from milhouse.parallel import Parallel
from milhouse.policy import unattended
from milhouse.run import run
from milhouse.runner import TurnResult
from milhouse.session import Session
from milhouse.step import StepResult

from .doubles import FakeClient, FakeRunner, FakeTracker, build

TARGET = Issue(id="bd-e", title="Add a hello command", status="open", issue_type="epic")

POLICY = unattended(max_attempts=3)


@pytest.fixture
def decomposed() -> FakeTracker:
    """An epic with five independent open issues under it."""
    tracker = FakeTracker(epic=TARGET)
    tracker.issues = [
        Issue(id=f"bd-e.{n}", title=f"Do thing {n}", status="open", parent="bd-e")
        for n in range(1, 6)
    ]
    return tracker


def with_lanes(*issue_ids: str) -> FakeClient:
    """A herdr client with a lane already standing for each issue.

    The tests inject a runner, so ``Session.runner_for`` never opens one itself,
    but ``reap`` still asks herdr where a dispatched turn went.
    """
    client = FakeClient()
    for issue_id in issue_ids:
        workspace_id = f"wL{len(client.workspaces)}"
        client.workspaces[workspace_id] = issue_id
        client.checkouts.append(
            Worktree(
                path=Path("/worktrees") / issue_id,
                branch=f"milhouse/{issue_id}",
                workspace_id=workspace_id,
            )
        )
    return client


def body(count: int, *, max_iterations: int = 50, poll_ms: int = 250) -> Parallel:
    """A body that never really waits."""
    return Parallel(
        count=count,
        max_iterations=max_iterations,
        poll_ms=poll_ms,
        sleep=lambda seconds: None,
    )


def drain(session: Session, running: Parallel) -> list[StepResult]:
    """Call the body until it says there is nothing left."""
    collected = []
    while (result := running(session, POLICY)) is not None:
        collected.append(result)
    return collected


# -- what it starts ------------------------------------------------------------


def test_the_body_starts_no_more_agents_at_once_than_the_count(
    config: Config, decomposed: FakeTracker
) -> None:
    """Five issues at a width of two is never three agents deep."""
    ids = [issue.id for issue in decomposed.issues]
    session, runner = build(
        config, tracker=decomposed, script=["close"] * 5, client=with_lanes(*ids)
    )
    started = FakeRunner.start_turn
    at_once: list[int] = []

    def watch(prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        # The audit entry for this turn is written after it starts, so what is
        # already dispatched plus this one is how many agents are live.
        at_once.append(len(session.audit.dispatches()) + 1)
        return started(runner, prompt, iteration=iteration, issue_id=issue_id)

    runner.start_turn = watch  # ty: ignore[invalid-assignment]

    with session as opened:
        results = drain(opened, body(2))

    assert [result.iteration.issue_id for result in results] == ids
    assert max(at_once) == 2
    assert len(at_once) == 5


def test_the_body_never_dispatches_past_the_runs_ceiling(
    config: Config, decomposed: FakeTracker
) -> None:
    """A turn started is a turn spent, so a width of four cannot overshoot by three."""
    ids = [issue.id for issue in decomposed.issues]
    session, runner = build(
        config, tracker=decomposed, script=["close"] * 5, client=with_lanes(*ids)
    )

    with session as opened:
        results = drain(opened, body(4, max_iterations=2))

    assert len(runner.turns) == 2
    assert len(results) == 2
    assert [issue.is_closed for issue in decomposed.issues] == [True, True, False, False, False]


def test_a_width_of_one_dispatches_one_turn_at_a_time(
    config: Config, decomposed: FakeTracker
) -> None:
    """``--count 1`` is the serial run, which is what makes the concurrent path opt-in."""
    ids = [issue.id for issue in decomposed.issues]
    session, runner = build(
        config, tracker=decomposed, script=["close"] * 5, client=with_lanes(*ids)
    )
    running = body(1)
    widths: list[int] = []
    started = FakeRunner.start_turn

    def watch(prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        widths.append(len(session.audit.dispatches()) + 1)
        return started(runner, prompt, iteration=iteration, issue_id=issue_id)

    runner.start_turn = watch  # ty: ignore[invalid-assignment]

    with session as opened:
        results = drain(opened, running)

    assert widths == [1] * 5
    assert len(results) == 5


# -- what it hands back --------------------------------------------------------


def test_results_are_handed_back_one_per_call(config: Config, decomposed: FakeTracker) -> None:
    """Three turns settle together, and ``run()`` still sees one iteration at a time."""
    decomposed.issues = decomposed.issues[:3]
    ids = [issue.id for issue in decomposed.issues]
    session, runner = build(
        config, tracker=decomposed, script=["close"] * 3, client=with_lanes(*ids)
    )
    running = body(3)

    with session as opened:
        first = running(opened, POLICY)
        assert len(runner.turns) == 3
        second = running(opened, POLICY)
        third = running(opened, POLICY)
        assert running(opened, POLICY) is None

    assert [result.iteration.issue_id for result in (first, second, third) if result] == ids
    # The queue was dry by the second call, so nothing new was started for it.
    assert len(runner.turns) == 3


def test_nothing_in_flight_and_nothing_ready_is_the_no_work_signal(
    config: Config, decomposed: FakeTracker
) -> None:
    """The same ``None`` the serial body gives, which ``run()`` reads as an empty queue."""
    for issue in decomposed.issues:
        issue.status = "closed"
    session, runner = build(config, tracker=decomposed, script=[])

    with session as opened:
        assert body(3)(opened, POLICY) is None

    assert runner.turns == []


def test_a_turn_still_working_is_polled_rather_than_reported_as_no_work(
    config: Config, decomposed: FakeTracker
) -> None:
    """An agent that has not settled is not an empty queue, and waiting is at ``poll_ms``."""
    decomposed.issues = decomposed.issues[:1]
    session, runner = build(
        config, tracker=decomposed, script=["close"], client=with_lanes("bd-e.1")
    )
    runner.working = True
    waits: list[float] = []

    def wake(seconds: float) -> None:
        waits.append(seconds)
        runner.working = False

    running = Parallel(count=2, max_iterations=50, poll_ms=250, sleep=wake)

    with session as opened:
        result = running(opened, POLICY)

    assert result is not None
    assert result.iteration.outcome == "success"
    assert waits == [0.25]


def test_a_turn_that_can_never_be_reaped_is_given_up_on(
    config: Config, decomposed: FakeTracker
) -> None:
    """A lane herdr no longer has cannot settle, and polling for it would hang the run."""
    decomposed.issues = decomposed.issues[:1]
    session, runner = build(config, tracker=decomposed, script=["close"], client=FakeClient())
    runner.working = True
    config.agent.turn_timeout_ms = 0
    lines: list[str] = []
    session.report = lines.append

    with session as opened:
        assert body(2, poll_ms=0)(opened, POLICY) is None

    assert runner.turns  # the agent was started
    assert any("giving up on it" in line for line in lines)


# -- what it is not allowed to know --------------------------------------------


def test_the_body_imports_neither_outcome_nor_policy() -> None:
    """Reaping already applied the policy, and halting is ``run()``'s.

    A module here that needed either would mean judgement had leaked into the
    repetition layer.
    """
    tree = ast.parse(Path(parallel.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not imported & {"outcome", "policy", "milhouse.outcome", "milhouse.policy"}


# -- as a loop body ------------------------------------------------------------


def test_run_works_a_target_through_the_concurrent_body(
    config: Config, decomposed: FakeTracker
) -> None:
    """The loop is unchanged: a different body, not a different ``run()``."""
    ids = [issue.id for issue in decomposed.issues]
    session, _ = build(config, tracker=decomposed, script=["close"] * 5, client=with_lanes(*ids))

    with session as opened:
        result = run(opened, TARGET, policy=POLICY, max_iterations=50, body=body(2))

    assert result.finished
    assert result.halt.reason == "finished"
    assert [item.issue_id for item in result.iterations] == ids


def test_a_concurrent_run_stops_at_its_ceiling(config: Config, decomposed: FakeTracker) -> None:
    """``run()`` counts what it was handed, and the body never started more."""
    ids = [issue.id for issue in decomposed.issues]
    session, runner = build(
        config, tracker=decomposed, script=["close"] * 5, client=with_lanes(*ids)
    )

    with session as opened:
        result = run(
            opened, TARGET, policy=POLICY, max_iterations=3, body=body(2, max_iterations=3)
        )

    assert not result.finished
    assert result.halt.reason == "ceiling"
    assert len(result.iterations) == 3
    assert len(runner.turns) == 3
