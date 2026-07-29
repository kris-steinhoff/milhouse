"""The issue-tracker interface a step depends on.

A step needs a short list from a tracker: claim the next ready issue, read an
issue back, list a scope of issues, and annotate, re-open, or set one aside.
:class:`Tracker` is exactly that list and nothing more.

Six methods is what is left once there is no task to decompose
(:doc:`ADR 0018 <../../docs/decisions/0018-no-task-milhouse-works-the-ready-queue>`),
plus the one a loop needs. Getting work *into* the tracker is somebody else's
job, so nothing here creates an issue.

Marking an issue **blocked** is deliberately not on it. That was how the old
attempt cap gave up on an issue and moved to the next, and giving up is a
person's decision
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).
:meth:`Tracker.defer` is not that rule coming back: deferring does not decide
the issue is hopeless, it sets it aside where a person will see it and stops the
run from spending the rest of its budget on it
(:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`).

There is one real implementation, :mod:`milhouse.tracker.beads`. The protocol
exists because the tests implement it, not as speculative generality.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Issue

__all__ = ["Tracker"]


@runtime_checkable
class Tracker(Protocol):
    """Everything milhouse asks of an issue tracker."""

    def ready(self, *, claim: bool) -> Issue | None:
        """Return the next issue ready to be worked, optionally claiming it.

        The scope is whatever ``[tracker]`` configures, which is the whole
        repository by default.

        Args:
            claim: Atomically mark the issue in progress and assigned. This is
                what makes concurrent dispatchers safe.

        Returns:
            The next ready issue, or ``None`` when there is none.
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

    def children(self, parent_id: str | None = None) -> list[Issue]:
        """Every issue under ``parent_id``, closed ones included.

        Args:
            parent_id: The epic to look under, or ``None`` for every issue in
                the configured scope.
        """
        ...

    def release(self, issue_id: str, *, note: str | None = None) -> None:
        """Return a claimed issue to the open, unassigned pool.

        Args:
            issue_id: The issue to release.
            note: Optional note appended to the issue explaining why.
        """
        ...

    def defer(self, issue_id: str, *, reason: str) -> None:
        """Set an issue aside: still unfinished, no longer offered as ready.

        What a run does with an issue that has used up its attempts. The issue
        stays open and stays listed, so it still counts as unfinished work and
        the run's report can name it, but it stops being claimed, so the rest of
        the budget goes somewhere it might do some good.

        Args:
            issue_id: The issue to set aside.
            reason: Why, recorded on the issue for whoever picks it back up.
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
