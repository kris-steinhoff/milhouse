"""The data milhouse passes between its modules.

Four types carry everything: a :class:`TaskDefinition` is what the user asked
for, an :class:`Issue` is one unit of work in beads, an :class:`Iteration` is one
pass of the ralph loop, and a :class:`RunState` is the durable bookkeeping that
lets a run be inspected or resumed after a crash.

Beads and git remain the source of truth for the work itself. Everything here is
either derived from them or is loop bookkeeping.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
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

Outcome = Literal["success", "blocked", "partial", "stalled", "timeout", "error"]
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
    """The record of one pass through the ralph loop.

    One iteration is: claim an issue, start a fresh agent, prompt it once, exit
    it, then classify what happened. Every field here is written to
    ``state.json`` so a finished or crashed run can be explained after the fact.
    """

    number: int
    """1-based iteration counter within the run."""

    issue_id: str
    issue_title: str = ""
    outcome: Outcome
    agent_state: str | None = None
    """Terminal herdr agent status observed for the turn, e.g. ``idle``."""

    head_before: str | None = None
    head_after: str | None = None
    """Git ``HEAD`` around the turn. A change means the agent committed."""

    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None
    detail: str = ""
    """One line explaining the outcome, shown in ``milhouse status``."""

    prompt_path: str | None = None
    transcript_path: str | None = None
    """Paths, repo-relative, of the captured prompt and pane transcript."""

    @property
    def made_commit(self) -> bool:
        """Whether ``HEAD`` moved during this iteration."""
        return bool(self.head_before and self.head_after and self.head_before != self.head_after)


class RunState(BaseModel):
    """Durable bookkeeping for one task's run, persisted as ``state.json``.

    This is not the source of truth for the work. It records what milhouse
    itself did: which workspace and pane it is driving, which epic it is working
    through, how many attempts each issue has cost, and the iteration history.
    Deleting it loses the history and the attempt counts, nothing else.
    """

    version: int = 1
    """Schema version of this file, so future milhouse can migrate it."""

    task_id: str
    task_slug: str
    epic_id: str | None = None
    workspace_id: str | None = None
    pane_id: str | None = None
    branch: str | None = None
    """Branch the loop commits to, when ``git.branch_strategy = "task"``."""

    owns_workspace: bool = False
    """Whether milhouse created the workspace (and may therefore close it)."""

    attempts: dict[str, int] = Field(default_factory=dict)
    """Failed attempts per issue id, against ``loop.max_attempts``."""

    iterations: list[Iteration] = Field(default_factory=list)
    claimed_issue: str | None = None
    """Issue claimed but not yet resolved. Reverted on teardown or resume."""

    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    @classmethod
    def load(cls, path: Path) -> RunState | None:
        """Read a state file, returning ``None`` when it does not exist.

        Args:
            path: Path to ``state.json``.

        Returns:
            The parsed state, or ``None`` for a first run.
        """
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        """Write this state to ``path`` atomically, creating parent directories.

        The write goes to a sibling temporary file and is then renamed, so a
        crash mid-write cannot leave a truncated ``state.json`` behind.

        Args:
            path: Path to ``state.json``.
        """
        self.updated_at = now()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(self.model_dump_json())
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def record(self, iteration: Iteration) -> None:
        """Append an iteration and update the issue's attempt counter.

        ``success`` and ``blocked`` do not count against the attempt cap: the
        first finished the issue, and the second is waiting on a human rather
        than failing.

        Args:
            iteration: The iteration that just ended.
        """
        self.iterations.append(iteration)
        if iteration.outcome not in ("success", "blocked"):
            self.attempts[iteration.issue_id] = self.attempts.get(iteration.issue_id, 0) + 1

    def attempts_for(self, issue_id: str) -> int:
        """Failed attempts recorded so far for ``issue_id``."""
        return self.attempts.get(issue_id, 0)
