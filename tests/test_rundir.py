"""Tests for the run directory: the lock, and keeping git out of it."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from milhouse.errors import RunLockedError
from milhouse.rundir import LOCK_FILENAME, LockHolder, RunLock, ensure_run_dir


@dataclass
class Run:
    """A run directory and the lock over it, the pair every test here needs."""

    run_dir: Path
    lock: RunLock


@pytest.fixture
def run(tmp_path: Path) -> Run:
    run_dir = tmp_path / ".milhouse" / "runs"
    return Run(run_dir=run_dir, lock=RunLock(run_dir / LOCK_FILENAME))


# -- the lock ------------------------------------------------------------------


def test_the_lock_is_taken_and_released(run: Run) -> None:
    assert run.lock.acquire() is None
    assert run.lock.path.exists()

    run.lock.release()

    assert not run.lock.path.exists()


def test_a_live_holder_refuses_a_second_run(run: Run) -> None:
    """Two runs in one repository would reconcile each other's in-flight claim."""
    run.lock.acquire()

    with pytest.raises(RunLockedError, match=f"pid {os.getpid()}"):
        RunLock(run.lock.path).acquire()


def test_a_dead_holder_is_stolen_and_reported(run: Run) -> None:
    run.run_dir.mkdir(parents=True)
    dead = LockHolder(pid=_unused_pid())
    run.lock.path.write_text(dead.model_dump_json(), encoding="utf-8")

    stale = run.lock.acquire()

    assert stale is not None
    assert stale.pid == dead.pid


def test_an_unreadable_lock_is_replaced_rather_than_obeyed(run: Run) -> None:
    run.run_dir.mkdir(parents=True)
    run.lock.path.write_text("half a lock file", encoding="utf-8")

    assert run.lock.acquire() is None
    assert run.lock.holder() is not None


def test_a_holder_on_another_host_counts_as_live(run: Run) -> None:
    """A pid means nothing off the machine that wrote it, so assume the worst."""
    run.run_dir.mkdir(parents=True)
    run.lock.path.write_text(
        LockHolder(pid=_unused_pid(), host="somewhere-else").model_dump_json(), encoding="utf-8"
    )

    with pytest.raises(RunLockedError, match="somewhere-else"):
        run.lock.acquire()


def test_releasing_a_lock_this_process_never_took_is_a_no_op(run: Run) -> None:
    run.run_dir.mkdir(parents=True)
    run.lock.path.write_text(LockHolder(pid=1).model_dump_json(), encoding="utf-8")

    RunLock(run.lock.path).release()

    assert run.lock.path.exists()


# -- keeping git out of it -----------------------------------------------------


def test_the_runs_directory_ignores_itself(run: Run) -> None:
    """Without this, the lock file is an uncommitted change in the repo.

    A run directory lives inside the repository being worked on, and milhouse
    reads its own turns partly on whether the working tree is dirty, so its
    bookkeeping would show up in the reading it takes.
    """
    run.lock.acquire()

    marker = run.run_dir / ".gitignore"
    assert marker.read_text(encoding="utf-8").splitlines()[-1] == "*"


def test_the_marker_goes_inside_the_run_directory(run: Run) -> None:
    """Its parent is `.milhouse/`, which also holds the committed config file."""
    ensure_run_dir(run.run_dir)

    assert (run.run_dir / ".gitignore").exists()
    assert not (run.run_dir.parent / ".gitignore").exists()


def test_the_marker_is_written_once_and_not_rewritten(run: Run) -> None:
    ensure_run_dir(run.run_dir)
    marker = run.run_dir / ".gitignore"
    marker.write_text("# edited by hand\n*\n", encoding="utf-8")

    ensure_run_dir(run.run_dir)

    assert marker.read_text(encoding="utf-8") == "# edited by hand\n*\n"


def _unused_pid() -> int:
    """A pid nothing is running under, for a lock that should read as stale."""
    for candidate in range(4_000_000, 4_000_100):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    raise AssertionError("no free pid to test with")
