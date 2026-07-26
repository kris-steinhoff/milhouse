"""The run directory: session state, the event log, and the run lock.

One task gets one directory under ``.milhouse/runs/<task_slug>/``, and
:class:`RunStore` owns everything in it:

======================  ====================================================
``state.json``          Session facts. Small, rewritten, atomic.
``events.jsonl``        One :class:`~milhouse.models.Iteration` per line,
                        append-only. The history and the post-mortem log.
``lock.json``           Who is running this task right now.
``iter-NNN.prompt``     The exact prompt sent for iteration ``NNN``.
``iter-NNN.term``       The pane transcript captured after it.
======================  ====================================================

Splitting the history out of ``state.json`` is what makes the state file small
enough to rewrite safely on every save, and it gives post-mortems a log to read
rather than a document to diff
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).

The lock exists because reconciliation is destructive. Re-opening a claim that a
*live* run is working would have two milhouse processes driving one pane and
fighting over one issue
(:doc:`ADR 0015 <../../docs/decisions/0015-one-run-at-a-time>`).
"""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .errors import RunLockedError
from .models import Iteration, RunState, now

__all__ = ["LockHolder", "RunLock", "RunStore"]

log = logging.getLogger(__name__)

STATE_FILENAME = "state.json"
EVENTS_FILENAME = "events.jsonl"
LOCK_FILENAME = "lock.json"


class LockHolder(BaseModel):
    """The process a lock file says is running this task.

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
    """An advisory lock over one task's run directory.

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stale: LockHolder | None = None
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                existing = self.holder()
                if existing is not None and existing.is_live:
                    raise RunLockedError(
                        f"another milhouse run holds {self.path.parent.name} "
                        f"({existing.describe()})"
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


class RunStore:
    """Reads and writes one task's run directory."""

    def __init__(self, run_dir: Path) -> None:
        """Bind to a run directory, which need not exist yet.

        Args:
            run_dir: ``.milhouse/runs/<task_slug>``.
        """
        self.run_dir = run_dir
        self.lock = RunLock(run_dir / LOCK_FILENAME)

    @property
    def state_path(self) -> Path:
        """Where the session facts live."""
        return self.run_dir / STATE_FILENAME

    @property
    def events_path(self) -> Path:
        """Where the append-only iteration log lives."""
        return self.run_dir / EVENTS_FILENAME

    # -- session state ----------------------------------------------------

    def load(self) -> RunState | None:
        """Read ``state.json``, returning ``None`` when this task has no run yet.

        Raises:
            ValidationError: The file exists but is not a run state.
        """
        if not self.state_path.exists():
            return None
        return RunState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def save(self, state: RunState) -> None:
        """Write ``state`` atomically, creating the run directory if needed.

        The write goes to a sibling temporary file and is then renamed, so a
        crash mid-write cannot leave a truncated ``state.json`` behind.

        Args:
            state: The state to persist. Its ``updated_at`` is refreshed.
        """
        state.updated_at = now()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = json.loads(state.model_dump_json())
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.state_path)

    # -- the event log ----------------------------------------------------

    def append(self, iteration: Iteration) -> None:
        """Append one iteration to the event log.

        Args:
            iteration: The iteration that just ended.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(iteration.model_dump_json() + "\n")

    def history(self) -> list[Iteration]:
        """Every iteration recorded for this task, oldest first.

        A line that will not parse is skipped with a warning rather than raising.
        This log is what a post-mortem reads, and half a history beats a
        traceback.
        """
        if not self.events_path.exists():
            return []
        iterations: list[Iteration] = []
        for number, line in enumerate(
            self.events_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                iterations.append(Iteration.model_validate_json(line))
            except ValidationError as exc:
                log.warning("skipping %s line %d: %s", self.events_path, number, exc)
        return iterations

    def history_for(self, issue_id: str) -> list[Iteration]:
        """Every recorded iteration that worked ``issue_id``, oldest first."""
        return [item for item in self.history() if item.issue_id == issue_id]

    def next_number(self) -> int:
        """The number the next iteration gets.

        Counts the whole log rather than this invocation, because the number
        names the artifact files and those have to stay unique across resumes.
        """
        if not self.events_path.exists():
            return 1
        with self.events_path.open(encoding="utf-8") as stream:
            return sum(1 for line in stream if line.strip()) + 1
