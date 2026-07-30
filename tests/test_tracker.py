"""Tests for the ``bd`` wrapper, against recorded JSON and a real database."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from milhouse.config import TrackerConfig
from milhouse.errors import TrackerError
from milhouse.tracker import BeadsTracker

from .fakes import FakeProc, Reply

CHILD_JSON = json.dumps(
    [
        {
            "id": "bd-4rt.1",
            "title": "Add the subcommand",
            "status": "in_progress",
            "issue_type": "task",
            "assignee": "Kris Steinhoff",
            "parent": "bd-4rt",
            "priority": 1,
            "labels": ["milhouse"],
        }
    ]
)


SCOPE_JSON = json.dumps(
    [
        {"id": "bd-4rt.1", "title": "Add it", "status": "open", "issue_type": "task"},
        {"id": "bd-4rt.2", "title": "Document it", "status": "open", "issue_type": "task"},
        {"id": "bd-4rt.3", "title": "Announce it", "status": "open", "issue_type": "task"},
    ]
)

DEP_JSON = json.dumps(
    [
        # `bd dep list --type blocks` answers with relations, not issues, and
        # names the blocker in `depends_on_id`.
        {"issue_id": "bd-4rt.2", "depends_on_id": "bd-4rt.1", "type": "blocks"},
        {"issue_id": "bd-4rt.3", "depends_on_id": "bd-4rt.2", "type": "blocks"},
        {"issue_id": "bd-4rt.1", "depends_on_id": "bd-elsewhere", "type": "blocks"},
    ]
)


@pytest.fixture
def tracker(repo: Path) -> BeadsTracker:
    return BeadsTracker(repo)


@pytest.fixture
def scope(repo: Path, fake_proc: FakeProc) -> FakeProc:
    """Reply to the two calls `graph()` makes: the listing and the relations."""
    fake_proc.expect(["bd", "-C", str(repo), "list"], SCOPE_JSON)
    fake_proc.expect(["bd", "-C", str(repo), "dep", "list"], DEP_JSON)
    return fake_proc


def test_ready_claims_the_next_issue(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))

    issue = tracker.ready(claim=True)

    assert issue is not None
    assert issue.id == "bd-4rt.1"
    assert issue.status == "in_progress"
    argv = fake_proc.calls[0]
    assert "--claim" in argv
    assert argv[argv.index("--limit") + 1] == "1"


def test_ready_is_unfenced_by_default(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    """A repository whose beads database is only agent work needs no fence."""
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))

    tracker.ready(claim=True)

    argv = fake_proc.calls[0]
    assert "--parent" not in argv
    assert "--label" not in argv


def test_ready_never_offers_an_epic(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    """An epic is a container for work, not a unit of it."""
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))

    tracker.ready(claim=True)

    argv = fake_proc.calls[0]
    assert argv[argv.index("--exclude-type") + 1] == "epic"


def test_a_configured_fence_is_passed_to_bd(repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))
    tracker = BeadsTracker(repo, TrackerConfig(parent="bd-4rt", label="agent"))

    tracker.ready(claim=True)

    argv = fake_proc.calls[0]
    assert argv[argv.index("--parent") + 1] == "bd-4rt"
    assert argv[argv.index("--label") + 1] == "agent"


def test_ready_without_claim_omits_the_flag(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))

    tracker.ready(claim=False)

    assert "--claim" not in fake_proc.calls[0]


def test_an_empty_ready_result_means_nothing_is_ready(
    tracker: BeadsTracker, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    assert tracker.ready(claim=True) is None


def test_children_lists_everything_in_scope(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))

    tracker.children()

    argv = fake_proc.calls[0]
    assert "--all" in argv
    assert argv[argv.index("--limit") + 1] == "0"
    assert "--parent" not in argv


def test_children_of_an_epic_are_asked_for_by_parent(
    tracker: BeadsTracker, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))

    tracker.children("bd-4rt")

    argv = fake_proc.calls[0]
    assert argv[argv.index("--parent") + 1] == "bd-4rt"


# -- the dependency graph ------------------------------------------------------


def test_graph_of_an_epic_scope_pairs_the_children_with_their_edges(
    repo: Path, scope: FakeProc
) -> None:
    graph = BeadsTracker(repo, TrackerConfig(parent="bd-4rt")).graph()

    assert list(graph.nodes) == ["bd-4rt.1", "bd-4rt.2", "bd-4rt.3"]
    assert graph.edges == [("bd-4rt.1", "bd-4rt.2"), ("bd-4rt.2", "bd-4rt.3")]
    assert graph.waves() == [["bd-4rt.1"], ["bd-4rt.2"], ["bd-4rt.3"]]
    listing = next(scope.commands("bd", "-C", str(repo), "list"))
    assert listing[listing.index("--parent") + 1] == "bd-4rt"


def test_the_edges_are_one_call_asking_only_for_blocks(repo: Path, scope: FakeProc) -> None:
    """Every id at once, and `--type` decides the shape of the answer as well."""
    BeadsTracker(repo).graph()

    calls = list(scope.commands("bd", "-C", str(repo), "dep", "list"))
    assert len(calls) == 1
    assert calls[0][calls[0].index("--type") + 1] == "blocks"
    assert set(calls[0]) >= {"bd-4rt.1", "bd-4rt.2", "bd-4rt.3"}


def test_graph_of_a_closure_scope_keeps_only_its_members(repo: Path, scope: FakeProc) -> None:
    """The membership fences the nodes, and an edge to anything else is dropped."""
    tracker = BeadsTracker(repo, members={"bd-4rt.1", "bd-4rt.2"})

    graph = tracker.graph()

    assert list(graph.nodes) == ["bd-4rt.1", "bd-4rt.2"]
    # `bd-4rt.3` is out of scope, and so is the blocker `bd-4rt.1` carries: the
    # graph cannot say whether a node it does not hold is closed.
    assert graph.edges == [("bd-4rt.1", "bd-4rt.2")]
    assert [issue.id for issue in graph.frontier()] == ["bd-4rt.1"]


def test_an_empty_scope_asks_bd_for_no_edges(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    assert tracker.graph().nodes == {}
    assert not fake_proc.ran("bd", "-C", str(tracker.repo_root), "dep")


def test_unexpected_dep_output_is_rejected(repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect(["bd", "-C", str(repo), "list"], SCOPE_JSON)
    fake_proc.expect(["bd", "-C", str(repo), "dep", "list"], '["not an object"]')

    with pytest.raises(TrackerError, match="unexpected bd output"):
        BeadsTracker(repo).graph()


def test_release_reopens_and_unassigns(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=""))

    tracker.release("bd-4rt.1", note="interrupted")

    assert fake_proc.ran("bd", "-C", str(tracker.repo_root), "note", "bd-4rt.1", "interrupted")
    update = next(call for call in fake_proc.calls if "update" in call)
    assert update[update.index("--status") + 1] == "open"
    assert update[update.index("--assignee") + 1] == ""


def test_defer_sets_an_issue_aside_with_a_reason(
    tracker: BeadsTracker, fake_proc: FakeProc
) -> None:
    """`bd defer` hides it from `bd ready` and leaves it in `bd list`."""
    fake_proc.expect("bd", Reply(stdout=""))

    tracker.defer("bd-4rt.1", reason="3 attempts, still stalling")

    assert fake_proc.ran(
        "bd",
        "-C",
        str(tracker.repo_root),
        "defer",
        "bd-4rt.1",
        "--reason=3 attempts, still stalling",
    )


def test_get_raises_for_a_missing_issue(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    with pytest.raises(TrackerError, match="no such issue"):
        tracker.get("bd-nope")


def test_a_bd_failure_becomes_a_tracker_error(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stderr="no beads database found\n", returncode=1))

    with pytest.raises(TrackerError) as caught:
        tracker.get("bd-1")

    assert caught.value.exit_code == 4


def test_unexpected_bd_output_is_rejected(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout='["not an object"]'))

    with pytest.raises(TrackerError, match="unexpected bd output"):
        tracker.get("bd-1")


# -- against a real bd database ------------------------------------------------


def scratch(tmp_path: Path) -> Path:
    """A git repository with an empty beads database, or a skip.

    These tests are what keeps the recorded JSON in the unit tests honest.
    Skipped when `bd` is not installed.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd is not installed")
    root = tmp_path / "scratch"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    # `bd -C` refuses to run without an existing project, so init runs via cwd.
    subprocess.run(["bd", "init"], cwd=root, check=True, capture_output=True)
    return root


def make(root: Path, title: str, *extra: str) -> str:
    """Create one issue with the real `bd` and return its id."""
    result = subprocess.run(
        ["bd", "-C", str(root), "create", title, "--json", *extra],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(json.loads(result.stdout)["id"])


def depends(root: Path, issue_id: str, blocker_id: str) -> None:
    """Record that `issue_id` is blocked by `blocker_id`."""
    subprocess.run(["bd", "-C", str(root), "dep", "add", issue_id, blocker_id], check=True)


def bd_ready(root: Path) -> list[str]:
    """The ids `bd ready` itself offers, which is what `frontier()` must match."""
    result = subprocess.run(
        ["bd", "-C", str(root), "ready", "--limit", "0", "--exclude-type", "epic", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(str(item["id"]) for item in json.loads(result.stdout.strip() or "[]") or [])


@pytest.mark.beads
def test_full_lifecycle_against_a_real_database(tmp_path: Path) -> None:
    """Claim, note, release, and close, using the real `bd`."""
    root = scratch(tmp_path)
    epic = make(root, "Add a hello command", "--type", "epic")
    first = make(root, "Add it", "--parent", epic, "--description", "do the thing")
    second = make(root, "Document it", "--parent", epic)
    depends(root, second, first)

    tracker = BeadsTracker(root)

    # Only the unblocked issue is ready, the epic is never offered, and claiming
    # it marks it in progress.
    claimed = tracker.ready(claim=True)
    assert claimed is not None
    assert claimed.id == first
    assert claimed.status == "in_progress"
    assert tracker.ready(claim=True) is None

    tracker.release(claimed.id, note="interrupted run")
    released = tracker.get(claimed.id)
    assert released.status == "open"
    assert not released.assignee

    tracker.close(claimed.id)
    assert tracker.get(claimed.id).is_closed
    # Closing the blocker makes its dependent ready.
    assert tracker.ready(claim=False) is not None
    assert len(tracker.children(epic)) == 2


@pytest.mark.beads
def test_the_graph_matches_a_real_database(tmp_path: Path) -> None:
    """A diamond in real `bd`, levelled, with `frontier()` held against `bd ready`."""
    root = scratch(tmp_path)
    epic = make(root, "Add a hello command", "--type", "epic")
    first = make(root, "Add it", "--parent", epic)
    second = make(root, "Document it", "--parent", epic)
    third = make(root, "Announce it", "--parent", epic)
    fourth = make(root, "Release it", "--parent", epic)
    depends(root, second, first)
    depends(root, third, first)
    depends(root, fourth, second)
    depends(root, fourth, third)

    tracker = BeadsTracker(root, TrackerConfig(parent=epic))
    graph = tracker.graph()

    # The epic itself is not among its own children, so every node is work.
    assert set(graph.nodes) == {first, second, third, fourth}
    assert sorted(graph.edges) == sorted(
        [(first, second), (first, third), (second, fourth), (third, fourth)]
    )
    assert graph.waves() == [[first], [second, third], [fourth]]
    assert graph.width == 2
    assert sorted(graph.blocked_behind(first)) == sorted([second, third, fourth])

    # What `bd ready` offers is the frontier, before and after a blocker closes.
    assert bd_ready(root) == [first]
    assert sorted(issue.id for issue in graph.frontier()) == bd_ready(root)
    tracker.close(first)
    assert bd_ready(root) == sorted([second, third])
    assert sorted(issue.id for issue in tracker.graph().frontier()) == bd_ready(root)

    # An unfenced tracker holds the epic as a node, and still does not offer it.
    whole = BeadsTracker(root).graph()
    assert epic in whole.nodes
    assert sorted(issue.id for issue in whole.frontier()) == bd_ready(root)

    # A closure-scoped tracker keeps its members, and the edges between them.
    closure = BeadsTracker(root, members={second, fourth}).graph()
    assert set(closure.nodes) == {second, fourth}
    assert closure.edges == [(second, fourth)]
