"""What a run's target means: which issues are in it, and what finishing is.

``milhouse run <target>...`` takes one or more beads ids and nothing else.
There is no task definition and no file, so a target introduces no second
record of the work
(:doc:`ADR 0018 <../../docs/decisions/0018-no-task-milhouse-works-the-ready-queue>`).

**A scope is a tracker.** Resolving a target produces a
:class:`~milhouse.tracker.beads.BeadsTracker` that is already fenced to it, so
:class:`~milhouse.session.Session`, :func:`~milhouse.step.step`, and everything
else keeps taking a plain ``Tracker`` and needs no idea how many targets, or
what kind, went into it. That is the whole reason this module is small.

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

**Several targets are one scope that is a union**, not several runs glued
together. :func:`resolve` still handles one target exactly as it always has,
``bd`` fence and all. :func:`resolve_many` is what a run actually calls: at one
target it is :func:`resolve` unchanged, and above one it computes each target's
own members the way :func:`resolve` already does per kind, unions them in the
order given, and hands the tracker that explicit membership --- the one fence
``bd`` cannot itself express, because ``--parent`` takes one parent. An issue
under one target that is blocked by an issue under another is a member of the
union either way, so it is offered by ``bd ready`` the moment its blocker
closes, in the same run rather than a second one. Everything downstream of a
``Scope`` --- :mod:`milhouse.run`, :mod:`milhouse.step`, :mod:`milhouse.parallel`
--- takes the tracker and never asks how many targets built it.

**Finishing is the same question for one target or several**, and it is one
somebody else already answers: :func:`milhouse.step.nothing_ready` reports
whether anything in the tracker's scope is unfinished. Nothing in this module
needs a second opinion on it, which is why there is no ``is_done`` here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import TrackerConfig
from .errors import MilhouseError, TrackerError
from .models import Issue
from .tracker.beads import BeadsTracker

__all__ = ["Scope", "resolve", "resolve_many"]

log = logging.getLogger(__name__)

EPIC = "epic"
"""Issue type whose descendants are the work."""


@dataclass(frozen=True)
class Scope:
    """A run's target(s), and the tracker fenced to them.

    Attributes:
        targets: The issue(s) or epic(s) the run is working towards, in the
            order given. Never empty.
        tracker: A tracker that offers only the issues in scope.
        members: The issues in scope for a closure target or a multi-target
            scope, in dependency order, deepest blocker first, per target, with
            duplicates dropped at their first occurrence. Empty for a single
            epic target, where ``bd`` holds the fence and milhouse never
            enumerates it.
    """

    targets: tuple[Issue, ...]
    tracker: BeadsTracker
    members: tuple[str, ...] = ()

    @property
    def is_epic(self) -> bool:
        """Whether the fence is ``bd``'s ``--parent`` rather than a membership."""
        return not self.members

    @property
    def key(self) -> str:
        """What a run's integration lane is labelled with.

        A single target's own id, unchanged
        (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`). Several
        targets have no one id to borrow, so the key is every target's id,
        sorted and joined, which makes it independent of the order they were
        given on the command line and lets a second run of the same targets
        find the first run's lane the same way a single target already can
        (:doc:`ADR 0025 <../../docs/decisions/0025-a-multi-target-run-shares-one-lane>`).
        """
        if len(self.targets) == 1:
            return self.targets[0].id
        return "+".join(sorted(target.id for target in self.targets))

    def describe(self) -> str:
        """One line naming what is in scope, for ``status`` and ``--dry-run``."""
        if len(self.targets) > 1:
            ids = ", ".join(target.id for target in self.targets)
            return f"{ids} ({len(self.members)} issue(s) total)"
        target = self.targets[0]
        if self.is_epic:
            return f"every ready issue under {target.id}"
        if len(self.members) == 1:
            return f"{target.id} alone"
        blockers = len(self.members) - 1
        return f"{target.id} and its {blockers} unmet blocker(s)"


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
        return Scope(targets=(target,), tracker=BeadsTracker(repo_root, under))

    members = _closure(reader, target)
    return Scope(
        targets=(target,),
        tracker=BeadsTracker(repo_root, config, members=members),
        members=tuple(members),
    )


def resolve_many(
    target_ids: Sequence[str],
    *,
    repo_root: Path,
    config: TrackerConfig | None = None,
) -> Scope:
    """Resolve one or more targets into a single scope that is their union.

    At one target this is :func:`resolve`, unchanged down to the lane it names:
    duplicates are dropped first, so a caller that repeats the same id still
    takes the one-target path. Above that, ``bd`` has no fence for a union of
    targets, so each target's own members are computed the way :func:`resolve`
    already computes them per kind --- an epic's descendants via ``bd``'s
    ``--parent``, a leaf's blockers via :func:`_closure` --- and unioned in the
    order given, keeping the first occurrence of a duplicate. The tracker is
    then fenced to that explicit membership
    (:doc:`ADR 0025 <../../docs/decisions/0025-a-multi-target-run-shares-one-lane>`).

    Args:
        target_ids: One or more beads ids: epics, issues, or a mix.
        repo_root: The repository whose beads database holds them.
        config: The repository's standing ``[tracker]`` fence. A label in it is
            kept. A parent in it plays no part above one target, since the
            explicit membership is the fence.

    Returns:
        The resolved scope.

    Raises:
        MilhouseError: No target was given, or one of them is already closed.
        TrackerError: One of the ids does not exist.
    """
    ids = list(dict.fromkeys(target_ids))
    if not ids:
        raise MilhouseError("run needs at least one target")
    if len(ids) == 1:
        return resolve(ids[0], repo_root=repo_root, config=config)

    config = config or TrackerConfig()
    reader = BeadsTracker(repo_root, config)
    targets: list[Issue] = []
    members: list[str] = []
    seen: set[str] = set()
    for target_id in ids:
        try:
            target = reader.get(target_id)
        except TrackerError as exc:
            raise _no_such(target_id, exc) from exc
        if target.is_closed:
            raise _finished(target)
        targets.append(target)
        for member_id in _scope_members(reader, target):
            if member_id not in seen:
                seen.add(member_id)
                members.append(member_id)

    return Scope(
        targets=tuple(targets),
        tracker=BeadsTracker(repo_root, config, members=members),
        members=tuple(members),
    )


def _scope_members(tracker: BeadsTracker, target: Issue) -> list[str]:
    """One target's own members: an epic's descendants, or a leaf's closure.

    What :func:`resolve` computes per kind, factored out so
    :func:`resolve_many` can union several without asking ``bd`` for a fence it
    cannot express.
    """
    if target.issue_type == EPIC:
        return [issue.id for issue in tracker.children(parent_id=target.id)]
    return _closure(tracker, target)


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
