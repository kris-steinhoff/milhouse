"""Tests for one iteration: what it sends, what it records, what it settles."""

from __future__ import annotations

import pytest

from milhouse.config import Config
from milhouse.errors import MilhouseError
from milhouse.models import Issue
from milhouse.policy import Decision
from milhouse.runner import TurnResult
from milhouse.step import nothing_ready, step

from .doubles import FakeRepo, FakeRunner, FakeTracker, build
from .fakes import FakeProc, Reply


@pytest.fixture
def decomposed() -> FakeTracker:
    tracker = FakeTracker(
        epic=Issue(
            id="bd-e",
            title="Add a hello command",
            status="open",
            issue_type="epic",
            description="It should greet.",
        )
    )
    tracker.issues = [
        Issue(id="bd-e.1", title="Add the subcommand", status="open", parent="bd-e"),
        Issue(id="bd-e.2", title="Document it", status="open", parent="bd-e"),
    ]
    return tracker


def test_a_step_claims_works_and_records_one_issue(config: Config, decomposed: FakeTracker) -> None:
    session, runner = build(config, tracker=decomposed, script=["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.issue_id == "bd-e.1"
    assert result.iteration.outcome == "success"
    # The runner files this turn's artifacts under the issue it worked.
    assert runner.issue_ids == ["bd-e.1"]
    assert [item.outcome for item in session.audit.iterations()] == ["success"]
    assert decomposed.issues[0].is_closed
    assert decomposed.issues[1].status == "open"


def test_a_step_returns_nothing_when_no_issue_is_ready(
    config: Config, decomposed: FakeTracker
) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    session, runner = build(config, tracker=decomposed, script=[])

    with session as opened:
        assert step(opened) is None

    assert runner.turns == []


def test_an_unfinished_issue_is_reopened_so_it_can_be_claimed_again(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=["stall"])

    with session as opened:
        step(opened)

    assert decomposed.released == ["bd-e.1"]
    assert decomposed.issues[0].status == "open"


def test_a_tracker_failure_is_an_error_rather_than_a_work_outcome(
    config: Config, decomposed: FakeTracker
) -> None:
    """Reading the issue back can fail; that is not the agent stalling."""
    session, _ = build(config, tracker=decomposed, script=["commit"])
    original = decomposed.get

    def explode(issue_id: str) -> Issue:
        if issue_id == "bd-e.1":
            raise MilhouseError("dolt is having a moment")
        return original(issue_id)

    decomposed.get = explode  # ty: ignore[invalid-assignment]

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "error"
    assert "dolt is having a moment" in result.iteration.detail


def test_the_policy_is_injectable(config: Config, decomposed: FakeTracker) -> None:
    """Swapping what happens after an iteration is swapping a function."""
    session, _ = build(config, tracker=decomposed, script=["stall"])

    with session as opened:
        result = step(opened, policy=lambda iteration: Decision(issue="none"))

    assert result is not None
    assert result.decision.issue == "none"
    assert decomposed.released == []


# -- what git says -------------------------------------------------------------


def test_a_commit_naming_the_issue_is_recorded_as_evidence(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=["commit"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "partial"
    assert result.iteration.commits == ["sha1"]
    assert result.iteration.attributed


def test_a_commit_that_names_no_issue_is_movement_rather_than_progress(
    config: Config, decomposed: FakeTracker
) -> None:
    """A hook, or a human in another terminal, moves HEAD too."""
    session, _ = build(config, tracker=decomposed, script=["commit-unrelated"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.commits == ["sha1"]
    assert not result.iteration.attributed
    assert "none naming it" in result.iteration.detail


def test_git_is_read_where_the_turn_ran(config: Config, decomposed: FakeTracker) -> None:
    """Under lanes the runner works in a worktree, and that is what gets classified."""
    repo = FakeRepo()
    session, runner = build(config, tracker=decomposed, script=["commit"], repo=repo)
    runner.workdir = config.repo_root / ".lanes" / "bd-e.1"

    with session as opened:
        step(opened)

    assert runner.workdir in repo.scoped_to


def test_a_dirty_tree_after_a_turn_is_recorded_and_reported(
    config: Config, decomposed: FakeTracker
) -> None:
    """The next agent would inherit changes it did not make and cannot explain."""
    repo = FakeRepo()
    session, runner = build(config, tracker=decomposed, script=["close"], repo=repo)

    def close_then_leave_a_mess(
        prompt: str, *, iteration: int, issue_id: str | None = None
    ) -> TurnResult:
        result = FakeRunner.run_turn(runner, prompt, iteration=iteration, issue_id=issue_id)
        repo.dirty = True
        return result

    runner.run_turn = close_then_leave_a_mess  # ty: ignore[invalid-assignment]
    runner.script = ["close"]

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "success"
    assert result.iteration.dirty_after
    assert "dirty" in result.decision.reason


# -- verification --------------------------------------------------------------


def test_a_closed_issue_is_verified_before_it_counts(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    config.verify.command = ["make", "check"]
    fake_proc.expect("make check", Reply(stdout="ok"))
    session, _ = build(config, tracker=decomposed, script=["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "success"
    assert result.iteration.verified is True


def test_a_closed_issue_that_fails_verification_is_reopened_with_the_output(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """`bd close` is the agent grading its own exam; this is the second marker."""
    config.verify.command = ["make", "check"]
    fake_proc.expect("make check", Reply(stdout="FAILED tests/test_it.py", returncode=1))
    session, _ = build(config, tracker=decomposed, script=["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "rejected"
    assert result.iteration.verified is False
    assert decomposed.released == ["bd-e.1"]
    assert decomposed.issues[0].status == "open"
    note = decomposed.notes[0][1]
    assert "FAILED tests/test_it.py" in note


def test_an_unfinished_issue_is_not_verified(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """The suite would only confirm that unfinished work is unfinished."""
    config.verify.command = ["make", "check"]
    session, _ = build(config, tracker=decomposed, script=["stall"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "stalled"
    assert result.iteration.verified is None
    assert not fake_proc.ran("make")


# -- the prompt a step builds --------------------------------------------------


def test_the_prompt_carries_the_issue_and_the_branch(
    config: Config, decomposed: FakeTracker
) -> None:
    session, runner = build(config, tracker=decomposed, script=["close"])

    with session as opened:
        step(opened)

    assert "bd-e.1" in runner.turns[0]
    assert "main" in runner.turns[0]
    assert "attempt" not in runner.turns[0]


def test_the_background_is_the_parents_description(config: Config, decomposed: FakeTracker) -> None:
    """With no task definition, the epic is where the wider context lives."""
    session, runner = build(config, tracker=decomposed, script=["close"])

    with session as opened:
        step(opened)

    assert "It should greet." in runner.turns[0]


def test_an_issue_with_no_parent_gets_no_background(
    config: Config, decomposed: FakeTracker
) -> None:
    """Background is context. A turn without it is still a turn."""
    decomposed.issues = [Issue(id="bd-1", title="Standalone", status="open")]
    session, runner = build(config, tracker=decomposed, script=["close"])

    with session as opened:
        step(opened)

    assert "Background" not in runner.turns[0]


def test_an_unreadable_parent_does_not_take_the_turn_down(
    config: Config, decomposed: FakeTracker
) -> None:
    def explode(issue_id: str) -> Issue:
        raise MilhouseError("dolt is having a moment")

    decomposed.get = explode  # ty: ignore[invalid-assignment]
    session, runner = build(config, tracker=decomposed, script=["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert "Background" not in runner.turns[0]


def test_a_second_attempt_is_told_what_the_first_one_did(
    config: Config, decomposed: FakeTracker
) -> None:
    """The event log is the only memory a fresh context window gets."""
    decomposed.issues = decomposed.issues[:1]
    session, runner = build(config, tracker=decomposed, script=["stall"])
    with session as opened:
        step(opened)

    resumed, runner = build(config, tracker=decomposed, script=["close"])
    with resumed as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.number == 2
    assert "attempt 2" in runner.turns[0]
    assert "stalled" in runner.turns[0]


# -- reporting an empty queue --------------------------------------------------


def test_everything_closed_is_finished(config: Config, decomposed: FakeTracker) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        reason, completed = nothing_ready(opened)

    assert completed
    assert reason == "no issues are ready; everything in scope is closed"


def test_a_queue_that_is_merely_stuck_is_not_reported_as_finished(
    config: Config, decomposed: FakeTracker
) -> None:
    """An empty ready queue means "finished" or "stuck", and they are opposites.

    A dogfood run hit a permission prompt on every issue and then reported "the
    epic is finished" and exited 0 with nothing done.
    """
    for issue in decomposed.issues:
        issue.status = "blocked"
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        reason, completed = nothing_ready(opened)

    assert not completed
    assert "unfinished" in reason
    for issue in decomposed.issues:
        assert issue.id in reason


def test_an_epic_nobody_closed_is_not_unfinished_work(
    config: Config, decomposed: FakeTracker
) -> None:
    """The ready queue skips epics, so the completion check has to skip them too."""
    for issue in decomposed.issues:
        issue.status = "closed"
    decomposed.issues.append(Issue(id="bd-e2", title="Later", status="open", issue_type="epic"))
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        _, completed = nothing_ready(opened)

    assert completed
