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


@pytest.fixture
def tracker(repo: Path) -> BeadsTracker:
    return BeadsTracker(repo)


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


def test_release_reopens_and_unassigns(tracker: BeadsTracker, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=""))

    tracker.release("bd-4rt.1", note="interrupted")

    assert fake_proc.ran("bd", "-C", str(tracker.repo_root), "note", "bd-4rt.1", "interrupted")
    update = next(call for call in fake_proc.calls if "update" in call)
    assert update[update.index("--status") + 1] == "open"
    assert update[update.index("--assignee") + 1] == ""


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
def test_full_lifecycle_against_a_real_database(tmp_path: Path) -> None:
    """Claim, note, release, and close, using the real `bd`.

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

    def create(title: str, *extra: str) -> str:
        result = subprocess.run(
            ["bd", "-C", str(root), "create", title, "--json", *extra],
            check=True,
            capture_output=True,
            text=True,
        )
        return str(json.loads(result.stdout)["id"])

    epic = create("Add a hello command", "--type", "epic")
    first = create("Add it", "--parent", epic, "--description", "do the thing")
    second = create("Document it", "--parent", epic)
    subprocess.run(["bd", "-C", str(root), "dep", "add", second, first], check=True)

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
