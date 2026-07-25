"""The issue-tracker interface the loop depends on.

The loop needs six things from a tracker: find the epic for a task, create an
epic, create children under it, claim the next ready issue, read an issue back,
and annotate or re-open one. :class:`Tracker` is exactly that list and nothing
more.

There is one real implementation, :mod:`milhouse.tracker.beads`. The protocol
exists because the loop's tests implement it, not as speculative generality.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Issue, TaskDefinition

__all__ = ["PlannedIssue", "Tracker"]


class PlannedIssue(Protocol):
    """One issue a plan proposes, before it exists in the tracker.

    Structurally compatible with :class:`milhouse.planner.PlanIssue`, which is
    the only thing that implements it.
    """

    key: str
    title: str
    type: str
    priority: int | None
    description: str
    acceptance: str
    blocked_by: list[str]


@runtime_checkable
class Tracker(Protocol):
    """Everything milhouse asks of an issue tracker."""

    def find_epic(self, task: TaskDefinition) -> Issue | None:
        """Return the epic already decomposing ``task``, or ``None``.

        Args:
            task: The resolved task definition.

        Returns:
            The epic carrying this task's id in its metadata, or ``None`` when
            the task has not been decomposed yet.
        """
        ...

    def create_epic(self, task: TaskDefinition) -> Issue:
        """Create the epic for ``task``, tagged so :meth:`find_epic` finds it.

        Args:
            task: The resolved task definition.

        Returns:
            The created epic.
        """
        ...

    def create_children(self, epic_id: str, issues: list[PlannedIssue]) -> list[Issue]:
        """Create every planned issue under ``epic_id`` and wire its dependencies.

        Args:
            epic_id: The parent epic.
            issues: Planned issues, whose ``blocked_by`` entries refer to other
                issues in the same list by ``key``.

        Returns:
            The created issues, in the order given.
        """
        ...

    def ready(self, epic_id: str, *, claim: bool) -> Issue | None:
        """Return the next issue ready to be worked, optionally claiming it.

        Args:
            epic_id: The epic to look under.
            claim: Atomically mark the issue in progress and assigned. This is
                what makes concurrent loops safe.

        Returns:
            The next ready issue, or ``None`` when there is none — which is how
            the loop learns the epic is finished.
        """
        ...

    def get(self, issue_id: str) -> Issue:
        """Read one issue back.

        Args:
            issue_id: The issue to read.

        Returns:
            The issue as the tracker currently has it.
        """
        ...

    def children(self, epic_id: str) -> list[Issue]:
        """Every issue under ``epic_id``, closed ones included."""
        ...

    def release(self, issue_id: str, *, note: str | None = None) -> None:
        """Return a claimed issue to the open, unassigned pool.

        Args:
            issue_id: The issue to release.
            note: Optional note appended to the issue explaining why.
        """
        ...

    def block(self, issue_id: str, note: str) -> None:
        """Mark an issue as needing a human, with a note saying why.

        Args:
            issue_id: The issue to block.
            note: What went wrong, for whoever picks it up.
        """
        ...

    def note(self, issue_id: str, text: str) -> None:
        """Append a note to an issue.

        Notes are how a fresh context window learns what the previous attempt
        found, so this is the loop's only memory between iterations.

        Args:
            issue_id: The issue to annotate.
            text: The note body.
        """
        ...
