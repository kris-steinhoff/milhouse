"""What a run's target means: which issues are in it, and what finishing is.

``milhouse run <target>`` takes a beads id and nothing else. There is no task
definition and no file, so the target introduces no second record of the work
(:doc:`ADR 0018 <../../docs/decisions/0018-no-task-milhouse-works-the-ready-queue>`).

**A scope is a tracker.** Resolving a target produces a
:class:`~milhouse.tracker.beads.BeadsTracker` that is already fenced to it, so
:class:`~milhouse.session.Session`, :func:`~milhouse.step.step`, and everything
else keeps taking a plain ``Tracker`` and needs no idea a target exists. That is
the whole reason this module is small.

Two kinds of target, because ``bd`` scopes one of them and cannot scope the
other:

``epic``
    ``bd ready --parent`` and ``bd list --parent`` filter to descendants, so the
    fence is a config value and ``bd`` applies it.

``closure``
    A leaf issue has no descendants, so ``--parent`` filters to nothing. What is
    in scope instead is the issue and its unmet blockers, transitively, because
    ``bd ready`` will not offer a blocked issue and the target cannot close
    until they do. That set is walked here and handed to the tracker as an
    explicit membership.

**Finishing is the same question for both**, and it is one somebody else already
answers: :func:`milhouse.step.nothing_ready` reports whether anything in the
tracker's scope is unfinished. Nothing in this module needs a second opinion on
it, which is why there is no ``is_done`` here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import TrackerConfig
from .errors import MilhouseError, TrackerError
from .models import Issue
from .tracker.beads import BeadsTracker

__all__ = ["Scope", "resolve"]

log = logging.getLogger(__name__)

EPIC = "epic"
"""Issue type whose descendants are the work."""


@dataclass(frozen=True)
class Scope:
    """One run's target, and the tracker fenced to it.

    Attributes:
        target: The issue or epic the run is working towards.
        tracker: A tracker that offers only the issues in scope.
        members: The issues in scope for a closure target, in dependency order,
            deepest blocker first. Empty for an epic, where ``bd`` holds the
            fence and milhouse never enumerates it.
    """

    target: Issue
    tracker: BeadsTracker
    members: tuple[str, ...] = ()

    @property
    def is_epic(self) -> bool:
        """Whether the fence is ``bd``'s ``--parent`` rather than a membership."""
        return not self.members

    def describe(self) -> str:
        """One line naming what is in scope, for ``status`` and ``--dry-run``."""
        if self.is_epic:
            return f"every ready issue under {self.target.id}"
        if len(self.members) == 1:
            return f"{self.target.id} alone"
        blockers = len(self.members) - 1
        return f"{self.target.id} and its {blockers} unmet blocker(s)"


def resolve(
    target_id: str,
    *,
    repo_root: Path,
    config: TrackerConfig | None = None,
) -> Scope:
    """Work out what ``target_id`` means and fence a tracker to it.

    Args:
        target_id: A beads id: an epic, or a single issue.
        repo_root: The repository whose beads database holds it.
        config: The repository's standing ``[tracker]`` fence. A label in it is
            kept, because it says which issues were ever meant for an agent. A
            parent in it is replaced, because the target is a narrower answer to
            the same question.

    Returns:
        The resolved scope.

    Raises:
        TrackerError: No issue has that id.
        MilhouseError: The target is already closed, so there is nothing to run.
    """
    config = config or TrackerConfig()
    reader = BeadsTracker(repo_root, config)
    try:
        target = reader.get(target_id)
    except TrackerError as exc:
        # A typo in the target is the likeliest first mistake with `run`, and
        # bd's own message for it arrives wrapped in an argv dump under a
        # remedy about `bd init`. Say the useful thing instead.
        raise _no_such(target_id, exc) from exc

    if target.is_closed:
        raise _finished(target)

    if target.issue_type == EPIC:
        under = config.model_copy(update={"parent": target.id})
        return Scope(target=target, tracker=BeadsTracker(repo_root, under))

    members = _closure(reader, target)
    return Scope(
        target=target,
        tracker=BeadsTracker(repo_root, config, members=members),
        members=tuple(members),
    )


def _closure(tracker: BeadsTracker, target: Issue) -> list[str]:
    """``target`` and everything it transitively depends on, blockers first.

    Depth-first over :attr:`~milhouse.models.Issue.blocked_by`, which only
    ``bd show`` carries and which only counts ``blocks`` relations, so a parent
    epic in the same array is not mistaken for a dependency.

    Visited ids are remembered, so a diamond is walked once and a cycle
    terminates rather than looping. ``bd`` should not permit a cycle, and a
    walk that hangs on one would be a poor way to find out.

    A blocker that cannot be read is skipped with a warning rather than taking
    the run down. The issue stays in scope through whatever depends on it, and
    the run will stop honestly when the queue deadlocks.
    """
    order: list[str] = []
    seen: set[str] = set()

    def walk(issue: Issue) -> None:
        if issue.id in seen:
            return
        seen.add(issue.id)
        for blocker_id in issue.blocked_by:
            try:
                walk(tracker.get(blocker_id))
            except TrackerError as exc:
                log.warning("skipping the blocker %s of %s: %s", blocker_id, issue.id, exc)
                seen.add(blocker_id)
        order.append(issue.id)

    walk(target)
    return order


def _no_such(target_id: str, cause: TrackerError) -> TrackerError:
    """The error for a target that is not in the tracker.

    The cause is kept, because "no issue found" and "the database is not there"
    both arrive here and they want opposite things done about them.
    """
    error = TrackerError(f"no such target: {target_id} ({cause})")
    error.remedy = f"Check the id with `bd list`, or `bd show {target_id}`."
    return error


def _finished(target: Issue) -> MilhouseError:
    """The error for a target there is nothing left to do to."""
    error = MilhouseError(f"{target.id} is already closed, so there is nothing to run")
    error.remedy = f"Pick another target, or re-open it with `bd update {target.id} --status open`."
    return error
