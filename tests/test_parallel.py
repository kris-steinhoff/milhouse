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
from milhouse.gitrepo import Merge
from milhouse.herdr import Worktree
from milhouse.models import Issue
from milhouse.parallel import Parallel
from milhouse.policy import unattended
from milhouse.run import run
from milhouse.runner import TurnResult
from milhouse.session import Session
from milhouse.step import StepResult

from .doubles import FakeClient, FakeRepo, FakeRunner, FakeTracker, build

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


def test_a_turn_given_up_on_is_still_a_turn_the_run_spent(
    config: Config, decomposed: FakeTracker
) -> None:
    """Its agent was started and its issue was claimed, so it cost one of the ceiling."""
    session, runner = build(config, tracker=decomposed, script=["close"] * 5, client=FakeClient())
    runner.working = True
    config.agent.turn_timeout_ms = 0
    running = body(2, max_iterations=2, poll_ms=0)

    with session as opened:
        # Two dispatched, both lost, and then the budget is gone.
        assert running(opened, POLICY) is None
        assert running(opened, POLICY) is None

    assert len(runner.turns) == 2
    assert running.in_flight == ["bd-e.1", "bd-e.2"]


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
        result = run(opened, (TARGET,), policy=POLICY, max_iterations=50, body=body(2))

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
            opened, (TARGET,), policy=POLICY, max_iterations=3, body=body(2, max_iterations=3)
        )

    assert not result.finished
    assert result.halt.reason == "ceiling"
    assert len(result.iterations) == 3
    assert len(runner.turns) == 3


def test_a_run_wider_than_its_ceiling_starts_only_what_the_ceiling_allows(
    config: Config, decomposed: FakeTracker
) -> None:
    """A dispatched turn is spent, so a width of four does not overshoot a ceiling of one."""
    ids = [issue.id for issue in decomposed.issues]
    session, runner = build(
        config, tracker=decomposed, script=["close"] * 5, client=with_lanes(*ids)
    )

    with session as opened:
        result = run(
            opened, (TARGET,), policy=POLICY, max_iterations=1, body=body(4, max_iterations=1)
        )

    assert result.halt.reason == "ceiling"
    assert len(runner.turns) == 1
    assert len(result.iterations) == 1
    assert [issue.is_closed for issue in decomposed.issues] == [True, False, False, False, False]


def test_a_halt_drains_the_turns_in_flight_rather_than_abandoning_them(
    config: Config, decomposed: FakeTracker
) -> None:
    """They have claimed issues and live agents, and ``milhouse reap`` would not merge them."""
    decomposed.issues = decomposed.issues[:3]
    ids = [issue.id for issue in decomposed.issues]
    # Every turn closes its issue and leaves the tree dirty, so the first one
    # handed back halts the run and the other two are still going.
    session, runner = build(
        config,
        tracker=decomposed,
        script=["close"] * 3,
        repo=FakeRepo(dirty=True),
        client=with_lanes(*ids),
    )

    with session as opened:
        result = run(opened, (TARGET,), policy=POLICY, max_iterations=50, body=body(3))

    assert result.halt.reason == "dirty"
    # All three were started, so all three are finished and settled.
    assert [item.issue_id for item in result.iterations] == ids
    assert len(runner.turns) == 3
    assert all(issue.is_closed for issue in decomposed.issues)
    assert result.still_running == []


def test_the_drain_waits_for_a_turn_that_has_not_settled_yet(
    config: Config, decomposed: FakeTracker
) -> None:
    """The drain is patient, and bounded by the turn timeout ``reap`` already enforces."""
    decomposed.issues = decomposed.issues[:2]
    ids = [issue.id for issue in decomposed.issues]
    repo = FakeRepo(dirty=True)
    session, runner = build(
        config, tracker=decomposed, script=["close"] * 2, repo=repo, client=with_lanes(*ids)
    )
    finish = FakeRunner.finish_turn

    def one_at_a_time(iteration: int, *, issue_id: str | None = None) -> TurnResult:
        # Whatever is reaped next has not settled yet, so the second lane is
        # left in flight and the halt has something real to drain.
        runner.working = True
        return finish(runner, iteration, issue_id=issue_id)

    runner.finish_turn = one_at_a_time  # ty: ignore[invalid-assignment]
    waits: list[float] = []

    def wake(seconds: float) -> None:
        waits.append(seconds)
        runner.working = False

    running = Parallel(count=2, max_iterations=50, poll_ms=250, sleep=wake)

    with session as opened:
        result = run(opened, (TARGET,), policy=POLICY, max_iterations=50, body=running)

    assert result.halt.reason == "dirty"
    assert [item.issue_id for item in result.iterations] == ids
    assert all(issue.is_closed for issue in decomposed.issues)
    # The second lane was polled during the drain rather than dropped.
    assert waits == [0.25]
    assert running.in_flight == []


def test_a_drained_body_starts_nothing_more(config: Config, decomposed: FakeTracker) -> None:
    """A halt means stop starting work, and that is the body's promise rather than the loop's."""
    ids = [issue.id for issue in decomposed.issues]
    session, runner = build(
        config, tracker=decomposed, script=["close"] * 5, client=with_lanes(*ids)
    )
    running = body(2)

    with session as opened:
        assert running(opened, POLICY) is not None
        started = len(runner.turns)
        running.drain(opened, POLICY)

        assert running(opened, POLICY) is None

    assert len(runner.turns) == started


def with_worker_lanes(target: str, *issue_ids: str) -> FakeClient:
    """A herdr client holding a worker lane per issue, branched under ``target``.

    What `--count N` above one leaves standing: each lane labelled with its
    issue and on a branch of its own, which is what `reap` reads back and what a
    merge into the integration branch names (ADR 0024).
    """
    client = FakeClient()
    for number, issue_id in enumerate(issue_ids):
        branch = f"milhouse/{target}--{issue_id}"
        workspace_id = f"wW{number}"
        client.workspaces[workspace_id] = issue_id
        client.checkouts.append(
            Worktree(
                path=Path("/worktrees") / branch.replace("/", "-"),
                branch=branch,
                workspace_id=workspace_id,
            )
        )
    return client


def test_a_drain_merges_nothing_into_a_branch_that_has_already_refused_one(
    config: Config, decomposed: FakeTracker
) -> None:
    """The fifth watched run, as a regression: four turns, one landed, three not.

    ADR 0024's `--count 4` run over four issues that all touched the same lines.
    The first turn fast-forwarded, the second conflicted and halted the run, and
    the two still in flight were reaped, verified, merged, and conflicted in the
    same three files. Each is a closed issue on a live branch, and the run ended
    saying `4 issue(s) closed, 1 merged`.
    """
    decomposed.issues = decomposed.issues[:4]
    ids = [issue.id for issue in decomposed.issues]
    repo = FakeRepo()
    attempts: list[Merge] = [
        Merge(sha="f" * 40, fast_forwarded=True),
        Merge(sha=None, fast_forwarded=False, conflicts=("src/a.py", "src/b.py", "tests/t.py")),
    ]

    def merge(branch: str, *, message: str = "") -> Merge:
        repo.merged.append((repo.scope, branch))
        # A third call would be the bug: git is asked to combine a branch with
        # one it has just refused to combine with.
        return attempts.pop(0)

    repo.merge = merge  # ty: ignore[invalid-assignment]
    session, runner = build(
        config,
        tracker=decomposed,
        script=["close"] * 4,
        repo=repo,
        client=with_worker_lanes("bd-e", *ids),
        lane_key="bd-e",
        worker_lanes=True,
    )
    finish = FakeRunner.finish_turn

    def one_at_a_time(iteration: int, *, issue_id: str | None = None) -> TurnResult:
        # One turn settles per poll, so the halt fires with two still working.
        runner.working = True
        return finish(runner, iteration, issue_id=issue_id)

    runner.finish_turn = one_at_a_time  # ty: ignore[invalid-assignment]

    def wake(seconds: float) -> None:
        runner.working = False

    running = Parallel(count=4, max_iterations=50, poll_ms=250, sleep=wake)
    lines: list[str] = []
    session.report = lines.append

    with session as opened:
        result = run(opened, (TARGET,), policy=POLICY, max_iterations=50, body=running)

    assert result.halt.reason == "conflict"
    # Two turns were still working when it fired, and the drain says what it is
    # about to do with them rather than promising a merge it will not make.
    assert any("draining 2 turn(s) already in flight" in line for line in lines)
    assert any("nothing more is merged into milhouse/bd-e" in line for line in lines)
    # Every turn was finished and settled: that is what the drain is for.
    assert [item.issue_id for item in result.iterations] == ids
    assert all(issue.is_closed for issue in decomposed.issues)
    assert result.still_running == []
    # And exactly two merges were attempted, not four.
    assert [branch for _, branch in repo.merged] == [
        "milhouse/bd-e--bd-e.1",
        "milhouse/bd-e--bd-e.2",
    ]
    assert [item.issue_id for item in result.merged()] == ["bd-e.1"]
    assert [item.issue_id for item in result.unmerged()] == ["bd-e.2", "bd-e.3", "bd-e.4"]
    drained = [item.merge for item in result.unmerged()[1:]]
    assert all(item is not None and item.skipped for item in drained)
    assert all(item is not None and "bd-e--bd-e.2" in item.skipped for item in drained)


def test_a_run_names_a_turn_it_could_not_collect(config: Config, decomposed: FakeTracker) -> None:
    """A lane herdr has lost will not settle, and the report is where somebody finds out."""
    decomposed.issues = decomposed.issues[:1]
    session, runner = build(config, tracker=decomposed, script=["close"], client=FakeClient())
    runner.working = True
    config.agent.turn_timeout_ms = 0

    with session as opened:
        result = run(opened, (TARGET,), policy=POLICY, max_iterations=50, body=body(2, poll_ms=0))

    assert result.still_running == ["bd-e.1"]
    assert result.iterations == []


def test_a_run_whose_agent_side_will_not_take_prompts_halts_saying_so(
    config: Config, decomposed: FakeTracker
) -> None:
    """milhouse-amd.14: the turn was settled inside `dispatch` and never handed back.

    So the body reported no work, the queue looked stuck, and the run ended
    "nothing is ready but N issue(s) are unfinished" — which is what a deadlocked
    dependency graph looks like, not a herdr that will not take a prompt.
    """
    ids = [issue.id for issue in decomposed.issues]
    session, runner = build(
        config, tracker=decomposed, script=["unsubmitted"] * 5, client=with_lanes(*ids)
    )

    with session as opened:
        result = run(opened, (TARGET,), policy=POLICY, max_iterations=50, body=body(3))

    assert not result.finished
    assert result.halt.reason == "error"
    assert "could not prompt the agent" in result.halt.detail
    assert "nothing is ready" not in result.halt.detail
    # One turn was attempted, not one per issue and not one per attempt.
    assert [item.issue_id for item in result.iterations] == ["bd-e.1"]
    assert len(runner.turns) == 1
    assert result.still_running == []
