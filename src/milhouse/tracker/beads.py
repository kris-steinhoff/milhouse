"""The ``bd`` implementation of :class:`~milhouse.tracker.base.Tracker`.

Every method is one or two ``bd`` invocations through :mod:`milhouse.proc`. The
only cleverness is in :meth:`BeadsTracker.create_children`, which has to create
issues before it can wire dependencies between them.

Three of the loop's questions are answered by ``bd`` directly, with no state of
milhouse's own (:doc:`ADR 0002 <../../docs/decisions/0002-link-issues-via-bead-metadata>`):

- decomposed already? ``bd list --metadata-field <key>=<task_id> --type epic``
- what next? ``bd ready --parent <epic> --claim --limit 1``
- done? the same ``bd ready`` returns an empty list
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import proc
from ..config import TrackerConfig
from ..errors import MilhouseError, TrackerError
from ..models import Issue, TaskDefinition
from .base import PlannedIssue

__all__ = ["BeadsTracker"]

TIMEOUT = 120.0
"""Seconds any single ``bd`` call may take. Generous: Dolt can be slow to start."""


class BeadsTracker:
    """Talks to the beads database in one repository."""

    def __init__(self, repo_root: Path, config: TrackerConfig | None = None) -> None:
        """Bind to the beads database discovered from ``repo_root``.

        Args:
            repo_root: Repository whose ``.beads`` database to use.
            config: Label and metadata key settings. Defaults apply if omitted.
        """
        self.repo_root = repo_root
        self.config = config or TrackerConfig()

    # -- queries ---------------------------------------------------------

    def find_epic(self, task: TaskDefinition) -> Issue | None:
        """Find the epic carrying ``task``'s id in its metadata."""
        issues = self._issues(
            [
                "list",
                "--metadata-field",
                f"{self.config.metadata_key}={task.task_id}",
                "--type",
                "epic",
                "--all",
                "--limit",
                "0",
            ]
        )
        return issues[0] if issues else None

    def get(self, issue_id: str) -> Issue:
        """Read one issue back.

        Raises:
            TrackerError: No issue with that id exists.
        """
        issues = self._issues(["show", issue_id])
        if not issues:
            raise TrackerError(f"no such issue: {issue_id}")
        return issues[0]

    def children(self, epic_id: str) -> list[Issue]:
        """Every issue under ``epic_id``, closed ones included."""
        return self._issues(["list", "--parent", epic_id, "--all", "--limit", "0"])

    def ready(self, epic_id: str, *, claim: bool) -> Issue | None:
        """Return the next ready issue under ``epic_id``, optionally claiming it.

        ``bd ready`` already excludes in-progress, blocked, and deferred issues,
        and ``--claim`` makes taking one atomic, so two loops over the same epic
        cannot pick the same issue.
        """
        argv = ["ready", "--parent", epic_id, "--limit", "1"]
        if claim:
            argv.append("--claim")
        issues = self._issues(argv)
        return issues[0] if issues else None

    # -- mutations -------------------------------------------------------

    def create_epic(self, task: TaskDefinition) -> Issue:
        """Create the epic for ``task``, tagged so :meth:`find_epic` finds it.

        The metadata and label are applied here, by milhouse, and never by the
        planning agent — that is what keeps an agent from forging an epic the
        loop would then pick up
        (:doc:`ADR 0006 <../../docs/decisions/0006-planning-agent-proposes-milhouse-creates>`).
        """
        argv = [
            "create",
            task.title,
            "--type",
            "epic",
            "--labels",
            self.config.label,
            "--metadata",
            json.dumps({self.config.metadata_key: task.task_id}),
            "--description",
            _epic_description(task),
        ]
        if task.external_ref:
            argv += ["--external-ref", task.external_ref]
        return self._one(argv, what="epic")

    def create_children(self, epic_id: str, issues: list[PlannedIssue]) -> list[Issue]:
        """Create every planned issue under ``epic_id``, then wire dependencies.

        Two passes rather than one: dependencies are expressed between plan keys,
        and a bead id only exists after creation, so nothing can be wired until
        everything exists.

        Raises:
            TrackerError: A ``bd`` call failed, or a ``blocked_by`` key names an
                issue that is not in ``issues``.
        """
        created: list[Issue] = []
        by_key: dict[str, str] = {}
        for planned in issues:
            argv = [
                "create",
                planned.title,
                "--parent",
                epic_id,
                "--type",
                planned.type,
            ]
            if planned.priority is not None:
                argv += ["--priority", str(planned.priority)]
            if planned.description:
                argv += ["--description", planned.description]
            if planned.acceptance:
                argv += ["--acceptance", planned.acceptance]
            issue = self._one(argv, what="issue")
            # `bd create` echoes the new bead without the parent it was given,
            # so fill it in from what we asked for rather than re-reading.
            created.append(issue.model_copy(update={"parent": epic_id}))
            by_key[planned.key] = issue.id

        for planned in issues:
            for blocker in planned.blocked_by:
                if blocker not in by_key:
                    raise TrackerError(
                        f"plan issue {planned.key!r} is blocked by unknown key {blocker!r}"
                    )
                self._run(["dep", "add", by_key[planned.key], by_key[blocker]])
        return created

    def release(self, issue_id: str, *, note: str | None = None) -> None:
        """Return a claimed issue to the open, unassigned pool."""
        if note:
            self.note(issue_id, note)
        self._run(["update", issue_id, "--status", "open", "--assignee", ""])

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

    def _one(self, args: list[str], *, what: str) -> Issue:
        """Run a ``bd`` command expected to create or return exactly one issue.

        Raises:
            TrackerError: ``bd`` returned nothing usable.
        """
        payload = self._json(args)
        issues = _parse_issues(payload)
        if not issues:
            raise TrackerError(f"bd did not report a created {what}")
        return issues[0]


def _parse_issues(payload: Any) -> list[Issue]:
    """Normalise ``bd``'s JSON into :class:`~milhouse.models.Issue` values.

    ``bd`` returns a bare object from ``create`` and a list from ``list``,
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


def _epic_description(task: TaskDefinition) -> str:
    """Body for the epic: a pointer back to the task definition, not a copy of it.

    The full text goes to the agent in the prompt each iteration; duplicating it
    into the epic would just be a second copy to drift.
    """
    lines = [f"Decomposition of the milhouse task `{task.task_id}`."]
    if task.url:
        lines.append(f"\nSource: {task.url}")
    return "\n".join(lines)
