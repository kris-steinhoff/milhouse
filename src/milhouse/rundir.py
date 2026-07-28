"""The run directory: turn artifacts and the run lock.

One repository gets one directory, ``.milhouse/runs/``, holding:

==============================  ============================================
``lock.json``                   Who is running in this repository right now.
``<issue-id>/``                 Every artifact of every attempt at one issue.
``<issue-id>/iter-NNN.prompt``  The exact prompt sent for iteration ``NNN``.
``<issue-id>/iter-NNN.term``    The pane transcript captured after it.
==============================  ============================================

That is all. milhouse used to keep ``state.json`` and ``events.jsonl`` here too,
and both moved to tools that already owned what was in them: the session facts to
``bd`` and herdr, the iteration history to the beads audit log
(:doc:`ADR 0021 <../../docs/decisions/0021-iteration-history-goes-in-the-beads-audit-log>`).
What survives is captured text with no other home — herdr's scrollback is live
and bounded, and gone once a pane is replaced.

The lock exists because reconciliation is destructive. Re-opening a claim that a
*live* run is working would have two milhouse processes driving one pane and
fighting over one issue
(:doc:`ADR 0015 <../../docs/decisions/0015-one-run-at-a-time>`).
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .errors import RunLockedError
from .models import now

__all__ = ["LOCK_FILENAME", "LockHolder", "RunLock", "ensure_run_dir"]

log = logging.getLogger(__name__)

LOCK_FILENAME = "lock.json"
IGNORE_FILENAME = ".gitignore"

IGNORE_BODY = """\
# Created by milhouse. Everything here is run bookkeeping, not source.
*
"""
"""What goes in ``.milhouse/runs/.gitignore``. ``*`` also ignores the file itself."""


def ensure_run_dir(run_dir: Path) -> Path:
    """Create a run directory that git does not see.

    This has to happen before anything is written into ``.milhouse/runs/``,
    because that directory lives inside the repository being worked on. Run
    bookkeeping that git can see is an uncommitted change, which shows up as a
    dirty working tree in the reading milhouse takes of its own turns.

    The marker ignores itself, so nobody has to commit anything for it to work,
    and a fresh clone gets one back on the next run. It goes inside the run
    directory rather than beside it, because the parent is ``.milhouse/``, which
    also holds the committed ``config.toml``.

    Args:
        run_dir: ``.milhouse/runs``. Created along with its parents.

    Returns:
        ``run_dir``, for chaining.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    marker = run_dir / IGNORE_FILENAME
    if not marker.exists():
        marker.write_text(IGNORE_BODY, encoding="utf-8")
    return run_dir


class LockHolder(BaseModel):
    """The process a lock file says is running in this repository.

    Attributes:
        pid: Process id of the holder.
        host: Hostname it was running on, because a pid means nothing off it.
        started_at: When the lock was taken.
    """

    pid: int
    host: str = Field(default_factory=socket.gethostname)
    started_at: str = Field(default_factory=lambda: now().isoformat())

    @property
    def is_live(self) -> bool:
        """Whether the holder still looks like a running process.

        Only decidable on the machine that took the lock: a pid from another
        host is treated as live, because guessing wrong in that direction merely
        refuses a run, while guessing wrong the other way corrupts one.
        """
        if self.host != socket.gethostname():
            return True
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # Most likely PermissionError: the pid exists and belongs to someone
            # else, which still counts as live.
            return True
        return True

    def describe(self) -> str:
        """One line naming the holder, for an error message."""
        return f"pid {self.pid} on {self.host}, since {self.started_at}"


class RunLock:
    """An advisory lock over one repository's run directory.

    Advisory in the usual sense: it stops milhouse from tripping over itself, not
    an unrelated process from writing to the same files.
    """

    def __init__(self, path: Path) -> None:
        """Bind to the lock file at ``path``.

        Args:
            path: Where ``lock.json`` lives.
        """
        self.path = path
        self._held = False

    def holder(self) -> LockHolder | None:
        """Whoever the lock file names, or ``None`` if there is nobody readable.

        An unparseable lock file counts as nobody. It is bookkeeping written by
        a previous milhouse, and refusing to run because it went bad would be
        worse than replacing it.
        """
        try:
            return LockHolder.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None

    def acquire(self) -> LockHolder | None:
        """Take the lock for this process.

        Returns:
            The stale holder this call displaced, or ``None`` when the lock was
            simply free. A caller that gets a holder back should say so: it
            means a previous run died without cleaning up.

        Raises:
            RunLockedError: A live process already holds it.
        """
        ensure_run_dir(self.path.parent)
        stale: LockHolder | None = None
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                existing = self.holder()
                if existing is not None and existing.is_live:
                    raise RunLockedError(
                        f"another milhouse run is working this repository ({existing.describe()})"
                    ) from None
                stale = existing
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(LockHolder(pid=os.getpid()).model_dump_json() + "\n")
            self._held = True
            return stale
        raise RunLockedError(f"could not take the run lock at {self.path}")

    def release(self) -> None:
        """Drop the lock, if this process took it. Safe to call twice."""
        if not self._held:
            return
        self._held = False
        self.path.unlink(missing_ok=True)
