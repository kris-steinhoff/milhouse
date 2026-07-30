"""Tests for lane assignment.

The registry is herdr's, so what is actually milhouse's here is the decision:
reuse, stack in the predecessor's lane, or open a new one. That is the whole of
what these check, plus the refusal when the dependency graph gives two answers.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
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

HERDR_AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
"""herdr's agent-name grammar, spelled out here rather than imported.

Asking milhouse what a valid name is would get milhouse's own answer back,
whatever that answer happened to be, which is how an invalid one got this far.
"""


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


def agent_name_in_a_subprocess(key: str, *, hash_seed: str) -> str:
    """What a fresh interpreter, started with ``hash_seed``, calls ``key``'s agent.

    The seed is the whole point. ``PYTHONHASHSEED`` is what makes :func:`hash`
    differ between processes, so pinning it to two values turns a name built from
    a salted digest into a certain failure rather than an occasional one.
    """
    source = (
        "import sys; from pathlib import Path; from milhouse.lanes import Lane; "
        "print(Lane(key=sys.argv[1], path=Path('.'), branch='b', workspace_id='w').agent_name)"
    )
    finished = subprocess.run(
        [sys.executable, "-c", source, key],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
    )
    return finished.stdout.strip()


def test_a_bead_id_s_dot_becomes_an_underscore(lanes: Lanes) -> None:
    """A dot is outside herdr's character set, and `_` carries it without collapsing it."""
    lane = open_lane(lanes, issue("milhouse-6or.4"))

    assert lane.agent_name == "milhouse-milhouse-6or_4"


def test_the_agent_name_drops_anything_odd(lanes: Lanes) -> None:
    lane = open_lane(lanes, issue("bd e/1"))

    assert lane.agent_name == "milhouse-bd-e-1"


@pytest.mark.parametrize(
    "key",
    [
        "bd-e",
        "milhouse-6or.4",
        "MILHOUSE-6OR.4",
        "bd e/1",
        "excalidraw-desktop-xyz.4",
        "a-repository-with-a-very-long-prefix-9zz.12",
    ],
    ids=["plain", "dotted", "uppercase", "space-and-slash", "just-over", "far-over"],
)
def test_every_key_produces_a_name_herdr_would_accept(lanes: Lanes, key: str) -> None:
    """The name goes to `herdr agent start`, which enforces this and nothing less."""
    lane = open_lane(lanes, issue(key))

    assert HERDR_AGENT_NAME.fullmatch(lane.agent_name), lane.agent_name


def test_a_dot_and_a_dash_are_not_the_same_agent(lanes: Lanes) -> None:
    """Collapsing both to `-` would have milhouse drive somebody else's agent."""
    dotted = open_lane(lanes, issue("bd-e.2"))
    dashed = open_lane(lanes, issue("bd-e-2"))

    assert dotted.agent_name != dashed.agent_name


def test_a_shortened_name_still_says_which_key_it_is_for(lanes: Lanes) -> None:
    """Whoever reads `herdr agent list` has to recognise the lane in the name."""
    lane = open_lane(lanes, issue("excalidraw-desktop-xyz.4"))

    assert lane.agent_name.startswith("milhouse-excalidraw-deskt-")
    assert len(lane.agent_name) == 32


def test_two_keys_that_shorten_to_the_same_stem_get_different_names(lanes: Lanes) -> None:
    """Truncation alone collides, so the digest covers the whole key."""
    first = open_lane(lanes, issue("excalidraw-desktop-xyz.4"))
    second = open_lane(lanes, issue("excalidraw-desktop-xyz.5"))

    assert first.agent_name != second.agent_name


def test_the_agent_name_is_the_same_in_another_process(lanes: Lanes) -> None:
    """`milhouse reap` recomputes it elsewhere, so a salted `hash()` will not do."""
    key = "excalidraw-desktop-xyz.4"
    here = open_lane(lanes, issue(key)).agent_name

    elsewhere = {agent_name_in_a_subprocess(key, hash_seed=seed) for seed in ("1", "2")}

    assert elsewhere == {here}


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


def test_locating_a_lane_does_not_choose_a_pane(lanes: Lanes, client: FakeClient) -> None:
    """Choosing one can create one, which is wrong for a read-only lookup."""
    lane = open_lane(lanes, issue("bd-e.1"))
    client.avoided = "not asked"

    located = lanes.locate("bd-e.1")

    assert located is not None
    assert located[0].path == lane.path
    assert located[0].pane_id == ""
    assert client.avoided == "not asked"


# -- a run's lane --------------------------------------------------------------


def open_for(lanes: Lanes, key: str) -> Any:
    return lanes.open_for(key, source_workspace=SOURCE, base="main")


def test_a_run_gets_one_lane_named_after_its_target(lanes: Lanes, client: FakeClient) -> None:
    lane = open_for(lanes, "bd-e")

    assert lane.key == "bd-e"
    assert lane.branch == "milhouse/bd-e"
    assert client.workspaces[lane.workspace_id] == "bd-e"


def test_every_issue_in_a_run_lands_in_that_one_lane(lanes: Lanes) -> None:
    first = open_for(lanes, "bd-e")

    second = open_for(lanes, "bd-e")

    assert (second.path, second.branch, second.workspace_id) == (
        first.path,
        first.branch,
        first.workspace_id,
    )


def test_a_run_ignores_the_dependency_rules_that_dispatch_follows(lanes: Lanes) -> None:
    """One base branch by construction, so a join has nothing to choose between."""
    dispatched = open_lane(lanes, issue("bd-e.1"))

    run_lane = open_for(lanes, "bd-e")

    assert run_lane.branch == "milhouse/bd-e"
    assert run_lane.path != dispatched.path


def test_dispatch_still_gets_a_lane_per_issue(lanes: Lanes) -> None:
    """ADR 0023 amends how a run picks a lane, and nothing about dispatch."""
    first = open_lane(lanes, issue("bd-e.1"))
    second = open_lane(lanes, issue("bd-e.2"))

    assert first.branch == "milhouse/bd-e.1"
    assert second.branch == "milhouse/bd-e.2"


def test_a_second_run_of_the_same_target_resumes_its_branch(lanes: Lanes) -> None:
    """Which is what makes re-running a target pick the work back up."""
    first = open_for(lanes, "bd-e")

    resumed = open_for(lanes, "bd-e")

    assert resumed.branch == first.branch
    assert resumed.path == first.path
