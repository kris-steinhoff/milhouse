"""The ``bd`` implementation of :class:`~milhouse.tracker.base.Tracker`.

Every method is one or two ``bd`` invocations through :mod:`milhouse.proc`.

The loop's two questions are answered by ``bd`` directly, with no state of
milhouse's own:

- what next? ``bd ready --claim --limit 1``, plus whatever ``[tracker]`` fences
  the queue with
- done? the same ``bd ready`` returns an empty list

The fence is a label, a parent, or neither
(:doc:`ADR 0018 <../../docs/decisions/0018-no-task-milhouse-works-the-ready-queue>`).
``bd`` applies it, because ``bd ready`` already takes both and already excludes
blocked, deferred, and in-progress issues.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

from .. import proc
from ..config import TrackerConfig
from ..errors import MilhouseError, TrackerError
from ..models import Issue

__all__ = ["BeadsTracker"]

TIMEOUT = 120.0
"""Seconds any single ``bd`` call may take. Generous: Dolt can be slow to start."""


class BeadsTracker:
    """Talks to the beads database in one repository."""

    def __init__(
        self,
        repo_root: Path,
        config: TrackerConfig | None = None,
        *,
        members: Collection[str] | None = None,
    ) -> None:
        """Bind to the beads database discovered from ``repo_root``.

        Args:
            repo_root: Repository whose ``.beads`` database to use.
            config: The ready-queue filter. Unfiltered if omitted.
            members: An explicit set of issues to work, and nothing else.
                ``None`` leaves the fence to ``config``. This is the one scope
                ``bd`` cannot express, so milhouse applies it here rather than
                threading it through everything that holds a tracker
                (:mod:`milhouse.scope`).
        """
        self.repo_root = repo_root
        self.config = config or TrackerConfig()
        self.members = frozenset(members) if members is not None else None

    # -- queries ---------------------------------------------------------

    def get(self, issue_id: str) -> Issue:
        """Read one issue back.

        Raises:
            TrackerError: No issue with that id exists.
        """
        issues = self._issues(["show", issue_id])
        if not issues:
            raise TrackerError(f"no such issue: {issue_id}")
        return issues[0]

    def children(self, parent_id: str | None = None) -> list[Issue]:
        """Every issue under ``parent_id``, or in the configured scope, closed included."""
        argv = ["list", "--all", "--limit", "0"]
        parent = parent_id or self.config.parent
        if parent:
            argv += ["--parent", parent]
        if self.config.label:
            argv += ["--label", self.config.label]
        issues = self._issues(argv)
        if self.members is not None and parent_id is None:
            return [issue for issue in issues if issue.id in self.members]
        return issues

    def ready(self, *, claim: bool) -> Issue | None:
        """Return the next ready issue in scope, optionally claiming it.

        ``bd ready`` already excludes in-progress, blocked, and deferred issues,
        and ``--claim`` makes taking one atomic, so two dispatchers cannot pick
        the same issue. Epics are excluded: an epic is a container for the work,
        not a unit of it.
        """
        if self.members is not None:
            return self._ready_among(claim=claim)
        issues = self._issues([*self._ready_argv(limit="1"), *(["--claim"] if claim else [])])
        return issues[0] if issues else None

    def _ready_argv(self, *, limit: str) -> list[str]:
        """``bd ready`` with whatever fence ``bd`` itself can apply."""
        argv = ["ready", "--limit", limit, "--exclude-type", "epic"]
        if self.config.parent:
            argv += ["--parent", self.config.parent]
        if self.config.label:
            argv += ["--label", self.config.label]
        return argv

    def _ready_among(self, *, claim: bool) -> Issue | None:
        """The next ready issue that is one of :attr:`members`.

        An explicit set of ids is the one fence ``bd ready`` does not take, so
        this asks for the whole ready queue and keeps the first issue in scope.
        Claiming is then by id, which ``bd update --claim`` does atomically.

        **This gives up the atomicity of ``bd ready --claim``.** Between the
        list and the claim another process could take the issue, and the claim
        would then quietly succeed against an issue that is already somebody
        else's. A serial run holding one lock is safe, and this is a real
        constraint on ever running several at once
        (:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`).
        """
        assert self.members is not None
        for issue in self._issues(self._ready_argv(limit="0")):
            if issue.id not in self.members:
                continue
            if not claim:
                return issue
            self._run(["update", issue.id, "--claim"])
            return self.get(issue.id)
        return None

    # -- mutations -------------------------------------------------------

    def release(self, issue_id: str, *, note: str | None = None) -> None:
        """Return a claimed issue to the open, unassigned pool."""
        if note:
            self.note(issue_id, note)
        self._run(["update", issue_id, "--status", "open", "--assignee", ""])

    def defer(self, issue_id: str, *, reason: str) -> None:
        """Set an issue aside with ``bd defer``.

        ``bd defer`` is the tracker's own word for this: a deferred issue is
        hidden from ``bd ready`` and still listed by ``bd list``, which is
        exactly "unfinished, but stop offering it". Deferring without
        ``--until`` is the status-based form, so nothing has to guess a date at
        which the issue becomes interesting again.

        ``bd update --status blocked`` was the alternative and would have been a
        lie: blocked in ``bd`` means the issue has an unmet dependency
        (:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`).
        """
        self._run(["defer", issue_id, f"--reason={reason}"])

    def close(self, issue_id: str, *, note: str | None = None) -> None:
        """Close an issue.

        milhouse does not normally call this — the agent closes its own issue,
        and that is the success signal
        (:doc:`ADR 0004 <../../docs/decisions/0004-outcome-from-beads-and-git>`).
        It exists for the cases where milhouse resolves an issue itself.
        """
        if note:
            self.note(issue_id, note)
        self._run(["close", issue_id])

    def note(self, issue_id: str, text: str) -> None:
        """Append a note to an issue."""
        self._run(["note", issue_id, text])

    # -- plumbing --------------------------------------------------------

    def _run(self, args: list[str]) -> proc.ProcResult:
        """Run a ``bd`` command against this repo, translating failures."""
        try:
            return proc.run(["bd", "-C", str(self.repo_root), *args], timeout=TIMEOUT)
        except MilhouseError as exc:
            raise TrackerError(f"bd {' '.join(args[:2])} failed: {exc}") from exc

    def _json(self, args: list[str]) -> Any:
        """Run a ``bd`` command with ``--json`` and parse the result."""
        try:
            return proc.run_json(
                ["bd", "-C", str(self.repo_root), *args, "--json"],
                timeout=TIMEOUT,
                allow_empty=True,
            )
        except MilhouseError as exc:
            raise TrackerError(f"bd {' '.join(args[:2])} failed: {exc}") from exc

    def _issues(self, args: list[str]) -> list[Issue]:
        """Run a ``bd`` command expected to return a list of issues."""
        return _parse_issues(self._json(args))


def _parse_issues(payload: Any) -> list[Issue]:
    """Normalise ``bd``'s JSON into :class:`~milhouse.models.Issue` values.

    ``bd`` returns a bare object from some commands and a list from ``list``,
    ``ready``, and ``show``, so both shapes are accepted.

    Raises:
        TrackerError: The payload is neither an object nor a list of objects.
    """
    if payload is None:
        return []
    items = payload if isinstance(payload, list) else [payload]
    issues = []
    for item in items:
        if not isinstance(item, dict):
            raise TrackerError(f"unexpected bd output: {item!r}")
        issues.append(_to_issue(item))
    return issues


def _to_issue(raw: dict[str, Any]) -> Issue:
    """Build an :class:`~milhouse.models.Issue` from one bead."""
    return Issue(
        id=str(raw.get("id", "")),
        title=str(raw.get("title", "")),
        status=str(raw.get("status", "open")),
        issue_type=str(raw.get("issue_type", "task")),
        description=str(raw.get("description") or ""),
        assignee=raw.get("assignee") or None,
        parent=raw.get("parent") or None,
        priority=raw.get("priority") if isinstance(raw.get("priority"), int) else None,
        labels=[str(label) for label in raw.get("labels") or []],
        raw=raw,
    )
