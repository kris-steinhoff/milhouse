"""Tests for resolving a run's target into a fenced tracker.

Faked at the :mod:`milhouse.proc` boundary, so the argv each kind of scope
builds is what is under test alongside the decision about which one to build.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from milhouse.config import TrackerConfig
from milhouse.errors import MilhouseError, TrackerError
from milhouse.scope import resolve

from .fakes import FakeProc, Reply


def bead(issue_id: str, **fields: Any) -> dict[str, Any]:
    """One bead as ``bd`` reports it, with sensible defaults."""
    return {
        "id": issue_id,
        "title": f"Do {issue_id}",
        "status": "open",
        "issue_type": "task",
        **fields,
    }


def blocked_by(*issue_ids: str) -> dict[str, Any]:
    """The ``dependencies`` array `bd show` returns for those blockers.

    A parent relation rides in the same array, so one is always included: only
    a `blocks` relation is an ordering constraint.
    """
    relations = [{"id": issue_id, "dependency_type": "blocks"} for issue_id in issue_ids]
    return {"dependencies": [*relations, {"id": "bd-e", "dependency_type": "parent-child"}]}


@pytest.fixture
def shown(repo: Path, fake_proc: FakeProc) -> Any:
    """Register a `bd show` reply per bead, keyed on the issue id."""

    def register(*beads: dict[str, Any]) -> None:
        fake_proc.expect("bd", Reply(stdout="[]"))
        for item in beads:
            fake_proc.expect(["bd", "-C", str(repo), "show", item["id"]], json.dumps([item]))

    return register


# -- an epic target ------------------------------------------------------------


def test_an_epic_is_scoped_by_parent(repo: Path, fake_proc: FakeProc, shown: Any) -> None:
    """`bd` can fence this one itself, so milhouse never enumerates it."""
    shown(bead("bd-e", issue_type="epic"))

    scope = resolve("bd-e", repo_root=repo)

    assert scope.is_epic
    assert scope.members == ()
    assert scope.tracker.config.parent == "bd-e"
    assert scope.tracker.members is None
    assert scope.describe() == "every ready issue under bd-e"


def test_the_target_replaces_a_standing_parent_but_keeps_a_label(
    repo: Path, fake_proc: FakeProc, shown: Any
) -> None:
    """A label says what was ever meant for an agent; a parent says which work."""
    shown(bead("bd-e", issue_type="epic"))

    scope = resolve("bd-e", repo_root=repo, config=TrackerConfig(parent="bd-other", label="agent"))

    assert scope.tracker.config.parent == "bd-e"
    assert scope.tracker.config.label == "agent"


def test_an_epic_scope_asks_bd_for_its_descendants(
    repo: Path, fake_proc: FakeProc, shown: Any
) -> None:
    shown(bead("bd-e", issue_type="epic"))
    scope = resolve("bd-e", repo_root=repo)

    scope.tracker.ready(claim=False)

    ready = next(call for call in fake_proc.calls if "ready" in call)
    assert ready[ready.index("--parent") + 1] == "bd-e"


# -- a leaf target -------------------------------------------------------------


def test_a_leaf_with_no_blockers_is_a_scope_of_one(
    repo: Path, fake_proc: FakeProc, shown: Any
) -> None:
    shown(bead("bd-1"))

    scope = resolve("bd-1", repo_root=repo)

    assert not scope.is_epic
    assert scope.members == ("bd-1",)
    assert scope.tracker.members == frozenset({"bd-1"})
    assert scope.describe() == "bd-1 alone"


def test_a_chain_of_blockers_is_walked_deepest_first(
    repo: Path, fake_proc: FakeProc, shown: Any
) -> None:
    """The target cannot close until they do, so they are the work as well."""
    shown(
        bead("bd-3", **blocked_by("bd-2")),
        bead("bd-2", **blocked_by("bd-1")),
        bead("bd-1"),
    )

    scope = resolve("bd-3", repo_root=repo)

    assert scope.members == ("bd-1", "bd-2", "bd-3")
    assert scope.describe() == "bd-3 and its 2 unmet blocker(s)"


def test_a_diamond_is_visited_once(repo: Path, fake_proc: FakeProc, shown: Any) -> None:
    shown(
        bead("bd-4", **blocked_by("bd-2", "bd-3")),
        bead("bd-3", **blocked_by("bd-1")),
        bead("bd-2", **blocked_by("bd-1")),
        bead("bd-1"),
    )

    scope = resolve("bd-4", repo_root=repo)

    assert sorted(scope.members) == ["bd-1", "bd-2", "bd-3", "bd-4"]
    assert len(scope.members) == len(set(scope.members))
    # The shared blocker comes before both things that need it, and the target last.
    assert scope.members.index("bd-1") == 0
    assert scope.members[-1] == "bd-4"


def test_a_cycle_terminates(repo: Path, fake_proc: FakeProc, shown: Any) -> None:
    """`bd` should not permit one, and hanging is a poor way to find out."""
    shown(bead("bd-1", **blocked_by("bd-2")), bead("bd-2", **blocked_by("bd-1")))

    scope = resolve("bd-1", repo_root=repo)

    assert sorted(scope.members) == ["bd-1", "bd-2"]


def test_an_unreadable_blocker_is_skipped_rather_than_fatal(
    repo: Path, fake_proc: FakeProc, shown: Any
) -> None:
    """A broken edge deadlocks the queue, which the run reports honestly."""
    shown(bead("bd-2", **blocked_by("bd-gone")))

    scope = resolve("bd-2", repo_root=repo)

    assert scope.members == ("bd-2",)


# -- what a leaf scope offers --------------------------------------------------


def test_a_closure_keeps_only_ready_issues_that_are_in_it(
    repo: Path, fake_proc: FakeProc, shown: Any
) -> None:
    """`bd ready` takes no id list, so the fence is applied here."""
    shown(bead("bd-2", **blocked_by("bd-1")), bead("bd-1"))
    scope = resolve("bd-2", repo_root=repo)
    fake_proc.expect(
        ["bd", "-C", str(repo), "ready"],
        Reply(stdout=json.dumps([bead("bd-99"), bead("bd-1")])),
    )

    found = scope.tracker.ready(claim=False)

    assert found is not None
    assert found.id == "bd-1"
    # The whole queue is asked for, because the first entry may not be in scope.
    ready = next(call for call in fake_proc.calls if "ready" in call)
    assert ready[ready.index("--limit") + 1] == "0"


def test_a_closure_claims_by_id(repo: Path, fake_proc: FakeProc, shown: Any) -> None:
    """`bd ready --claim` takes whatever is first, which may not be in scope."""
    shown(bead("bd-1", status="in_progress"))
    scope = resolve("bd-1", repo_root=repo)
    fake_proc.expect(["bd", "-C", str(repo), "ready"], Reply(stdout=json.dumps([bead("bd-1")])))
    fake_proc.expect(["bd", "-C", str(repo), "update"], Reply())

    found = scope.tracker.ready(claim=True)

    assert found is not None
    assert fake_proc.ran("bd", "-C", str(repo), "update", "bd-1", "--claim")
    assert not any("--claim" in call and "ready" in call for call in fake_proc.calls)


def test_a_closure_lists_only_its_members(repo: Path, fake_proc: FakeProc, shown: Any) -> None:
    """Which is what makes `unfinished` and the run's report right."""
    shown(bead("bd-2", **blocked_by("bd-1")), bead("bd-1"))
    scope = resolve("bd-2", repo_root=repo)
    fake_proc.expect(
        ["bd", "-C", str(repo), "list"],
        Reply(stdout=json.dumps([bead("bd-1"), bead("bd-2"), bead("bd-99")])),
    )

    assert [issue.id for issue in scope.tracker.children()] == ["bd-1", "bd-2"]


# -- targets that are not work -------------------------------------------------


def test_a_missing_target_is_refused(repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    with pytest.raises(TrackerError, match="no such issue"):
        resolve("bd-nope", repo_root=repo)


def test_a_closed_target_is_refused_with_a_remedy(
    repo: Path, fake_proc: FakeProc, shown: Any
) -> None:
    """Running it would report success having done nothing."""
    shown(bead("bd-1", status="closed"))

    with pytest.raises(MilhouseError, match="already closed") as caught:
        resolve("bd-1", repo_root=repo)

    assert caught.value.remedy is not None
    assert "bd-1" in caught.value.remedy
