"""The data milhouse passes between its modules.

Two types carry everything: an :class:`Issue` is one unit of work in beads, and
an :class:`Iteration` is one turn of the agent.

Beads and git remain the source of truth for the work itself. Everything here is
derived from them. Persisting it is :mod:`milhouse.audit`'s job, not this
module's: these are values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "Issue",
    "Iteration",
    "Outcome",
    "now",
]

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

    verified: bool | None = None
    """Whether the verification command passed. ``None`` when it was not run."""

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
