"""Tests for one iteration: what it sends, what it records, what it settles."""

from __future__ import annotations

from pathlib import Path

import pytest

from milhouse.config import Config
from milhouse.errors import MilhouseError
from milhouse.gitrepo import Merge
from milhouse.herdr import Worktree
from milhouse.models import Issue
from milhouse.policy import Decision, unattended
from milhouse.renderer import PlainRenderer
from milhouse.runner import TurnResult
from milhouse.session import Session
from milhouse.step import DispatchResult, dispatch, nothing_ready, reap, step

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


def test_every_progress_line_names_the_turn_it_belongs_to(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """With four turns in flight, an unlabelled `→ success` says nothing (ADR 0024)."""
    lines: list[str] = []
    config.verify.command = ["a-gate"]
    fake_proc.expect("a-gate", Reply())
    session, _ = build(config, tracker=decomposed, script=["close", "close"])
    session.report = lines.append

    with session as opened:
        step(opened)
        step(opened)

    indented = [line for line in lines if line.startswith("  ")]
    assert indented, "a turn reports nothing under its header line"
    for line in indented:
        assert line.split()[0] in {"bd-e.1", "bd-e.2"}, f"{line!r} does not say whose turn it is"
    # And each turn's outcome is attributable to it rather than to the run.
    assert any(line.startswith("  bd-e.1  → success") for line in indented)
    assert any(line.startswith("  bd-e.2  → success") for line in indented)


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


def with_lane(client: FakeClient, issue_id: str, *, branch: str = "") -> FakeClient:
    """Stand a lane up under `issue_id`, the way a real dispatch would have.

    The tests inject a runner, so `Session.runner_for` never opens one itself.
    `branch` overrides the branch it commits to, which is how a run's worker lane
    differs from a `dispatch` lane carrying the same label (ADR 0024).
    """
    workspace_id = f"wL{len(client.workspaces)}"
    client.workspaces[workspace_id] = issue_id
    client.checkouts.append(
        Worktree(
            path=Path("/worktrees") / issue_id,
            branch=branch or f"milhouse/{issue_id}",
            workspace_id=workspace_id,
        )
    )
    return client


def test_dispatch_starts_a_turn_and_does_not_wait(config: Config, decomposed: FakeTracker) -> None:
    session, runner = build(config, tracker=decomposed, script=["close"])
    runner.working = True

    with session as opened:
        started = dispatch(opened).started

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
        started = dispatch(opened, limit=5).started

    # Only two issues exist, so the queue runs dry before the limit does.
    assert [pending.issue.id for pending in started] == ["bd-e.1", "bd-e.2"]


def test_dispatch_reports_nothing_when_the_queue_is_empty(
    config: Config, decomposed: FakeTracker
) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        assert dispatch(opened) == DispatchResult()


def test_a_turn_that_will_not_start_is_settled_rather_than_left_claimed(
    config: Config, decomposed: FakeTracker
) -> None:
    """An agent that never ran will never be reaped, so it cannot be handed off."""
    session, _ = build(config, tracker=decomposed, script=["error"])

    with session as opened:
        assert dispatch(opened).started == []

    assert [item.outcome for item in session.audit.iterations()] == ["error"]
    assert decomposed.released == ["bd-e.1"]


def test_a_turn_that_will_not_start_is_handed_back_rather_than_dropped(
    config: Config, decomposed: FakeTracker
) -> None:
    """milhouse-amd.14: it is an `error` iteration, which is a row of the halt table.

    Settling it inside `dispatch` and returning nothing left the caller unable to
    tell a sick agent side from an empty queue.
    """
    session, _ = build(config, tracker=decomposed, script=["error"])

    with session as opened:
        dispatched = dispatch(opened, limit=2)

    assert dispatched.started == []
    assert [result.iteration.issue_id for result in dispatched.failed] == ["bd-e.1"]
    assert [result.iteration.outcome for result in dispatched.failed] == ["error"]
    assert "herdr fell over" in dispatched.failed[0].iteration.detail


def test_one_dispatch_charges_one_issue_one_attempt(
    config: Config, decomposed: FakeTracker
) -> None:
    """milhouse-amd.14: the loop was handed the issue it had just released, twice more.

    `_finish` re-opens the issue, so `bd ready` offers it straight back and a
    `--count 3` dispatch could spend an issue's whole retry ladder on one thing
    that was wrong with the agent side — three attempts and a deferral, inside a
    single call, in the time three failures take.
    """
    session, runner = build(config, tracker=decomposed, script=["unsubmitted"] * 3)

    with session as opened:
        dispatched = dispatch(opened, limit=3, policy=unattended(max_attempts=3))

    assert [item.attempt for item in session.audit.iterations()] == [1]
    assert len(dispatched.failed) == 1
    assert len(runner.turns) == 1
    assert decomposed.deferred == []
    # And the issues behind it in the queue were never claimed either.
    assert decomposed.released == ["bd-e.1"]
    assert decomposed.issues[1].status == "open"


def test_a_prompt_the_agent_never_took_is_never_dispatched_or_reaped(
    config: Config, decomposed: FakeTracker
) -> None:
    """milhouse-amd.12: nine of seventeen turns settled having done nothing.

    A prompt swallowed by a just-started agent leaves herdr reporting the same
    "not working" it reports for an agent that has finished, so `reap` collected
    the turn about twelve seconds later and `classify` called it `stalled` — the
    issue was open and nothing was committed, which was true and was not what
    happened. Three of those spend an issue's whole retry ladder in under a
    minute, at poll speed rather than agent speed.

    The fix is at the seam: `dispatch` now confirms the prompt was taken, so
    there is no turn in flight to misread. This asserts the whole arc, because
    the damage was never in one call — it was a turn recorded as started, then
    read back by a poller that had nothing true left to read.
    """
    client = with_lane(FakeClient(), "bd-e.1")
    session, _ = build(config, tracker=decomposed, script=["unsubmitted"], client=client)

    with session as opened:
        assert dispatch(opened).started == []
        # Nothing is in flight, so a poller finds nothing to collect.
        assert opened.audit.dispatches() == {}
        assert reap(opened) == []

    outcomes = [item.outcome for item in session.audit.iterations()]
    assert "stalled" not in outcomes
    assert outcomes == ["error"]
    # And it says which agent, and what herdr said about it.
    assert "did not observe" in session.audit.iterations()[0].detail
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


def test_many_polls_over_one_unchanged_turn_do_not_grow_the_plain_output(
    config: Config, decomposed: FakeTracker, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `--count N` run at the default `poll_ms` against a long turn.

    `reap` used to print "... is still working" on every one of these polls.
    Through the plain renderer, none past the first does.
    """
    client = with_lane(FakeClient(), "bd-e.1")
    session, runner = build(config, tracker=decomposed, script=["close"], client=client)
    runner.working = True
    session.report = PlainRenderer().handle

    with session as opened:
        dispatch(opened)
        capsys.readouterr()  # drop the dispatch line; only the polls are under test
        for _ in range(50):
            assert reap(opened) == []

    assert capsys.readouterr().out.count("\n") <= 1


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


# -- landing a worker lane in the integration branch ---------------------------

WORKER_PATH = Path("/worktrees/milhouse-bd-e--bd-e.1")
"""Where a run of `bd-e` checks out the worker lane for `bd-e.1`."""

WORKER_BRANCH = "milhouse/bd-e--bd-e.1"
"""What that lane commits to, namespaced under the target (ADR 0024)."""

INTEGRATION_PATH = Path("/worktrees/milhouse-bd-e")
"""Where the same run checks out the one branch a person reviews."""


def landing(
    config: Config,
    tracker: FakeTracker,
    script: list[str],
    *,
    client: FakeClient | None = None,
) -> tuple[Session, FakeRunner, FakeRepo]:
    """A run of `bd-e` whose turn happens in `bd-e.1`'s worker lane.

    Which is what `--count N` above one assembles. The runner is still injected,
    so no agent is started, but it works in a second checkout on a branch of its
    own, and a branch of its own is the whole reason there is anything to merge.
    """
    repo = FakeRepo(branches={WORKER_PATH: WORKER_BRANCH})
    session, runner = build(
        config,
        tracker=tracker,
        script=script,
        repo=repo,
        client=client,
        lane_key="bd-e",
        worker_lanes=True,
    )
    runner.workdir = WORKER_PATH
    return session, runner, repo


def test_a_successful_worker_turn_lands_on_the_integration_branch(
    config: Config, decomposed: FakeTracker
) -> None:
    """The work has to reach the branch under review, or nobody will see it."""
    session, _, repo = landing(config, decomposed, ["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "success"
    assert repo.merged == [(INTEGRATION_PATH, WORKER_BRANCH)]
    merge = result.iteration.merge
    assert merge is not None
    assert (merge.source, merge.target) == (WORKER_BRANCH, "milhouse/bd-e")
    assert merge.sha == "shaM"
    assert merge.landed
    # And it survives the round trip through the audit log, which is where a
    # later process reads what a run did.
    recorded = session.audit.iterations()[0].merge
    assert recorded is not None
    assert (recorded.sha, recorded.source, recorded.target) == (
        merge.sha,
        merge.source,
        merge.target,
    )


def test_a_merge_that_joined_two_histories_says_so_and_a_fast_forward_does_not(
    config: Config, decomposed: FakeTracker
) -> None:
    """Whether git had to join anything is what decides a second gate run."""
    session, _, repo = landing(config, decomposed, ["close"])
    repo.merge_result = Merge(sha="shaW", fast_forwarded=True)

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.merge is not None
    assert result.iteration.merge.fast_forwarded
    assert not result.iteration.merge.joined
    assert result.iteration.merge.landed


def test_a_conflict_names_both_branches_and_loses_nothing(
    config: Config, decomposed: FakeTracker
) -> None:
    """A closed issue, a live branch, and an integration branch without its work."""
    lines: list[str] = []
    session, _, repo = landing(config, decomposed, ["close"])
    repo.merge_result = Merge(sha=None, fast_forwarded=False, conflicts=("src/a.py", "src/b.py"))
    session.report = lines.append

    with session as opened:
        result = step(opened)

    assert result is not None
    # The turn still succeeded: the agent did the work and closed the issue.
    assert result.iteration.outcome == "success"
    assert decomposed.issues[0].is_closed
    assert decomposed.released == []
    merge = result.iteration.merge
    assert merge is not None
    assert not merge.landed
    assert merge.conflicts == ["src/a.py", "src/b.py"]
    assert merge.sha is None
    # Both branches are named, because the recovery is entirely by hand.
    reported = " ".join(lines)
    assert WORKER_BRANCH in reported
    assert "milhouse/bd-e" in reported
    assert "src/a.py" in reported


def test_a_turn_that_did_not_succeed_is_not_merged(config: Config, decomposed: FakeTracker) -> None:
    """Its commits stay on its worker branch, where the next attempt finds them."""
    session, _, repo = landing(config, decomposed, ["commit"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "partial"
    assert result.iteration.commits == ["sha1"]
    assert result.iteration.merge is None
    assert repo.merged == []
    # Back in the queue, and back to the same lane, which is where its work is.
    assert decomposed.released == ["bd-e.1"]


def test_a_merge_git_refuses_still_records_the_turn(
    config: Config, decomposed: FakeTracker
) -> None:
    """The turn has already happened; losing it to report the merge would be worse."""
    session, _, repo = landing(config, decomposed, ["close"])

    def explode(branch: str, *, message: str = "") -> Merge:
        raise MilhouseError("could not merge: the index is locked")

    repo.merge = explode  # ty: ignore[invalid-assignment]

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "success"
    merge = result.iteration.merge
    assert merge is not None
    assert not merge.landed
    assert "the index is locked" in merge.error
    assert [item.outcome for item in session.audit.iterations()] == ["success"]


def test_a_reaped_worker_turn_lands_the_branch_herdr_says_it_ran_on(
    config: Config, decomposed: FakeTracker
) -> None:
    """A concurrent run dispatches and reaps, so that is the path that merges."""
    client = with_lane(FakeClient(), "bd-e.1", branch=WORKER_BRANCH)
    session, runner, repo = landing(config, decomposed, ["close"], client=client)
    runner.working = True
    with session as opened:
        dispatch(opened)

    assert repo.merged == []

    runner.working = False
    with session as opened:
        results = reap(opened)

    assert [result.iteration.outcome for result in results] == ["success"]
    assert repo.merged == [(INTEGRATION_PATH, WORKER_BRANCH)]


SECOND_BRANCH = "milhouse/bd-e--bd-e.2"
"""The worker branch of a turn that settles after one has already conflicted."""


def two_lanes() -> FakeClient:
    """Herdr holding a worker lane per issue, which is what `--count 2` leaves."""
    client = with_lane(FakeClient(), "bd-e.1", branch=WORKER_BRANCH)
    return with_lane(client, "bd-e.2", branch=SECOND_BRANCH)


def test_nothing_is_merged_into_a_branch_that_has_already_refused_a_merge(
    config: Config, decomposed: FakeTracker
) -> None:
    """The integration branch is no longer a prefix of the merge order (ADR 0024)."""
    session, runner, repo = landing(config, decomposed, ["close", "close"], client=two_lanes())
    repo.merge_result = Merge(sha=None, fast_forwarded=False, conflicts=("src/a.py",))
    runner.working = True
    with session as opened:
        dispatch(opened, limit=2)

    runner.working = False
    lines: list[str] = []
    session.report = lines.append
    with session as opened:
        results = reap(opened)

    # Both turns settled in the same reap pass, which is how the second one was
    # merged in the watched run before `run()` had been told about the first.
    assert [result.iteration.issue_id for result in results] == ["bd-e.1", "bd-e.2"]
    assert repo.merged == [(INTEGRATION_PATH, WORKER_BRANCH)]
    first, second = (result.iteration.merge for result in results)
    assert first is not None and second is not None
    assert first.conflicts == ["src/a.py"]
    assert not second.landed
    assert second.sha is None
    assert second.skipped == f"{WORKER_BRANCH} did not land in milhouse/bd-e"
    assert session.refused_merge is first
    # And the branch that has to be landed first is named where somebody sees it.
    named = [line for line in lines if f"{SECOND_BRANCH} was not merged" in line]
    assert named and all(WORKER_BRANCH in line for line in named)


def test_a_turn_whose_branch_is_not_merged_is_still_finished(
    config: Config, decomposed: FakeTracker
) -> None:
    """A drain exists so in-flight work is not abandoned; only the merge stops."""
    session, runner, repo = landing(config, decomposed, ["close", "close"], client=two_lanes())
    repo.merge_result = Merge(sha=None, fast_forwarded=False, conflicts=("src/a.py",))
    runner.working = True
    with session as opened:
        dispatch(opened, limit=2)

    runner.working = False
    with session as opened:
        results = reap(opened)

    assert [result.iteration.outcome for result in results] == ["success", "success"]
    # Closed, recorded, settled, and its commits are on its own branch either way.
    assert all(issue.is_closed for issue in decomposed.issues)
    assert decomposed.released == []
    assert [item.issue_id for item in session.audit.iterations()] == ["bd-e.1", "bd-e.2"]
    recorded = session.audit.iterations()[1].merge
    assert recorded is not None
    assert recorded.skipped and not recorded.landed


def test_a_step_with_no_integration_lane_merges_nothing(
    config: Config, decomposed: FakeTracker
) -> None:
    """`step`, `dispatch` and `reap` have no branch to land anything in."""
    repo = FakeRepo()
    session, _ = build(config, tracker=decomposed, script=["close"], repo=repo)

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "success"
    assert result.iteration.merge is None
    assert repo.merged == []


def test_a_run_without_worker_lanes_merges_nothing(config: Config, decomposed: FakeTracker) -> None:
    """At `--count 1` the turn happens in the integration lane: ADR 0023 exactly."""
    repo = FakeRepo()
    session, _ = build(config, tracker=decomposed, script=["close"], repo=repo, lane_key="bd-e")

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "success"
    assert result.iteration.merge is None
    assert repo.merged == []


# -- verifying the integration branch ------------------------------------------


def test_a_merge_that_joined_two_histories_is_verified_on_the_integration_branch(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """The merged tree is the only place two green branches can be seen to be red."""
    config.verify.command = ["make", "check"]
    fake_proc.expect("make check", Reply(stdout="ok"))
    session, _, _ = landing(config, decomposed, ["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert result.iteration.outcome == "success"
    # Once where the work happened, then again where it landed.
    assert fake_proc.where("make", "check") == [WORKER_PATH, INTEGRATION_PATH]
    assert result.iteration.verified is True
    assert result.iteration.integration_verified is True


def test_a_fast_forward_is_not_verified_a_second_time(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """It left the tree the lane was verified against, and paying twice for that is waste."""
    config.verify.command = ["make", "check"]
    fake_proc.expect("make check", Reply(stdout="ok"))
    session, _, repo = landing(config, decomposed, ["close"])
    repo.merge_result = Merge(sha="shaW", fast_forwarded=True)

    with session as opened:
        result = step(opened)

    assert result is not None
    assert fake_proc.where("make", "check") == [WORKER_PATH]
    assert result.iteration.integration_verified is None


def test_a_red_integration_branch_notes_the_issue_and_reverts_nothing(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """The work was done; it is the combination that is red, and somebody has to see it."""
    config.verify.command = ["make", "check"]
    fake_proc.expect(
        "make check",
        [Reply(stdout="ok"), Reply(stdout="FAILED tests/test_together.py", returncode=1)],
    )
    session, _, repo = landing(config, decomposed, ["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    # Nothing is undone: the issue stays closed and the merge stays merged.
    assert result.iteration.outcome == "success"
    assert decomposed.issues[0].is_closed
    assert decomposed.released == []
    assert repo.merged == [(INTEGRATION_PATH, WORKER_BRANCH)]
    assert result.iteration.merge is not None
    assert result.iteration.merge.landed
    # And the failure is on the issue, where somebody will look for it.
    assert result.iteration.integration_verified is False
    assert "FAILED tests/test_together.py" in result.iteration.integration_output
    issue_id, note = decomposed.notes[0]
    assert issue_id == "bd-e.1"
    assert "FAILED tests/test_together.py" in note
    assert "milhouse/bd-e" in note
    # The verdict reaches the audit log; the unbounded output stays off it.
    recorded = session.audit.iterations()[0]
    assert recorded.integration_verified is False
    assert recorded.integration_output == ""


def test_a_repository_with_no_gate_pays_for_no_extra_runs(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """No configured gate means no verification at all, merge or no merge."""
    session, _, repo = landing(config, decomposed, ["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert repo.merged == [(INTEGRATION_PATH, WORKER_BRANCH)]
    assert fake_proc.calls == []
    assert result.iteration.verified is None
    assert result.iteration.integration_verified is None


def test_a_merge_nobody_attempted_costs_no_gate_run_on_the_integration_branch(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """The turn's own gate still decides its outcome; the integration branch is untouched."""
    config.verify.command = ["make", "check"]
    fake_proc.expect("make check", Reply(stdout="ok"))
    session, runner, repo = landing(config, decomposed, ["close", "close"], client=two_lanes())
    repo.merge_result = Merge(sha=None, fast_forwarded=False, conflicts=("src/a.py",))
    runner.working = True
    with session as opened:
        dispatch(opened, limit=2)

    runner.working = False
    with session as opened:
        results = reap(opened)

    # Once per turn, in the lane the work happened in, and never on the branch
    # nothing was merged into.
    assert fake_proc.where("make", "check") == [WORKER_PATH, WORKER_PATH]
    assert [result.iteration.verified for result in results] == [True, True]
    assert [result.iteration.integration_verified for result in results] == [None, None]


def test_a_turn_that_landed_nothing_verifies_no_integration_branch(
    config: Config, decomposed: FakeTracker, fake_proc: FakeProc
) -> None:
    """`step` has no integration lane, so the gate runs once, in the lane it worked."""
    config.verify.command = ["make", "check"]
    fake_proc.expect("make check", Reply(stdout="ok"))
    session, _ = build(config, tracker=decomposed, script=["close"])

    with session as opened:
        result = step(opened)

    assert result is not None
    assert fake_proc.where("make", "check") == [config.repo_root]
    assert result.iteration.integration_verified is None
