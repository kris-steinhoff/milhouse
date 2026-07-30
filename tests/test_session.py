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
from milhouse.herdr import Worktree
from milhouse.models import Issue
from milhouse.policy import Decision
from milhouse.session import Session

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


# -- a run's integration lane and its worker lanes -----------------------------


def concurrent(config: Config, tracker: FakeTracker, client: FakeClient) -> Session:
    """A run of ``bd-e`` that gives every issue in flight a worker lane.

    Which is what ``--count N`` above one will assemble. The runner is dropped so
    real lanes get opened, since the lanes are what these are about.
    """
    session, _ = build(config, tracker=tracker, script=[], client=client)
    session._runner = None
    session.lane_key = "bd-e"
    session.worker_lanes = True
    return session


def test_a_session_with_no_target_has_no_integration_lane(
    config: Config, decomposed: FakeTracker
) -> None:
    """`step`, `dispatch` and `reap` have no branch to land anything in."""
    session, _ = build(config, tracker=decomposed, script=[], client=FakeClient())

    with session as opened:
        assert opened.integration_lane() is None


def test_a_worker_lane_branches_off_the_integration_branch(
    config: Config, decomposed: FakeTracker
) -> None:
    """One reviewable branch, and a lane per issue coming off it (ADR 0024)."""
    client = FakeClient()
    session = concurrent(config, decomposed, client)

    with session as opened:
        first = opened.runner_for(decomposed.issues[0])
        second = opened.runner_for(decomposed.issues[1])
        integration = opened.integration_lane()

    assert integration is not None
    assert integration.branch == "milhouse/bd-e"
    assert first.workdir != second.workdir
    assert first.workdir != integration.path
    assert client.bases["milhouse/bd-e--bd-e.1"] == "milhouse/bd-e"
    assert client.bases["milhouse/bd-e--bd-e.2"] == "milhouse/bd-e"


def test_a_second_attempt_in_a_run_returns_to_the_issue_s_worker_lane(
    config: Config, decomposed: FakeTracker
) -> None:
    """A failed turn's commits stay there, which is where the retry finds them."""
    client = FakeClient()
    session = concurrent(config, decomposed, client)

    with session as opened:
        first = opened.runner_for(decomposed.issues[0])
        again = opened.runner_for(decomposed.issues[0])

    assert again.workdir == first.workdir
    assert again.agent_name == first.agent_name
    assert list(client.bases) == ["milhouse/bd-e", "milhouse/bd-e--bd-e.1"]


def test_a_run_with_worker_lanes_holds_one_lock_per_lane(
    config: Config, decomposed: FakeTracker
) -> None:
    """One on the target, one per worker lane, and all of them dropped at the end."""
    session = concurrent(config, decomposed, FakeClient())
    locks = [config.run_dir() / key / "lock.json" for key in ("bd-e", "bd-e.1", "bd-e.2")]

    with session as opened:
        opened.runner_for(decomposed.issues[0])
        opened.runner_for(decomposed.issues[1])
        assert opened.lock_for("bd-e.1") is not opened.lock_for("bd-e.2")
        assert all(lock.exists() for lock in locks)

    assert not any(lock.exists() for lock in locks)


def test_a_run_without_worker_lanes_opens_none(config: Config, decomposed: FakeTracker) -> None:
    """At `--count 1` there is nothing to merge, so it is ADR 0023 exactly."""
    client = FakeClient()
    session, _ = build(config, tracker=decomposed, script=[], client=client)
    session._runner = None
    session.lane_key = "bd-e"

    with session as opened:
        first = opened.runner_for(decomposed.issues[0])
        second = opened.runner_for(decomposed.issues[1])
        integration = opened.integration_lane()

    assert integration is not None
    assert first.workdir == second.workdir == integration.path
    assert list(client.bases) == ["milhouse/bd-e"]


def test_a_run_with_worker_lanes_names_every_lane_it_left_open(
    config: Config, decomposed: FakeTracker
) -> None:
    """The integration branch first, because that is the one a person reviews."""
    lines: list[str] = []
    session = concurrent(config, decomposed, FakeClient())
    session.report = lines.append

    with session as opened:
        opened.runner_for(decomposed.issues[0])
        opened.runner_for(decomposed.issues[1])

    assert [line for line in lines if "left open" in line] == [
        "lane wL1 is left open (/worktrees/milhouse-bd-e)",
        "lane wL2 is left open (/worktrees/milhouse-bd-e--bd-e.1)",
        "lane wL3 is left open (/worktrees/milhouse-bd-e--bd-e.2)",
    ]


# -- reconciling a run that had worker lanes -----------------------------------


def with_worker_lane(config: Config, issue_id: str) -> FakeClient:
    """A herdr holding the primary checkout and one live worker lane for ``issue_id``."""
    return FakeClient(
        workspaces={"wG": f"milhouse:{config.repo_root.name}", "wL9": issue_id},
        checkouts=[
            Worktree(path=config.repo_root, branch="main", workspace_id="wG"),
            Worktree(
                path=Path("/worktrees/milhouse-bd-e--bd-e.1"),
                branch=f"milhouse/bd-e/{issue_id}",
                workspace_id="wL9",
            ),
        ],
    )


def test_a_claim_whose_worker_lane_is_live_is_left_alone(
    config: Config, decomposed: FakeTracker
) -> None:
    """A worker lane carries the issue id, so an in-flight turn is visible."""
    session = concurrent(config, decomposed, with_worker_lane(config, "bd-e.1"))
    decomposed.issues[0].status = "in_progress"
    session.audit.claimed("bd-e.1")

    with session:
        pass

    assert decomposed.released == []
    assert decomposed.issues[0].status == "in_progress"


def test_a_claim_whose_worker_lane_is_gone_is_reopened(
    config: Config, decomposed: FakeTracker
) -> None:
    """Nobody is working it, and `bd ready` would never offer it again."""
    session = concurrent(config, decomposed, with_worker_lane(config, "bd-e.1"))
    decomposed.issues[0].status = "in_progress"
    decomposed.issues[1].status = "in_progress"
    session.audit.claimed("bd-e.1")
    session.audit.claimed("bd-e.2")

    with session:
        pass

    assert decomposed.released == ["bd-e.2"]
    assert decomposed.issues[0].status == "in_progress"


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


# -- what teardown says it left behind -----------------------------------------


def test_a_failed_step_names_the_lane_it_left_open(config: Config, decomposed: FakeTracker) -> None:
    """Naming the source workspace instead pointed at the checkout it was run from.

    That is where the person reading the line already is, and no agent runs in
    it. The lane is the thing that was left behind, so it is the thing named.
    """
    client = FakeClient()
    lines: list[str] = []
    session, _ = build(config, tracker=decomposed, script=[], client=client)
    session._runner = None
    session.report = lines.append

    with pytest.raises(MilhouseError, match="herdr said no"), session as opened:
        opened.runner_for(decomposed.issues[0])
        raise MilhouseError("herdr said no")

    assert "lane wL1 is left open (/worktrees/milhouse-bd-e.1)" in lines
    # wG is the source workspace, which was open before the step and stays open.
    assert not any("wG is left open" in line for line in lines)


def test_a_step_that_opened_no_lane_leaves_no_lane_open(
    config: Config, decomposed: FakeTracker
) -> None:
    """Nothing ready means no lane, and nothing for a person to go and look at."""
    lines: list[str] = []
    session, _ = build(config, tracker=decomposed, script=[], client=FakeClient())
    session.report = lines.append

    with session:
        pass

    assert not any("left open" in line for line in lines)


def test_a_session_that_opened_several_lanes_names_every_one(
    config: Config, decomposed: FakeTracker
) -> None:
    """What `milhouse dispatch` leaves: a lane per issue, each in its own checkout."""
    lines: list[str] = []
    session, _ = build(config, tracker=decomposed, script=[], client=FakeClient())
    session._runner = None
    session.report = lines.append

    with session as opened:
        opened.runner_for(decomposed.issues[0])
        opened.runner_for(decomposed.issues[1])

    assert [line for line in lines if "left open" in line] == [
        "lane wL1 is left open (/worktrees/milhouse-bd-e.1)",
        "lane wL2 is left open (/worktrees/milhouse-bd-e.2)",
    ]


def test_a_run_s_one_lane_is_named_once_however_many_issues_it_worked(
    config: Config, decomposed: FakeTracker
) -> None:
    """A run works every issue in one checkout (ADR 0023), so there is one place to look."""
    lines: list[str] = []
    session, _ = build(config, tracker=decomposed, script=[], client=FakeClient())
    session._runner = None
    session.lane_key = "bd-e"
    session.report = lines.append

    with session as opened:
        opened.runner_for(decomposed.issues[0])
        opened.runner_for(decomposed.issues[1])

    assert [line for line in lines if "left open" in line] == [
        "lane wL1 is left open (/worktrees/milhouse-bd-e)"
    ]
