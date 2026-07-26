"""The data milhouse passes between its modules.

Four types carry everything: a :class:`TaskDefinition` is what the user asked
for, an :class:`Issue` is one unit of work in beads, an :class:`Iteration` is one
turn of the agent, and a :class:`RunState` is the durable bookkeeping that lets a
run be inspected or resumed after a crash.

Beads and git remain the source of truth for the work itself. Everything here is
either derived from them or is bookkeeping. Persisting it is
:mod:`milhouse.state`'s job, not this module's: these are values.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "Issue",
    "Iteration",
    "Outcome",
    "RunState",
    "SourceKind",
    "TaskDefinition",
    "now",
    "slugify",
]

SourceKind = Literal["file", "github"]

Outcome = Literal["success", "rejected", "blocked", "partial", "stalled", "timeout", "error"]
"""How one iteration ended. See ``docs/architecture.md`` for the decision table."""

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def now() -> datetime:
    """Current UTC time, used for every timestamp milhouse records."""
    return datetime.now(UTC)


def slugify(text: str) -> str:
    """Reduce ``text`` to lowercase ``[a-z0-9-]``, for use in paths and labels.

    Args:
        text: Arbitrary text.

    Returns:
        A slug, or ``"task"`` when nothing survives normalisation.
    """
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return slug or "task"


class TaskDefinition(BaseModel):
    """One thing the user wants done, resolved from a source spec.

    The ``task_id`` is the stable link between a task definition and the beads
    epic that decomposes it. It is stored in the epic's metadata, so re-running
    milhouse against the same spec finds the existing decomposition instead of
    planning again.
    """

    task_id: str
    """Stable identity: ``file:<repo-relative-path>`` or ``gh:<owner>/<repo>#<n>``."""

    title: str
    """Short human title, used for the epic title and the workspace label."""

    body: str
    """Full text of the task definition, handed to the planning agent verbatim."""

    kind: SourceKind
    """Which resolver produced this definition."""

    slug: str
    """Filesystem- and label-safe short name, e.g. ``hello``."""

    external_ref: str | None = None
    """``bd --external-ref`` value, e.g. ``gh-123``. ``None`` for file sources."""

    url: str | None = None
    """Where a human can read the original, when the source has a URL."""


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


class Iteration(BaseModel):
    """The record of one turn: one issue, one fresh agent, one classification.

    One iteration is: claim an issue, start a fresh agent, prompt it once, exit
    it, verify, then classify what happened. Every field here is appended to
    ``events.jsonl`` so a finished or crashed run can be explained after the fact.
    """

    number: int
    """1-based iteration counter, over the whole task rather than one invocation.

    It names the run artifacts (``iter-007.prompt``), so it has to keep counting
    across invocations even though the iteration budget does not.
    """

    issue_id: str
    issue_title: str = ""
    outcome: Outcome
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


class RunState(BaseModel):
    """The session facts for one task, persisted as ``state.json``.

    This is not the source of truth for the work, and it is not the history
    either. It records only what milhouse needs in order to pick a task back up:
    which epic it is working through, which workspace and pane it is driving,
    which branch, and whether a claim was left in flight. The history lives in
    ``events.jsonl`` beside it (:mod:`milhouse.state`).

    Deleting the run directory loses the history, nothing else.
    """

    version: int = 2
    """Schema version of this file.

    Version 1 also carried ``iterations`` and ``attempts``. Both are gone: the
    history moved to ``events.jsonl``, and the attempt ladder went with the
    unattended retry policy. A version 1 file still loads, minus its history.
    """

    task_id: str
    task_slug: str
    epic_id: str | None = None
    workspace_id: str | None = None
    pane_id: str | None = None
    branch: str | None = None
    """Branch the work is committed to, when ``git.branch_strategy = "task"``."""

    owns_workspace: bool = False
    """Whether milhouse created the workspace (and may therefore close it)."""

    claimed_issue: str | None = None
    """Issue claimed but not yet resolved. Reverted on teardown or resume."""

    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
