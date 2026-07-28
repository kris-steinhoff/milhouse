"""Tests for the session: the lock, the branch, the workspace, and the state.

Nothing here is about policy. A session claims and releases when it is told to,
and knows how to pick the run back up after a crash.
"""

from __future__ import annotations

import os
import signal

import pytest

from milhouse.config import Config
from milhouse.errors import MilhouseError, RunLockedError, UserAbortError
from milhouse.models import Issue, RunState

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


def test_a_second_session_in_one_repository_is_refused(
    config: Config, decomposed: FakeTracker
) -> None:
    """Both would drive the same pane and reconcile each other's claim."""
    first, _ = build(config, tracker=decomposed, script=[])
    second, _ = build(config, tracker=decomposed, script=[])

    with first, pytest.raises(RunLockedError):
        second.__enter__()


def test_the_lock_is_dropped_even_when_opening_fails(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=[])

    def explode() -> None:
        raise MilhouseError("herdr is not running")

    session.open_workspace = explode  # ty: ignore[invalid-assignment]

    with pytest.raises(MilhouseError, match="herdr is not running"):
        session.__enter__()

    assert not session.store.lock.path.exists()


# -- reconciliation ------------------------------------------------------------


def test_a_stale_claim_is_reopened_when_the_session_opens(
    config: Config, decomposed: FakeTracker
) -> None:
    """SIGKILL leaves a claim behind; re-running is the recovery mechanism."""
    session, _ = build(config, tracker=decomposed, script=[])
    decomposed.issues[0].status = "in_progress"
    session.store.save(RunState(claimed_issue="bd-e.1"))

    with build(config, tracker=decomposed, script=[])[0] as resumed:
        assert resumed.state.claimed_issue is None

    assert decomposed.released == ["bd-e.1"]


def test_an_in_flight_claim_is_released_on_the_way_out(
    config: Config, decomposed: FakeTracker
) -> None:
    session, _ = build(config, tracker=decomposed, script=[])

    with session as opened:
        opened.claim()
        assert opened.state.claimed_issue == "bd-e.1"

    assert decomposed.released == ["bd-e.1"]
    assert decomposed.issues[0].status == "open"


# -- branching -----------------------------------------------------------------


def test_the_run_stays_on_the_branch_it_found(config: Config, decomposed: FakeTracker) -> None:
    """No task, no branch to name after one. Lanes decide this next (ADR 0020)."""
    repo = FakeRepo(branch="some-worktree-branch", dirty=True)
    session, _ = build(config, tracker=decomposed, script=[], repo=repo)

    with session as opened:
        assert opened.state.branch == "some-worktree-branch"

    assert repo.branch == "some-worktree-branch"


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
    assert not session.store.lock.path.exists()


def test_the_previous_handlers_are_put_back(config: Config, decomposed: FakeTracker) -> None:
    """Milhouse is a library as well as a command; it does not keep the signals."""
    before = signal.getsignal(signal.SIGTERM)
    session, _ = build(config, tracker=decomposed, script=[])

    with session:
        assert signal.getsignal(signal.SIGTERM) is not before

    assert signal.getsignal(signal.SIGTERM) is before


# -- the workspace -------------------------------------------------------------


def test_the_workspace_and_pane_are_recorded(config: Config, decomposed: FakeTracker) -> None:
    session, _ = build(config, tracker=decomposed, script=[])

    with session:
        pass

    saved = session.store.load()
    assert saved is not None
    assert saved.workspace_id == "wG"
    assert saved.pane_id == "wG:p1"
    assert saved.branch == "main"


def test_a_reused_workspace_does_not_take_the_callers_own_pane(
    config: Config, decomposed: FakeTracker
) -> None:
    """Run from inside a pane, HERDR_WORKSPACE_ID names the workspace it is in.

    That workspace contains the caller's pane, and starting an iteration agent
    there sends the exit keys to the session that launched milhouse.
    """
    config.herdr.workspace = "wG"
    config.herdr.self_pane = "wG:p1"
    client = FakeClient(workspaces={"wG"})
    session, _ = build(config, tracker=decomposed, script=[], client=client)

    with session:
        pass

    assert client.avoided == "wG:p1"
    saved = session.store.load()
    assert saved is not None
    assert saved.pane_id == "wG:p2"
