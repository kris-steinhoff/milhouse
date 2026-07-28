"""Iteration history, kept in the beads audit log.

``bd audit record`` appends one JSON object per line to
``.beads/interactions.jsonl``, for exactly the question an iteration record
answers: why did the agent do that. bd already writes a ``field_change`` entry
there on every issue mutation, so putting milhouse's iterations in the same file
gives one ordered trail rather than two
(:doc:`ADR 0021 <../../docs/decisions/0021-iteration-history-goes-in-the-beads-audit-log>`).

Two entry kinds are written here:

===============  =========================================================
``claim``        milhouse took an issue and is about to work it.
``iteration``    The turn is over, and this is what it achieved.
===============  =========================================================

A ``claim`` with no ``iteration`` after it is an unfinished turn, which is how a
run killed with ``SIGKILL`` is recovered from
(:doc:`ADR 0008 <../../docs/decisions/0008-crash-recovery-by-reconciliation>`).

**Entries stay small.** ``interactions.jsonl`` has many concurrent writers —
every agent's own ``bd close`` appends from its own process — and POSIX
guarantees an atomic append only below ``PIPE_BUF``. Everything written here is
a few hundred bytes, so the verification output stays on the ``bd`` note and in
the transcript, and the entry carries the verdict plus a path.

Reading is done by hand, because ``bd audit`` has ``record`` and ``label`` and no
query. That means parsing a file milhouse does not own, against a schema it does
not control, which is the cost the ADR accepted.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import proc
from .errors import MilhouseError
from .models import Iteration

__all__ = ["CLAIM", "ITERATION", "MAX_COMMITS", "AuditLog"]

log = logging.getLogger(__name__)

CLAIM = "claim"
"""Entry kind for an issue milhouse has taken and not yet settled."""

ITERATION = "iteration"
"""Entry kind for a finished turn."""

MAX_COMMITS = 20
"""Shas an entry may carry.

A prolific turn must not be the line that exceeds ``PIPE_BUF`` and tears. The
count is recorded separately, so a truncated list still reports honestly.
"""

TIMEOUT = 120.0
"""Seconds a single ``bd audit`` call may take. Matches the tracker's."""

LOG_RELPATH = Path(".beads/interactions.jsonl")
"""Where ``bd audit record`` appends, relative to the repository root."""

_RECORDED = (
    "number",
    "outcome",
    "detail",
    "agent_state",
    "head_before",
    "head_after",
    "attributed",
    "dirty_after",
    "verified",
    "started_at",
    "ended_at",
    "prompt_path",
    "transcript_path",
)
"""Iteration fields that go into an entry's ``extra``.

``issue_id`` is absent because the entry has a field of its own for it, which is
the one bd itself indexes on. ``verification_output`` is absent for a different
reason: it is a tail of test output, it is the one field with no bound, and it
already has two homes — the ``bd`` note carrying the failure to the next agent,
and the transcript on disk.
"""


class AuditLog:
    """milhouse's slice of one repository's beads audit trail."""

    def __init__(self, repo_root: Path) -> None:
        """Bind to the trail of the beads database under ``repo_root``.

        Args:
            repo_root: The repository whose ``.beads`` directory holds the log.
        """
        self.repo_root = repo_root

    @property
    def path(self) -> Path:
        """Where the entries are appended. Not created by milhouse."""
        return self.repo_root / LOG_RELPATH

    # -- writing ----------------------------------------------------------

    def claimed(self, issue_id: str) -> None:
        """Record that milhouse has taken ``issue_id`` and is about to work it."""
        self._record(CLAIM, issue_id, {})

    def record(self, iteration: Iteration) -> None:
        """Record a finished turn.

        Args:
            iteration: The iteration that just ended, already classified.
        """
        self._record(ITERATION, iteration.issue_id, _extra(iteration))

    def _record(self, kind: str, issue_id: str, extra: dict[str, Any]) -> None:
        """Append one entry, tolerating a bd that will not take it.

        A failed write loses a history line. Raising instead would lose the turn
        it describes, which has already happened and cannot be re-run.
        """
        payload = json.dumps({"kind": kind, "issue_id": issue_id, "extra": extra})
        try:
            proc.run(
                ["bd", "-C", str(self.repo_root), "audit", "record", "--stdin"],
                stdin=payload,
                timeout=TIMEOUT,
            )
        except MilhouseError as exc:
            log.warning("could not record the %s entry for %s: %s", kind, issue_id, exc)

    # -- reading ----------------------------------------------------------

    def iterations(self) -> list[Iteration]:
        """Every iteration recorded in this repository, oldest first.

        A line that will not parse is skipped rather than raising. This trail is
        what a post-mortem reads, and half a history beats a traceback.
        """
        found = []
        for entry in self._entries():
            if entry.get("kind") != ITERATION:
                continue
            iteration = _to_iteration(entry)
            if iteration is not None:
                found.append(iteration)
        return found

    def iterations_for(self, issue_id: str) -> list[Iteration]:
        """Every recorded iteration that worked ``issue_id``, oldest first."""
        return [item for item in self.iterations() if item.issue_id == issue_id]

    def next_number(self) -> int:
        """The number the next iteration gets.

        Counts the whole trail rather than this invocation, because the number
        names the artifact files and those have to stay unique across resumes.
        """
        return len(self.iterations()) + 1

    def unsettled_claims(self) -> list[str]:
        """Issues milhouse claimed and never recorded an iteration for.

        A turn writes a ``claim`` entry before it starts and an ``iteration``
        entry when it ends, so a claim with nothing after it is a run that died
        mid-turn. The issue is still ``in_progress`` in ``bd``, which excludes it
        from ``bd ready`` forever, so somebody has to re-open it.

        Returns:
            Issue ids, oldest claim first.
        """
        pending: dict[str, None] = {}
        for entry in self._entries():
            issue_id = str(entry.get("issue_id") or "")
            if not issue_id:
                continue
            if entry.get("kind") == CLAIM:
                pending[issue_id] = None
            elif entry.get("kind") == ITERATION:
                pending.pop(issue_id, None)
        return list(pending)

    def _entries(self) -> list[dict[str, Any]]:
        """Every readable line of the trail, oldest first.

        Only milhouse's own kinds are of interest, but the file is bd's, so a
        line whose shape is unfamiliar is somebody else's entry rather than a
        problem.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        entries = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries


def _extra(iteration: Iteration) -> dict[str, Any]:
    """The recorded fields of an iteration, small enough to append atomically."""
    payload = json.loads(iteration.model_dump_json(include=set(_RECORDED)))
    payload["commits"] = iteration.commits[:MAX_COMMITS]
    payload["commit_count"] = len(iteration.commits)
    return payload


def _to_iteration(entry: dict[str, Any]) -> Iteration | None:
    """Rebuild an :class:`~milhouse.models.Iteration` from an entry, or skip it.

    The issue comes off the entry rather than out of ``extra``, because that is
    where it was written: bd's own field is the one thing in the schema that
    already means "which issue is this about".
    """
    extra = entry.get("extra")
    if not isinstance(extra, dict):
        return None
    try:
        return Iteration.model_validate({**extra, "issue_id": entry.get("issue_id")})
    except ValueError as exc:
        log.warning("skipping an unreadable iteration entry: %s", exc)
        return None
