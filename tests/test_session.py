"""Tests for the session: the lock, the branch, the workspace, and the claim.

Nothing here is about policy. A session claims and releases when it is told to,
and knows how to pick the run back up after a crash.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from milhouse.config import Config
from milhouse.errors import MilhouseError, RunLockedError, UserAbortError
from milhouse.models import Issue
from milhouse.policy import Decision

from .doubles import FakeClient, FakeRepo, FakeTracker, build


@pytest.fixture
def decomposed() -> FakeTracker:
    """A tracker with an epic and two open issues."""
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


# -- the lock ------------------------------------------------------------------


def test_a_second_session_may_open_but_not_take_the_same_lane(
    config: Config, decomposed: FakeTracker
) -> None:
    """Concurrent lanes are the point, so the lock is per lane, not per run."""
    first, _ = build(config, tracker=decomposed, script=[])
    second, _ = build(config, tracker=decomposed, script=[])

    with first as opened, second as also_opened:
        opened.lock_for("bd-e.1")

        also_opened.lock_for("bd-e.2")
        with pytest.raises(RunLockedError):
            also_opened.lock_for("bd-e.1")


def test_the_lock_is_dropped_even_when_opening_fails(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=[])

    def explode() -> None:
        raise MilhouseError("herdr is not running")

    session.open_workspace = explode  # ty: ignore[invalid-assignment]

    with pytest.raises(MilhouseError, match="herdr is not running"):
        session.__enter__()

    assert not (config.run_dir() / "bd-e.1" / "lock.json").exists()


# -- reconciliation ------------------------------------------------------------


def test_a_stale_claim_is_reopened_when_the_session_opens(
    config: Config, decomposed: FakeTracker
) -> None:
    """SIGKILL leaves a claim behind; re-running is the recovery mechanism."""
    session, _ = build(config, tracker=decomposed, script=[])
    decomposed.issues[0].status = "in_progress"
    session.audit.claimed("bd-e.1")

    with build(config, tracker=decomposed, script=[])[0] as resumed:
        assert resumed.in_flight == []

    assert decomposed.released == ["bd-e.1"]


def test_an_in_flight_claim_is_released_on_the_way_out(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        opened.claim()
        assert opened.in_flight == ["bd-e.1"]

    assert decomposed.released == ["bd-e.1"]
    assert decomposed.issues[0].status == "open"


# -- branching -----------------------------------------------------------------


def test_the_run_stays_on_the_branch_it_found(config: Config, decomposed: FakeTracker) -> None:
    """No task, no branch to name after one. Lanes decide this next (ADR 0020)."""
    repo = FakeRepo(branch="some-worktree-branch", dirty=True)
    session, _ = build(config, tracker=decomposed, script=[], repo=repo)

    with session as opened:
        assert opened.branch == "some-worktree-branch"

    assert repo.branch == "some-worktree-branch"


# -- a run's lane and lock -----------------------------------------------------


def test_a_run_works_every_issue_in_the_lane_named_after_its_target(
    config: Config, decomposed: FakeTracker
) -> None:
    """One reviewable branch, rather than an epic finished in several places."""
    client = FakeClient()
    session, _ = build(config, tracker=decomposed, script=[], client=client)
    session._runner = None
    session.lane_key = "bd-e"

    with session as opened:
        first = opened.runner_for(decomposed.issues[0])
        second = opened.runner_for(decomposed.issues[1])

    assert first.workdir == second.workdir
    assert first.agent_name == second.agent_name == "milhouse-bd-e"
    assert client.workspaces[first.pane_id.split(":")[0]] == "bd-e"


def test_a_run_holds_one_lock_however_many_issues_it_works(
    config: Config, decomposed: FakeTracker
) -> None:
    """A lock per issue would let a second run start the moment this one moved on."""
    session, _ = build(config, tracker=decomposed, script=[])
    session.lane_key = "bd-e"

    with session as opened:
        assert opened.lock_for("bd-e.1") is opened.lock_for("bd-e.2")
        assert (config.run_dir() / "bd-e" / "lock.json").exists()

    assert not (config.run_dir() / "bd-e.1" / "lock.json").exists()


def test_without_a_lane_key_each_issue_still_gets_its_own_lock(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        assert opened.lock_for("bd-e.1") is not opened.lock_for("bd-e.2")


# -- settling an issue ---------------------------------------------------------


def test_deferring_an_issue_also_returns_it_to_the_open_pool(
    config: Config, decomposed: FakeTracker
) -> None:
    """A deferred issue still claimed reads as work somebody is doing."""
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        opened.claim()
        opened.settle(
            "bd-e.1",
            Decision(issue="defer", reason="3 attempts, still stalling", note="gave up"),
        )

    assert decomposed.deferred == [("bd-e.1", "3 attempts, still stalling")]
    assert decomposed.released == ["bd-e.1"]
    assert decomposed.issues[0].status == "open"
    assert decomposed.notes == [("bd-e.1", "gave up")]
    # And it is no longer this process's to release on the way out.
    assert session.in_flight == []


def test_a_tracker_that_will_not_defer_does_not_take_the_turn_down(
    config: Config, decomposed: FakeTracker
) -> None:
    """The turn already happened, so losing the bookkeeping beats losing it."""
    session, _ = build(config, tracker=decomposed, script=[])

    def explode(issue_id: str, *, reason: str) -> None:
        raise MilhouseError("dolt is having a moment")

    decomposed.defer = explode  # ty: ignore[invalid-assignment]

    with session as opened:
        opened.claim()
        opened.settle("bd-e.1", Decision(issue="defer", reason="out of attempts"))

    assert decomposed.released == ["bd-e.1"]
    assert session.in_flight == []


# -- the background a prompt gets ----------------------------------------------


def test_the_background_comes_from_the_parent(config: Config, decomposed: FakeTracker) -> None:
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        assert opened.background(decomposed.issues[0]) == "It should greet."


def test_an_issue_with_no_parent_has_no_background(config: Config, decomposed: FakeTracker) -> None:
    session, _ = build(config, tracker=decomposed, script=[])
    orphan = Issue(id="bd-1", title="Standalone", status="open")

    with session as opened:
        assert opened.background(orphan) == ""


# -- signals -------------------------------------------------------------------


def test_an_interrupt_reverts_the_claim_and_drops_the_lock(
    config: Config, decomposed: FakeTracker
) -> None:
    """SIGTERM would otherwise kill the process before teardown ever ran."""
    session, _ = build(config, tracker=decomposed, script=[])

    with pytest.raises(UserAbortError), session as opened:
        opened.claim()
        os.kill(os.getpid(), signal.SIGTERM)

    assert decomposed.released == ["bd-e.1"]
    assert not (config.run_dir() / "bd-e.1" / "lock.json").exists()


def test_the_previous_handlers_are_put_back(config: Config, decomposed: FakeTracker) -> None:
    """Milhouse is a library as well as a command; it does not keep the signals."""
    before = signal.getsignal(signal.SIGTERM)
    session, _ = build(config, tracker=decomposed, script=[])

    with session:
        assert signal.getsignal(signal.SIGTERM) is not before

    assert signal.getsignal(signal.SIGTERM) is before


# -- the workspace -------------------------------------------------------------


def test_a_workspace_is_created_and_labelled_after_the_repository(
    config: Config, decomposed: FakeTracker
) -> None:
    client = FakeClient()
    session, _ = build(config, tracker=decomposed, script=[], client=client)

    with session as opened:
        assert opened.workspace is not None
        assert opened.workspace.workspace_id == "wG"
        assert opened.branch == "main"

    assert client.workspaces == {"wG": f"milhouse:{config.repo_root.name}"}


def test_a_second_run_finds_the_first_one_s_workspace_by_label(
    config: Config, decomposed: FakeTracker
) -> None:
    """No workspace id is written down, so milhouse asks the tool that owns them."""
    client = FakeClient(workspaces={"wY": f"milhouse:{config.repo_root.name}"})
    session, _ = build(config, tracker=decomposed, script=[], client=client)

    with session as opened:
        assert opened.workspace is not None
        assert opened.workspace.workspace_id == "wY"

    assert not client.focused


def test_a_configured_workspace_for_another_repo_is_not_used(
    config: Config, decomposed: FakeTracker
) -> None:
    """Herdr reads the repository off the source workspace, so this branches the wrong one."""
    client = FakeClient(
        workspaces={"wE": "some-other-project"},
        workspace_repos={"wE": Path("/elsewhere/other")},
    )
    config.herdr.workspace = "wE"
    lines: list[str] = []
    session, _ = build(config, tracker=decomposed, script=[], client=client)
    session.report = lines.append

    with session as opened:
        assert opened.workspace is not None
        assert opened.workspace.workspace_id != "wE"

    assert any("ignoring herdr workspace wE" in line for line in lines)
    assert any("/elsewhere/other" in line for line in lines)


def test_a_configured_workspace_for_this_repo_is_used(
    config: Config, decomposed: FakeTracker
) -> None:
    client = FakeClient(
        workspaces={"wE": "whatever"},
        workspace_repos={"wE": config.repo_root},
    )
    config.herdr.workspace = "wE"
    session, _ = build(config, tracker=decomposed, script=[], client=client)

    with session as opened:
        assert opened.workspace is not None
        assert opened.workspace.workspace_id == "wE"


def test_a_workspace_herdr_reports_no_repository_for_is_still_used(
    config: Config, decomposed: FakeTracker
) -> None:
    """Herdr allows a workspace with no worktree; refusing one would break the usual case."""
    client = FakeClient(workspaces={"wE": "whatever"})
    config.herdr.workspace = "wE"
    session, _ = build(config, tracker=decomposed, script=[], client=client)

    with session as opened:
        assert opened.workspace is not None
        assert opened.workspace.workspace_id == "wE"


def test_a_turn_runs_in_the_issue_s_own_lane(config: Config, decomposed: FakeTracker) -> None:
    """No agent runs in the source workspace: every turn happens in a worktree."""
    client = FakeClient()
    session, _ = build(config, tracker=decomposed, script=[], client=client)
    session._runner = None

    with session as opened:
        runner = opened.runner_for(decomposed.issues[0])

    assert runner.workdir != config.repo_root
    assert runner.agent_name == "milhouse-bd-e_1"
    assert client.workspaces[runner.pane_id.split(":")[0]] == "bd-e.1"


def test_a_configured_workspace_is_the_source_rather_than_a_new_one(
    config: Config, decomposed: FakeTracker
) -> None:
    """Run from inside a pane, HERDR_WORKSPACE_ID names the workspace it is in.

    That is the checkout a lane's worktree comes from, and no agent runs in it,
    so nothing here can take the pane milhouse was typed into.
    """
    config.herdr.workspace = "wG"
    config.herdr.self_pane = "wG:p1"
    client = FakeClient(workspaces={"wG": "somebody-elses-label"})
    session, _ = build(config, tracker=decomposed, script=[], client=client)

    with session as opened:
        assert opened.workspace is not None
        assert opened.workspace.workspace_id == "wG"

    assert client.workspaces == {"wG": "somebody-elses-label"}
