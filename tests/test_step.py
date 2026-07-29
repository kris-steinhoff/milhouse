"""Tests for one iteration: what it sends, what it records, what it settles."""

from __future__ import annotations

from pathlib import Path

import pytest

from milhouse.config import Config
from milhouse.errors import MilhouseError
from milhouse.herdr import Worktree
from milhouse.models import Issue
from milhouse.policy import Decision
from milhouse.runner import TurnResult
from milhouse.step import dispatch, nothing_ready, reap, step

from .doubles import FakeClient, FakeRepo, FakeRunner, FakeTracker, build
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


def test_the_attempt_is_recorded_and_counted_per_issue(
    config: Config, decomposed: FakeTracker
) -> None:
    """`number` counts turns in the repository; `attempt` counts tries at one issue."""
    session, _ = build(config, tracker=decomposed, script=["stall"])
    with session as opened:
        first = step(opened)

    # A different issue, so this is iteration 2 and attempt 1.
    decomposed.issues[0].status = "closed"
    session, _ = build(config, tracker=decomposed, script=["stall"])
    with session as opened:
        other = step(opened)

    assert first is not None and other is not None
    assert (first.iteration.number, first.iteration.attempt) == (1, 1)
    assert (other.iteration.number, other.iteration.attempt) == (2, 1)
    # And it survives the round trip through the audit log.
    assert [item.attempt for item in session.audit.iterations()] == [1, 1]


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


# -- dispatch and reap ---------------------------------------------------------


def with_lane(client: FakeClient, issue_id: str) -> FakeClient:
    """Stand a lane up under `issue_id`, the way a real dispatch would have.

    The tests inject a runner, so `Session.runner_for` never opens one itself.
    """
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


def test_dispatch_starts_a_turn_and_does_not_wait(config: Config, decomposed: FakeTracker) -> None:
    session, runner = build(config, tracker=decomposed, script=["close"])
    runner.working = True

    with session as opened:
        started = dispatch(opened)

        assert [pending.issue.id for pending in started] == ["bd-e.1"]
        assert runner.turns  # the prompt was submitted
        # Nothing has been classified: no iteration is recorded yet.
        assert opened.audit.iterations() == []
        # The claim belongs to the lane now, not to this process.
        assert opened.in_flight == []

    assert decomposed.released == []


def test_dispatch_takes_up_to_the_count_asked_for(config: Config, decomposed: FakeTracker) -> None:
    session, runner = build(config, tracker=decomposed, script=["stall", "stall"])
    runner.working = True

    with session as opened:
        started = dispatch(opened, limit=5)

    # Only two issues exist, so the queue runs dry before the limit does.
    assert [pending.issue.id for pending in started] == ["bd-e.1", "bd-e.2"]


def test_dispatch_reports_nothing_when_the_queue_is_empty(
    config: Config, decomposed: FakeTracker
) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        assert dispatch(opened) == []


def test_a_turn_that_will_not_start_is_settled_rather_than_left_claimed(
    config: Config, decomposed: FakeTracker
) -> None:
    """An agent that never ran will never be reaped, so it cannot be handed off."""
    session, _ = build(config, tracker=decomposed, script=["error"])

    with session as opened:
        assert dispatch(opened) == []

    assert [item.outcome for item in session.audit.iterations()] == ["error"]
    assert decomposed.released == ["bd-e.1"]


def test_a_dispatched_turn_survives_the_session_that_started_it(
    config: Config, decomposed: FakeTracker
) -> None:
    """Teardown must not re-open a claim an agent is still working."""
    session, runner = build(config, tracker=decomposed, script=["stall"])
    runner.working = True

    with session as opened:
        dispatch(opened)

    assert decomposed.released == []
    assert decomposed.issues[0].status == "in_progress"


def test_reap_finishes_a_settled_turn(config: Config, decomposed: FakeTracker) -> None:
    client = with_lane(FakeClient(), "bd-e.1")
    session, runner = build(config, tracker=decomposed, script=["close"], client=client)
    runner.working = True
    with session as opened:
        dispatch(opened)

    runner.working = False
    with session as opened:
        results = reap(opened)

    assert [result.iteration.outcome for result in results] == ["success"]
    assert decomposed.issues[0].is_closed
    # A settled turn is settled once: reaping again finds nothing.
    assert session.audit.dispatches() == {}


def test_a_reaped_turn_keeps_the_attempt_it_was_dispatched_as(
    config: Config, decomposed: FakeTracker
) -> None:
    """Reap may be another process, and by then the history no longer says."""
    decomposed.issues = decomposed.issues[:1]
    client = with_lane(FakeClient(), "bd-e.1")
    session, _ = build(config, tracker=decomposed, script=["stall"], client=client)
    with session as opened:
        step(opened)

    session, runner = build(config, tracker=decomposed, script=["stall"], client=client)
    runner.working = True
    with session as opened:
        dispatch(opened)

    runner.working = False
    with session as opened:
        results = reap(opened)

    assert [result.iteration.attempt for result in results] == [2]


def test_reap_leaves_a_turn_that_is_still_working(config: Config, decomposed: FakeTracker) -> None:
    client = with_lane(FakeClient(), "bd-e.1")
    session, runner = build(config, tracker=decomposed, script=["close"], client=client)
    runner.working = True

    with session as opened:
        dispatch(opened)
        assert reap(opened) == []

    assert list(session.audit.dispatches()) == ["bd-e.1"]


def test_a_reaped_turn_that_did_not_finish_reopens_its_issue(
    config: Config, decomposed: FakeTracker
) -> None:
    client = with_lane(FakeClient(), "bd-e.1")
    session, runner = build(config, tracker=decomposed, script=["stall"], client=client)
    runner.working = True
    with session as opened:
        dispatch(opened)

    runner.working = False
    with session as opened:
        results = reap(opened)

    assert [result.iteration.outcome for result in results] == ["stalled"]
    assert decomposed.released == ["bd-e.1"]


def test_a_turn_past_the_timeout_is_reaped_anyway(config: Config, decomposed: FakeTracker) -> None:
    """Nobody is waiting on a dispatched turn, so the deadline is the record."""
    client = with_lane(FakeClient(), "bd-e.1")
    session, runner = build(config, tracker=decomposed, script=["stall"], client=client)
    runner.working = True
    config.agent.turn_timeout_ms = 0

    with session as opened:
        dispatch(opened)
        results = reap(opened)

    assert [result.iteration.outcome for result in results] == ["timeout"]


# -- reconciling around a dispatched turn --------------------------------------


def test_reconcile_leaves_a_dispatched_turn_alone(config: Config, decomposed: FakeTracker) -> None:
    """An in-progress issue with a live lane has somebody working it."""
    client = with_lane(FakeClient(), "bd-e.1")
    session, runner = build(config, tracker=decomposed, script=["stall"], client=client)
    runner.working = True
    with session as opened:
        dispatch(opened)

    later, _ = build(config, tracker=decomposed, script=[], client=client)
    with later:
        pass

    assert decomposed.released == []


def test_reconcile_reopens_a_claim_whose_lane_is_gone(
    config: Config, decomposed: FakeTracker
) -> None:
    session, runner = build(config, tracker=decomposed, script=["stall"])
    runner.working = True
    with session as opened:
        dispatch(opened)

    later, _ = build(config, tracker=decomposed, script=[])
    with later:
        pass

    assert decomposed.released == ["bd-e.1"]
