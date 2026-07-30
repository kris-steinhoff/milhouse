"""The data milhouse passes between its modules.

Four types carry everything: an :class:`Issue` is one unit of work in beads, an
:class:`Iteration` is one turn of the agent, a :class:`MergeRecord` is what
became of the branch that turn wrote, and a :class:`Graph` is a scope of issues
with the ``blocks`` edges between them.

Beads and git remain the source of truth for the work itself. Everything here is
derived from them. Persisting it is :mod:`milhouse.audit`'s job, not this
module's: these are values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "Graph",
    "Issue",
    "Iteration",
    "MergeRecord",
    "Outcome",
    "now",
]

_EPIC = "epic"
"""Issue type that is a container for work rather than a unit of it."""

_OPEN = "open"
"""``bd`` status for an issue nobody has claimed and nobody has set aside."""

Outcome = Literal["success", "rejected", "blocked", "partial", "stalled", "timeout", "error"]
"""How one iteration ended. See ``docs/architecture.md`` for the decision table."""


def now() -> datetime:
    """Current UTC time, used for every timestamp milhouse records."""
    return datetime.now(UTC)


class Issue(BaseModel):
    """One issue as reported by ``bd``.

    Only the fields milhouse actually reasons about are modelled; the raw bead is
    kept in :attr:`raw` so nothing is silently lost.
    """

    id: str
    title: str
    status: str
    """``bd`` status string: ``open``, ``in_progress``, ``blocked``, ``closed``."""

    issue_type: str = "task"
    description: str = ""
    assignee: str | None = None
    parent: str | None = None
    priority: int | None = None
    labels: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    """The unmodified bead JSON, for anything this model does not name."""

    @property
    def is_closed(self) -> bool:
        """Whether ``bd`` considers this issue closed."""
        return self.status == "closed"

    @property
    def blocked_by(self) -> list[str]:
        """Ids of the issues this one depends on, oldest relation first.

        ``bd show`` puts every relation in one ``dependencies`` array and tells
        them apart by ``dependency_type``, so the parent epic appears there too
        as ``parent-child``. Only ``blocks`` is a real ordering constraint, and
        only ordering constraints decide which lane an issue goes in
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

        Empty when the bead came from a query that does not carry relations,
        such as ``bd ready``.
        """
        relations = self.raw.get("dependencies")
        if not isinstance(relations, list):
            return []
        return [
            str(item["id"])
            for item in relations
            if isinstance(item, dict) and item.get("dependency_type") == "blocks" and item.get("id")
        ]


class Graph(BaseModel):
    """The issues in one scope and the ``blocks`` edges between them.

    Fetching it is :meth:`~milhouse.tracker.base.Tracker.graph`'s job. This is
    the value that comes back, and every helper on it is pure, so what the graph
    means is unit-tested rather than acted out against a database.

    ``bd ready`` already finds the parallelism, because an issue is offered only
    when every blocker is closed. The graph answers what the ready queue cannot:
    how wide the scope actually is (:attr:`width`), and what is stuck behind one
    issue (:meth:`blocked_behind`).

    **The helpers reason about unfinished work only.** A closed issue is in
    :attr:`nodes`, because the scope contains it, but it is in no wave and
    nothing waits on it. Neither is an epic, which is a container for work and
    never a unit of it, the same rule
    :meth:`~milhouse.tracker.beads.BeadsTracker.ready` applies.
    """

    nodes: dict[str, Issue] = Field(default_factory=dict)
    """Every issue in scope, by id, closed ones included."""

    edges: list[tuple[str, str]] = Field(default_factory=list)
    """``(blocker, blocked)`` pairs: ``blocks`` relations, and nothing else.

    Both ends are always in :attr:`nodes`. An edge to an issue outside the scope
    is dropped when the graph is built, because every helper here turns on
    whether a blocker is closed and the graph cannot answer that for a node it
    does not hold.
    """

    def frontier(self) -> list[Issue]:
        """Open issues with no open blocker: what ``bd ready`` offers.

        Open means exactly ``bd``'s ``open``, so an issue already in progress or
        set aside with ``bd defer`` is not on the frontier even though it is
        still unfinished work and still in a wave.

        Returns:
            The issues that could be claimed now, in :attr:`nodes` order.
        """
        blockers = self._blockers()
        return [
            issue
            for issue_id, issue in self._unfinished().items()
            if issue.status == _OPEN and not blockers[issue_id]
        ]

    def waves(self) -> list[list[str]]:
        """The unfinished issues in topological levels, deepest blockers first.

        Everything in one wave can be worked at the same time, and nothing in a
        wave can start until the wave before it is done, so this is the shape a
        concurrent run would take if every turn succeeded first time.

        A cycle cannot be levelled. Whatever is left when no further wave can be
        peeled off is appended as one final wave, so a cycle terminates and its
        issues are still named rather than quietly vanishing. ``bd`` should not
        permit one, and a walk that hangs would be a poor way to find out it did.

        Returns:
            Issue ids per level, the first level being :meth:`frontier` plus
            whatever is already in progress or set aside.
        """
        remaining = {issue_id: set(ids) for issue_id, ids in self._blockers().items()}
        levels: list[list[str]] = []
        while remaining:
            wave = [
                issue_id
                for issue_id, blockers in remaining.items()
                if not blockers & remaining.keys()
            ]
            if not wave:
                levels.append(list(remaining))
                break
            levels.append(wave)
            for issue_id in wave:
                del remaining[issue_id]
        return levels

    @property
    def width(self) -> int:
        """The widest wave: the most concurrency this scope can ever use.

        A ``--count`` above it buys nothing, which is worth knowing before a run
        rather than after one.
        """
        return max((len(wave) for wave in self.waves()), default=0)

    def blocked_behind(self, issue_id: str) -> list[str]:
        """The unfinished issues waiting on ``issue_id``, transitively.

        What a deadlocked run wants to name: not the list of things that did not
        get done, but the one issue they are all queued behind.

        Args:
            issue_id: The blocker to look behind.

        Returns:
            Ids in breadth-first order, nearest dependents first, without
            ``issue_id`` itself. Empty when nothing waits on it, and empty when
            the issue is closed or out of scope, since neither holds anything
            up. Visited ids are remembered, so a cycle terminates.
        """
        if issue_id not in self._unfinished():
            return []
        dependents = self._dependents()
        seen = {issue_id}
        queue = [issue_id]
        behind: list[str] = []
        while queue:
            for dependent in dependents.get(queue.pop(0), []):
                if dependent in seen:
                    continue
                seen.add(dependent)
                behind.append(dependent)
                queue.append(dependent)
        return behind

    def _unfinished(self) -> dict[str, Issue]:
        """The nodes that are still work: not closed, and not an epic."""
        return {
            issue_id: issue
            for issue_id, issue in self.nodes.items()
            if not issue.is_closed and issue.issue_type != _EPIC
        }

    def _blockers(self) -> dict[str, list[str]]:
        """Unfinished blockers per unfinished issue, every one of them keyed."""
        unfinished = self._unfinished()
        blockers: dict[str, list[str]] = {issue_id: [] for issue_id in unfinished}
        for blocker, blocked in self.edges:
            if blocker in unfinished and blocked in unfinished:
                blockers[blocked].append(blocker)
        return blockers

    def _dependents(self) -> dict[str, list[str]]:
        """The same edges the other way round: who waits on whom."""
        dependents: dict[str, list[str]] = {}
        for blocked, blockers in self._blockers().items():
            for blocker in blockers:
                dependents.setdefault(blocker, []).append(blocked)
        return dependents


class MergeRecord(BaseModel):
    """What became of a worker lane when the run tried to land it.

    A concurrent run merges each successful turn's branch into its integration
    branch, one at a time
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
    This is that merge as a value, so the report and the audit log carry it.

    It is the only place a **conflict** is named, and the naming is the whole
    point: the issue is closed, the worker branch is still there, the integration
    branch is exactly where it was, and only a person can land it from here. Both
    branch names are on the record for that reason, rather than being derivable
    from somewhere else.
    """

    source: str
    """The worker branch that was merged, which is where the work still is."""

    target: str
    """The integration branch it was merged into."""

    sha: str | None = None
    """Full sha the integration branch ended on, or ``None`` when nothing moved.

    ``None`` covers three cases, which :attr:`landed` tells apart: the branch was
    already contained, the merge conflicted, or git refused it outright.
    """

    fast_forwarded: bool = False
    """Whether the integration branch simply moved to :attr:`source`."""

    conflicts: list[str] = Field(default_factory=list)
    """Paths git could not merge. The merge was aborted, so nothing is half-done."""

    error: str = ""
    """Why a merge that did not conflict could not be run at all.

    Recorded rather than raised, for the reason the audit log tolerates a failed
    write: the turn it describes has already happened and cannot be re-run, so
    losing it to report the merge failure would be the worse trade.
    """

    skipped: str = ""
    """Why git was never asked, when it was not.

    Set for every merge after the first one that did not land: the integration
    branch is closed to further merges for the rest of the run, because it has
    stopped being a prefix of the merge order and a later branch landing on it
    would change the resolution a person has to do
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
    The turn itself is finished, recorded and settled exactly as any other, so
    this says only what became of its branch.
    """

    @property
    def landed(self) -> bool:
        """Whether the integration branch now contains :attr:`source`.

        True for a fast-forward, for a merge commit, and for a branch that was
        already contained. False is the mess a serial run could not leave, and
        it covers a merge nobody attempted as well as one git refused: the
        consequence is the same closed issue on the same live branch.
        """
        return not self.conflicts and not self.error and not self.skipped

    @property
    def joined(self) -> bool:
        """Whether this merge combined two histories nobody has tested together.

        The signal ADR 0024 keeps by not passing ``--no-ff``, carried through to
        the record: a fast-forward leaves the tree the worker lane was already
        verified against, and only a real merge commit produces one that nothing
        has run a gate over.
        """
        return self.sha is not None and not self.fast_forwarded


class Iteration(BaseModel):
    """The record of one turn: one issue, one fresh agent, one classification.

    One iteration is: claim an issue, start a fresh agent, prompt it once, exit
    it, verify, then classify what happened. Nearly every field here goes into
    the beads audit log so a finished or crashed run can be explained after the
    fact (:mod:`milhouse.audit`).
    """

    number: int
    """1-based iteration counter, over the repository rather than one invocation.

    It names the run artifacts (``<issue-id>/iter-007.prompt``), so it has to
    keep counting across invocations even though the iteration budget does not.
    """

    issue_id: str
    issue_title: str = ""
    outcome: Outcome

    attempt: int = 1
    """1-based attempt number for this issue, counted from the audit history.

    Distinct from :attr:`number`, which counts every turn in the repository.
    Two issues worked once each are iterations 1 and 2, both attempt 1.

    It is on the iteration so a policy can cap attempts without going and
    looking. :func:`milhouse.policy.decide` and
    :func:`milhouse.policy.unattended` are pure, so anything they weigh has to
    arrive on the value they are given
    (:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`).
    """

    agent_state: str | None = None
    """Terminal herdr agent status observed for the turn, e.g. ``idle``."""

    head_before: str | None = None
    head_after: str | None = None
    """Git ``HEAD`` around the turn."""

    commits: list[str] = Field(default_factory=list)
    """Short shas of every commit that landed during the turn, oldest first."""

    attributed: bool = False
    """Whether any of :attr:`commits` names this issue in its message.

    ``HEAD`` moving is weak evidence on its own: a hook, or a human in another
    terminal, moves it too. The iteration prompt asks for the issue id in the
    commit message, and this is milhouse checking that it is there.
    """

    dirty_after: bool = False
    """Whether the working tree had uncommitted changes when the turn ended.

    An agent that edits without committing hands the mess to the next agent,
    which did not make it and cannot explain it.
    """

    merge: MergeRecord | None = None
    """What happened when this turn's branch was landed in the integration branch.

    ``None`` when there was nothing to land: an unsuccessful turn, whose commits
    stay on its worker branch for the next attempt, or a session with no worker
    lanes at all. That last is ``step``, ``dispatch``, ``reap``, and a
    ``--count 1`` run, which is
    :doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>` exactly and
    has one branch to merge into and nothing to merge into it.
    """

    verified: bool | None = None
    """Whether the verification command passed. ``None`` when it was not run."""

    integration_verified: bool | None = None
    """Whether the gate passed on the integration branch after this turn landed.

    ``None`` when it was not run, which is most turns: no gate is configured, or
    nothing was merged, or the merge fast-forwarded and so left the tree the
    worker lane was already verified against
    (:attr:`MergeRecord.joined`). ``False`` is the case this whole second run
    exists to find — two branches that were green apart and are red together —
    and it stops the run without undoing anything
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
    """

    integration_output: str = ""
    """Tail of the gate's output from the integration branch, kept only when red.

    Left out of the audit entry for the reason :attr:`verification_output` is:
    it is unbounded, and it already has a home on the issue's notes, where the
    person who comes to look for it will be.
    """

    verification_output: str = ""
    """Tail of the verification command's output, kept only when it failed.

    This is what the note on the re-opened issue carries, and therefore the only
    way the next agent learns why the last one's work was turned down. It is the
    one field the audit entry leaves out: it is unbounded, and a long line in
    ``interactions.jsonl`` is a line that can tear
    (:doc:`ADR 0021 <../../docs/decisions/0021-iteration-history-goes-in-the-beads-audit-log>`).
    """

    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None
    detail: str = ""
    """One line explaining the outcome, shown in ``milhouse status``."""

    prompt_path: str | None = None
    transcript_path: str | None = None
    """Paths, repo-relative, of the captured prompt and pane transcript."""

    @property
    def made_commit(self) -> bool:
        """Whether anything was committed during this iteration."""
        return bool(self.commits)
