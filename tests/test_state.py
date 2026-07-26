"""Tests for the run directory: state, the event log, and the lock."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from milhouse.errors import RunLockedError
from milhouse.models import Iteration, RunState
from milhouse.state import LockHolder, RunStore


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs" / "hello")


def iteration(number: int, *, issue_id: str = "bd-e.1", detail: str = "") -> Iteration:
    """One recorded iteration, with only the fields these tests care about."""
    return Iteration(number=number, issue_id=issue_id, outcome="success", detail=detail)


# -- session state -------------------------------------------------------------


def test_state_round_trips_through_disk(store: RunStore) -> None:
    store.save(RunState(task_id="file:docs/tasks/hello.md", task_slug="hello", epic_id="bd-9"))

    loaded = store.load()

    assert loaded is not None
    assert loaded.epic_id == "bd-9"
    assert not store.state_path.with_suffix(".json.tmp").exists()


def test_loading_a_missing_state_returns_none(store: RunStore) -> None:
    assert store.load() is None


def test_a_version_1_state_file_still_loads(store: RunStore) -> None:
    """The history and attempt counts are gone, but the session facts are not."""
    store.run_dir.mkdir(parents=True)
    store.state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "task_id": "file:hello.md",
                "task_slug": "hello",
                "epic_id": "bd-9",
                "attempts": {"bd-9.1": 2},
                "iterations": [{"number": 1, "issue_id": "bd-9.1", "outcome": "stalled"}],
            }
        ),
        encoding="utf-8",
    )

    loaded = store.load()

    assert loaded is not None
    assert loaded.epic_id == "bd-9"


# -- the event log -------------------------------------------------------------


def test_the_event_log_appends_rather_than_rewrites(store: RunStore) -> None:
    store.append(iteration(1, detail="first"))
    store.append(iteration(2, detail="second"))

    assert [item.detail for item in store.history()] == ["first", "second"]
    assert store.events_path.read_text(encoding="utf-8").count("\n") == 2


def test_history_can_be_filtered_to_one_issue(store: RunStore) -> None:
    store.append(iteration(1, issue_id="bd-e.1"))
    store.append(iteration(2, issue_id="bd-e.2"))
    store.append(iteration(3, issue_id="bd-e.1"))

    assert [item.number for item in store.history_for("bd-e.1")] == [1, 3]


def test_a_corrupt_line_is_skipped_rather_than_raising(store: RunStore) -> None:
    """Half a history beats a traceback: this log is what a post-mortem reads."""
    store.append(iteration(1, detail="good"))
    with store.events_path.open("a", encoding="utf-8") as stream:
        stream.write("{not json at all\n")
    store.append(iteration(3, detail="also good"))

    assert [item.detail for item in store.history()] == ["good", "also good"]


def test_iteration_numbers_keep_counting_across_invocations(store: RunStore) -> None:
    """The number names iter-NNN.prompt, so it cannot restart at 1 on a resume."""
    assert store.next_number() == 1
    store.append(iteration(1))
    store.append(iteration(2))
    assert store.next_number() == 3


# -- the lock ------------------------------------------------------------------


def test_the_lock_is_taken_and_released(store: RunStore) -> None:
    assert store.lock.acquire() is None
    assert store.lock.path.exists()

    store.lock.release()

    assert not store.lock.path.exists()


def test_a_live_holder_refuses_a_second_run(store: RunStore) -> None:
    """Two runs over one task would reconcile each other's in-flight claim."""
    store.lock.acquire()

    with pytest.raises(RunLockedError, match=f"pid {os.getpid()}"):
        RunStore(store.run_dir).lock.acquire()


def test_a_dead_holder_is_stolen_and_reported(store: RunStore) -> None:
    store.run_dir.mkdir(parents=True)
    dead = LockHolder(pid=_unused_pid())
    store.lock.path.write_text(dead.model_dump_json(), encoding="utf-8")

    stale = store.lock.acquire()

    assert stale is not None
    assert stale.pid == dead.pid


def test_an_unreadable_lock_is_replaced_rather_than_obeyed(store: RunStore) -> None:
    store.run_dir.mkdir(parents=True)
    store.lock.path.write_text("half a lock file", encoding="utf-8")

    assert store.lock.acquire() is None
    assert store.lock.holder() is not None


def test_a_holder_on_another_host_counts_as_live(store: RunStore) -> None:
    """A pid means nothing off the machine that wrote it, so assume the worst."""
    store.run_dir.mkdir(parents=True)
    store.lock.path.write_text(
        LockHolder(pid=_unused_pid(), host="somewhere-else").model_dump_json(), encoding="utf-8"
    )

    with pytest.raises(RunLockedError, match="somewhere-else"):
        store.lock.acquire()


def test_releasing_a_lock_this_process_never_took_is_a_no_op(store: RunStore) -> None:
    store.run_dir.mkdir(parents=True)
    store.lock.path.write_text(LockHolder(pid=1).model_dump_json(), encoding="utf-8")

    RunStore(store.run_dir).lock.release()

    assert store.lock.path.exists()


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
