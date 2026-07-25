"""Tests for the ``bd`` wrapper, against recorded JSON and a real database."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from milhouse.errors import TrackerError
from milhouse.models import TaskDefinition
from milhouse.tracker import BeadsTracker

from .fakes import FakeProc, Reply

EPIC_JSON = json.dumps(
    {
        "id": "bd-4rt",
        "title": "Add a hello command",
        "status": "open",
        "issue_type": "epic",
        "labels": ["milhouse"],
        "metadata": {"milhouse_task": "file:docs/tasks/hello.md"},
        "priority": 2,
    }
)

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


@dataclass
class Planned:
    """Stands in for a planner's PlanIssue, structurally."""

    key: str
    title: str
    type: str = "task"
    priority: int | None = None
    description: str = ""
    acceptance: str = ""
    blocked_by: list[str] = field(default_factory=list)


@pytest.fixture
def task() -> TaskDefinition:
    return TaskDefinition(
        task_id="file:docs/tasks/hello.md",
        title="Add a hello command",
        body="body",
        kind="file",
        slug="hello",
    )


@pytest.fixture
def tracker(repo: Path) -> BeadsTracker:
    return BeadsTracker(repo)


def test_find_epic_queries_by_metadata(
    tracker: BeadsTracker, task: TaskDefinition, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout=f"[{EPIC_JSON}]"))

    epic = tracker.find_epic(task)

    assert epic is not None
    assert epic.id == "bd-4rt"
    argv = fake_proc.calls[0]
    assert "--metadata-field" in argv
    assert "milhouse_task=file:docs/tasks/hello.md" in argv
    assert argv[argv.index("--type") + 1] == "epic"


def test_find_epic_returns_none_when_undecomposed(
    tracker: BeadsTracker, task: TaskDefinition, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    assert tracker.find_epic(task) is None


def test_create_epic_sets_the_label_and_metadata(
    tracker: BeadsTracker, task: TaskDefinition, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout=EPIC_JSON))

    epic = tracker.create_epic(task)

    assert epic.id == "bd-4rt"
    argv = fake_proc.calls[0]
    assert argv[argv.index("--labels") + 1] == "milhouse"
    assert json.loads(argv[argv.index("--metadata") + 1]) == {
        "milhouse_task": "file:docs/tasks/hello.md"
    }
    assert "--external-ref" not in argv


def test_create_epic_carries_a_github_external_ref(
    tracker: BeadsTracker, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout=EPIC_JSON))
    task = TaskDefinition(
        task_id="gh:o/r#7",
        title="t",
        body="b",
        kind="github",
        slug="gh-7",
        external_ref="gh-7",
    )

    tracker.create_epic(task)

    argv = fake_proc.calls[0]
    assert argv[argv.index("--external-ref") + 1] == "gh-7"


def test_ready_claims_under_the_epic(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))

    issue = tracker.ready("bd-4rt", claim=True)

    assert issue is not None
    assert issue.id == "bd-4rt.1"
    assert issue.status == "in_progress"
    argv = fake_proc.calls[0]
    assert argv[argv.index("--parent") + 1] == "bd-4rt"
    assert "--claim" in argv
    assert argv[argv.index("--limit") + 1] == "1"


def test_ready_without_claim_omits_the_flag(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=CHILD_JSON))

    tracker.ready("bd-4rt", claim=False)

    assert "--claim" not in fake_proc.calls[0]


def test_an_empty_ready_result_means_the_epic_is_done(
    tracker: BeadsTracker, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    assert tracker.ready("bd-4rt", claim=True) is None


def test_children_are_created_then_wired(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    ids = iter(["bd-4rt.1", "bd-4rt.2"])
    fake_proc.expect(
        "bd -C",
        lambda argv: Reply(
            stdout=json.dumps({"id": next(ids), "title": "t", "status": "open"})
            if "create" in argv
            else ""
        ),
    )

    created = tracker.create_children(
        "bd-4rt",
        [
            Planned(key="a", title="Add it", priority=1, description="d", acceptance="ac"),
            Planned(key="b", title="Document it", blocked_by=["a"]),
        ],
    )

    assert [issue.id for issue in created] == ["bd-4rt.1", "bd-4rt.2"]
    creates = [call for call in fake_proc.calls if "create" in call]
    assert creates[0][creates[0].index("--parent") + 1] == "bd-4rt"
    assert creates[0][creates[0].index("--priority") + 1] == "1"
    assert creates[0][creates[0].index("--acceptance") + 1] == "ac"
    # Dependencies are wired only after every issue exists.
    deps = [call for call in fake_proc.calls if "dep" in call]
    assert deps == [("bd", "-C", str(tracker.repo_root), "dep", "add", "bd-4rt.2", "bd-4rt.1")]


def test_an_unknown_blocker_key_is_rejected(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=json.dumps({"id": "bd-1", "title": "t", "status": "open"})))

    with pytest.raises(TrackerError, match="unknown key 'ghost'"):
        tracker.create_children("bd-4rt", [Planned(key="a", title="t", blocked_by=["ghost"])])


def test_release_reopens_and_unassigns(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=""))

    tracker.release("bd-4rt.1", note="interrupted")

    assert fake_proc.ran("bd", "-C", str(tracker.repo_root), "note", "bd-4rt.1", "interrupted")
    update = next(call for call in fake_proc.calls if "update" in call)
    assert update[update.index("--status") + 1] == "open"
    assert update[update.index("--assignee") + 1] == ""


def test_block_sets_the_blocked_status(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=""))

    tracker.block("bd-4rt.1", "three failed attempts")

    update = next(call for call in fake_proc.calls if "update" in call)
    assert update[update.index("--status") + 1] == "blocked"


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


@pytest.mark.beads
def test_full_lifecycle_against_a_real_database(tmp_path: Path, task: TaskDefinition) -> None:
    """Create, claim, note, release, and close, using the real `bd`.

    This is what keeps the recorded JSON in the unit tests honest. Skipped when
    `bd` is not installed.
    """
    if shutil.which("bd") is None:
        pytest.skip("bd is not installed")
    root = tmp_path / "scratch"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    # `bd -C` refuses to run without an existing project, so init runs via cwd.
    subprocess.run(["bd", "init"], cwd=root, check=True, capture_output=True)

    tracker = BeadsTracker(root)
    assert tracker.find_epic(task) is None

    epic = tracker.create_epic(task)
    assert epic.issue_type == "epic"
    found = tracker.find_epic(task)
    assert found is not None
    assert found.id == epic.id

    children = tracker.create_children(
        epic.id,
        [
            Planned(key="a", title="Add it", description="do the thing"),
            Planned(key="b", title="Document it", blocked_by=["a"]),
        ],
    )
    assert len(children) == 2
    assert {issue.parent for issue in children} == {epic.id}

    # Only the unblocked issue is ready, and claiming it marks it in progress.
    claimed = tracker.ready(epic.id, claim=True)
    assert claimed is not None
    assert claimed.id == children[0].id
    assert claimed.status == "in_progress"
    assert tracker.ready(epic.id, claim=True) is None

    tracker.release(claimed.id, note="interrupted run")
    released = tracker.get(claimed.id)
    assert released.status == "open"
    assert not released.assignee

    tracker.close(claimed.id)
    assert tracker.get(claimed.id).is_closed
    # Closing the blocker makes its dependent ready.
    assert tracker.ready(epic.id, claim=False) is not None
    assert len(tracker.children(epic.id)) == 2
