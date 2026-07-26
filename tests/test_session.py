"""Tests for the session: the lock, the branch, the workspace, and the state.

Nothing here is about policy. A session claims and releases when it is told to,
and knows how to pick a task back up after a crash.
"""

from __future__ import annotations

import pytest

from milhouse.config import Config
from milhouse.errors import MilhouseError, RunLockedError
from milhouse.models import Issue, RunState, TaskDefinition
from milhouse.state import RunStore

from .doubles import FakeRepo, FakeTracker, build


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


# -- the lock ------------------------------------------------------------------


def test_a_second_session_on_one_task_is_refused(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Both would drive the same pane and reconcile each other's claim."""
    first, _ = build(config, task, tracker=decomposed, script=[])
    second, _ = build(config, task, tracker=decomposed, script=[])

    with first, pytest.raises(RunLockedError):
        second.__enter__()


def test_the_lock_is_dropped_even_when_opening_fails(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, _ = build(config, task, tracker=decomposed, script=[], repo=FakeRepo(dirty=True))

    with pytest.raises(MilhouseError, match="uncommitted changes"):
        session.__enter__()

    assert not session.store.lock.path.exists()


# -- reconciliation ------------------------------------------------------------


def test_a_stale_claim_is_reopened_when_the_session_opens(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """SIGKILL leaves a claim behind; re-running is the recovery mechanism."""
    session, _ = build(config, task, tracker=decomposed, script=[])
    decomposed.issues[0].status = "in_progress"
    session.store.save(RunState(task_id=task.task_id, task_slug=task.slug, claimed_issue="bd-e.1"))

    with build(config, task, tracker=decomposed, script=[])[0] as resumed:
        assert resumed.state.claimed_issue is None

    assert decomposed.released == ["bd-e.1"]


def test_an_in_flight_claim_is_released_on_the_way_out(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, _ = build(config, task, tracker=decomposed, script=[])

    with session as opened:
        opened.claim(decomposed.epic)  # ty: ignore[invalid-argument-type]
        assert opened.state.claimed_issue == "bd-e.1"

    assert decomposed.released == ["bd-e.1"]
    assert decomposed.issues[0].status == "open"


def test_a_state_file_from_a_different_task_is_refused(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Two task definitions sharing a filename would otherwise share a run dir."""
    RunStore(config.run_dir("hello")).save(
        RunState(task_id="file:elsewhere/hello.md", task_slug="hello")
    )

    with pytest.raises(MilhouseError, match="share a slug"):
        build(config, task, tracker=decomposed, script=[])


# -- branching -----------------------------------------------------------------


def test_the_run_gets_its_own_branch(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    repo = FakeRepo()
    session, _ = build(config, task, tracker=decomposed, script=[], repo=repo)

    with session:
        pass

    assert repo.branch == "milhouse/hello"


def test_a_dirty_working_tree_stops_the_session_before_anything_starts(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Losing someone's uncommitted work to a checkout is the worst failure."""
    session, runner = build(config, task, tracker=decomposed, script=[], repo=FakeRepo(dirty=True))

    with pytest.raises(MilhouseError, match="uncommitted changes"):
        session.__enter__()

    assert runner.turns == []


def test_the_current_branch_strategy_leaves_the_repo_alone(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    config.git.branch_strategy = "current"
    repo = FakeRepo(branch="some-worktree-branch", dirty=True)
    session, _ = build(config, task, tracker=decomposed, script=[], repo=repo)

    with session as opened:
        assert opened.state.branch == "some-worktree-branch"

    assert repo.branch == "some-worktree-branch"


# -- the workspace -------------------------------------------------------------


def test_the_workspace_and_pane_are_recorded(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    session, _ = build(config, task, tracker=decomposed, script=[])

    with session:
        pass

    saved = session.store.load()
    assert saved is not None
    assert saved.workspace_id == "wG"
    assert saved.pane_id == "wG:p1"
    assert saved.branch == "milhouse/hello"
