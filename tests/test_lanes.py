"""Tests for lane assignment.

The registry is herdr's, so what is actually milhouse's here is the decision:
reuse, stack in the predecessor's lane, or open a new one. That is the whole of
what these check, plus the refusal when the dependency graph gives two answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from milhouse.config import Config
from milhouse.errors import MilhouseError
from milhouse.herdr import Worktree
from milhouse.lanes import Lanes
from milhouse.models import Issue

from .doubles import FakeClient

SOURCE = "wG"


def issue(issue_id: str, *, blocked_by: list[str] | None = None) -> Issue:
    """An issue carrying the relations `bd show` would return for it."""
    relations: list[dict[str, Any]] = [
        {"id": blocker, "dependency_type": "blocks"} for blocker in blocked_by or []
    ]
    relations.append({"id": "bd-e", "dependency_type": "parent-child"})
    return Issue(
        id=issue_id,
        title=issue_id,
        status="in_progress",
        parent="bd-e",
        raw={"dependencies": relations},
    )


@pytest.fixture
def client(config: Config) -> FakeClient:
    """A herdr holding only the primary checkout, in milhouse's own workspace."""
    return FakeClient(
        workspaces={SOURCE: f"milhouse:{config.repo_root.name}"},
        checkouts=[Worktree(path=config.repo_root, branch="main", workspace_id=SOURCE)],
    )


@pytest.fixture
def lanes(client: FakeClient, config: Config) -> Lanes:
    return Lanes(client, config)  # ty: ignore[invalid-argument-type]


def open_lane(lanes: Lanes, subject: Issue) -> Any:
    return lanes.open(subject, source_workspace=SOURCE, base="main")


# -- opening a lane ------------------------------------------------------------


def test_an_independent_issue_gets_a_worktree_of_its_own(lanes: Lanes, client: FakeClient) -> None:
    lane = open_lane(lanes, issue("bd-e.1"))

    assert lane.branch == "milhouse/bd-e.1"
    assert lane.workspace_id != SOURCE
    assert client.workspaces[lane.workspace_id] == "bd-e.1"


def test_the_branch_prefix_is_configurable(client: FakeClient, config: Config) -> None:
    config.lane.branch_prefix = "lane/"

    lane = open_lane(Lanes(client, config), issue("bd-e.1"))  # ty: ignore[invalid-argument-type]

    assert lane.branch == "lane/bd-e.1"


def test_two_independent_issues_get_separate_lanes(lanes: Lanes) -> None:
    first = open_lane(lanes, issue("bd-e.1"))
    second = open_lane(lanes, issue("bd-e.2"))

    assert first.workspace_id != second.workspace_id
    assert first.path != second.path


def test_a_lane_is_found_again_by_its_label(lanes: Lanes) -> None:
    """A second attempt has to land on the branch the first one committed to."""
    first = open_lane(lanes, issue("bd-e.1"))

    again = open_lane(lanes, issue("bd-e.1"))

    assert again.path == first.path
    assert again.branch == first.branch
    assert again.workspace_id == first.workspace_id


def test_find_says_nothing_about_an_issue_with_no_lane(lanes: Lanes) -> None:
    assert lanes.find("bd-e.9") is None


# -- stacking ------------------------------------------------------------------


def test_a_dependent_issue_continues_in_its_blocker_s_lane(
    lanes: Lanes, client: FakeClient
) -> None:
    """A new tab on the same branch, not a worktree branched from it."""
    first = open_lane(lanes, issue("bd-e.1"))

    second = open_lane(lanes, issue("bd-e.2", blocked_by=["bd-e.1"]))

    assert second.path == first.path
    assert second.branch == first.branch
    assert second.workspace_id == first.workspace_id
    assert second.pane_id != first.pane_id
    assert "bd-e.2" in client.tab_labels[first.workspace_id].values()


def test_a_stacked_issue_is_found_again_by_its_tab_label(lanes: Lanes) -> None:
    open_lane(lanes, issue("bd-e.1"))
    stacked = open_lane(lanes, issue("bd-e.2", blocked_by=["bd-e.1"]))

    found = lanes.find("bd-e.2")

    assert found is not None
    assert found.path == stacked.path
    assert found.branch == stacked.branch


def test_a_blocker_with_no_lane_does_not_stack(lanes: Lanes) -> None:
    """A blocker worked before lanes existed, or in a lane since closed."""
    lane = open_lane(lanes, issue("bd-e.2", blocked_by=["bd-e.1"]))

    assert lane.branch == "milhouse/bd-e.2"


def test_the_parent_epic_is_not_a_blocker(lanes: Lanes) -> None:
    """`bd show` puts every relation in one array; only `blocks` orders work."""
    epic_lane = open_lane(lanes, issue("bd-e"))
    lane = open_lane(lanes, issue("bd-e.1"))

    assert lane.workspace_id != epic_lane.workspace_id


def test_two_blockers_in_separate_lanes_is_refused(lanes: Lanes) -> None:
    """Two candidate base branches and no rule picking between them (ADR 0020)."""
    open_lane(lanes, issue("bd-e.1"))
    open_lane(lanes, issue("bd-e.2"))

    with pytest.raises(MilhouseError, match="more than one lane"):
        open_lane(lanes, issue("bd-e.3", blocked_by=["bd-e.1", "bd-e.2"]))


# -- resuming ------------------------------------------------------------------


def test_a_lane_whose_workspace_was_closed_is_reopened(lanes: Lanes, client: FakeClient) -> None:
    """Closing a workspace leaves the checkout and its branch alone."""
    first = open_lane(lanes, issue("bd-e.1"))
    del client.workspaces[first.workspace_id]
    client.checkouts = [
        Worktree(path=item.path, branch=item.branch, workspace_id="")
        if item.path == first.path
        else item
        for item in client.checkouts
    ]

    again = open_lane(lanes, issue("bd-e.1"))

    assert again.path == first.path
    assert again.branch == first.branch
    assert client.workspaces[again.workspace_id] == "bd-e.1"


# -- keeping git out of it -----------------------------------------------------


def test_a_lane_outside_the_repository_needs_no_ignoring(lanes: Lanes, config: Config) -> None:
    """The normal case: herdr checks linked worktrees out under ~/.herdr."""
    open_lane(lanes, issue("bd-e.1"))

    assert not (config.repo_root / ".git" / "info" / "exclude").exists()


def test_a_lane_inside_the_repository_is_excluded_locally(
    client: FakeClient, config: Config
) -> None:
    """Untracked lane files would read as a dirty tree in every other lane."""
    inside = config.repo_root / ".lanes" / "bd-e.1"

    def create_inside(**kwargs: Any) -> Worktree:
        made = Worktree(path=inside, branch=kwargs["branch"], workspace_id="wL9", pane_id="wL9:p1")
        client.workspaces["wL9"] = kwargs["label"]
        client.checkouts.append(made)
        return made

    client.create_worktree = create_inside  # ty: ignore[invalid-assignment]

    open_lane(Lanes(client, config), issue("bd-e.1"))  # ty: ignore[invalid-argument-type]

    exclude = config.repo_root / ".git" / "info" / "exclude"
    assert "/.lanes/bd-e.1/" in exclude.read_text(encoding="utf-8").splitlines()


def test_the_exclude_entry_is_written_once(client: FakeClient, config: Config) -> None:
    inside = config.repo_root / ".lanes" / "bd-e.1"
    exclude = config.repo_root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True)
    exclude.write_text("# git ls-files --others --exclude-from=.git/info/exclude\n", "utf-8")

    def create_inside(**kwargs: Any) -> Worktree:
        return Worktree(path=inside, branch=kwargs["branch"], workspace_id="wL9", pane_id="wL9:p1")

    client.create_worktree = create_inside  # ty: ignore[invalid-assignment]
    lanes = Lanes(client, config)  # ty: ignore[invalid-argument-type]

    open_lane(lanes, issue("bd-e.1"))
    open_lane(lanes, issue("bd-e.1"))

    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines.count("/.lanes/bd-e.1/") == 1


# -- the agent name ------------------------------------------------------------


def test_the_agent_name_survives_a_bead_id(lanes: Lanes) -> None:
    """Bead ids carry dots, and the name is handed to `herdr agent start`."""
    lane = open_lane(lanes, issue("milhouse-6or.4"))

    assert lane.agent_name == "milhouse-milhouse-6or.4"


def test_the_agent_name_drops_anything_odd(lanes: Lanes) -> None:
    lane = open_lane(lanes, issue("bd e/1"))

    assert lane.agent_name == "milhouse-bd-e-1"


def test_a_pane_the_caller_owns_is_never_taken(lanes: Lanes, config: Config) -> None:
    """Reusing a lane picks a pane, and the caller's own is not a candidate."""
    lane = open_lane(lanes, issue("bd-e.1"))
    config.herdr.self_pane = f"{lane.workspace_id}:p1"

    again = lanes.find("bd-e.1")

    assert again is not None
    assert again.pane_id != config.herdr.self_pane


def test_a_lane_path_is_left_to_herdr(lanes: Lanes, config: Config) -> None:
    lane = open_lane(lanes, issue("bd-e.1"))

    assert not lane.path.is_relative_to(config.repo_root)
    assert isinstance(lane.path, Path)
