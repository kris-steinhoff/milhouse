"""Tests for the iteration history in the beads audit log.

Two things matter beyond round-tripping a record: the entry has to be small
enough that a concurrent append cannot tear it, and reading has to survive a file
milhouse does not own — one bd writes its own kinds into, and that a partial
write can leave a broken line in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from milhouse.audit import MAX_COMMITS, MAX_CONFLICTS, AuditLog
from milhouse.models import Iteration, MergeRecord

from .fakes import FakeProc, Reply

SAFE_ENTRY_BYTES = 1_000
"""What "a few hundred bytes" is allowed to mean.

POSIX guarantees an atomic append only below ``PIPE_BUF``, which is 4096 on
Linux, and every agent's own `bd close` appends to this file from its own
process. The margin is deliberate.
"""


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    (tmp_path / ".beads").mkdir()
    return AuditLog(tmp_path)


def iteration(number: int = 1, *, issue_id: str = "bd-e.1", **fields: Any) -> Iteration:
    """One classified iteration, with only the fields a test cares about."""
    fields.setdefault("outcome", "success")
    return Iteration(number=number, issue_id=issue_id, **fields)


def written(fake_proc: FakeProc) -> dict:
    """The JSON payload the last `bd audit record` was handed on stdin."""
    return json.loads(fake_proc.stdins[-1] or "")


# -- writing -------------------------------------------------------------------


def test_an_iteration_is_recorded_through_bd(audit: AuditLog, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="int-abc\n"))

    audit.record(iteration(7, detail="bd-e.1 closed in beads"))

    argv = fake_proc.calls[0]
    assert argv[:3] == ("bd", "-C", str(audit.repo_root))
    assert argv[3:] == ("audit", "record", "--stdin")
    payload = written(fake_proc)
    assert payload["kind"] == "iteration"
    assert payload["issue_id"] == "bd-e.1"
    assert payload["extra"]["detail"] == "bd-e.1 closed in beads"


def test_a_claim_is_recorded_before_the_turn(audit: AuditLog, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="int-abc\n"))

    audit.claimed("bd-e.1")

    payload = written(fake_proc)
    assert payload["kind"] == "claim"
    assert payload["issue_id"] == "bd-e.1"


def test_the_verification_output_is_not_recorded(audit: AuditLog, fake_proc: FakeProc) -> None:
    """It is the one unbounded field, and it already lives on the bd note."""
    fake_proc.expect("bd", Reply(stdout="int-abc\n"))

    audit.record(iteration(verified=False, verification_output="FAILED " * 500))

    extra = written(fake_proc)["extra"]
    assert extra["verified"] is False
    assert "verification_output" not in extra


def test_a_prolific_turn_does_not_make_a_long_entry(audit: AuditLog, fake_proc: FakeProc) -> None:
    """The shas are capped, but the count is not, so it still reports honestly."""
    fake_proc.expect("bd", Reply(stdout="int-abc\n"))

    audit.record(iteration(commits=[f"sha{n:04d}" for n in range(200)]))

    extra = written(fake_proc)["extra"]
    assert len(extra["commits"]) == MAX_COMMITS
    assert extra["commit_count"] == 200


def test_a_conflict_in_many_files_does_not_make_a_long_entry(
    audit: AuditLog, fake_proc: FakeProc
) -> None:
    """A path is longer than a sha, and there is no bound on how many conflict."""
    fake_proc.expect("bd", Reply(stdout="int-abc\n"))

    audit.record(
        iteration(
            merge=MergeRecord(
                source="milhouse/bd-e/bd-e.1",
                target="milhouse/bd-e",
                conflicts=[f"src/milhouse/module_{n:03d}.py" for n in range(200)],
            )
        )
    )

    extra = written(fake_proc)["extra"]
    assert len(extra["merge"]["conflicts"]) == MAX_CONFLICTS
    assert extra["merge"]["conflict_count"] == 200
    assert len(fake_proc.stdins[-1] or "") < SAFE_ENTRY_BYTES


def test_a_merge_survives_the_round_trip(audit: AuditLog) -> None:
    """What .8 reads to decide whether the integration branch needs verifying."""
    audit.path.write_text(
        json.dumps(
            {
                "kind": "iteration",
                "issue_id": "bd-e.1",
                "extra": {
                    "number": 1,
                    "outcome": "success",
                    "merge": {
                        "source": "milhouse/bd-e/bd-e.1",
                        "target": "milhouse/bd-e",
                        "sha": "c" * 40,
                        "fast_forwarded": False,
                        "conflicts": [],
                        "conflict_count": 0,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    merge = audit.iterations()[0].merge

    assert merge is not None
    assert merge.joined
    assert merge.landed
    assert merge.source == "milhouse/bd-e/bd-e.1"


def test_an_entry_stays_small_enough_to_append_atomically(
    audit: AuditLog, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="int-abc\n"))

    audit.record(
        iteration(
            999,
            outcome="rejected",
            detail="bd-e.1 was closed but `uv run pytest -m 'not herdr and not beads'` failed",
            agent_state="done",
            head_before="a" * 40,
            head_after="b" * 40,
            commits=[f"sha{n:04d}" for n in range(MAX_COMMITS)],
            attributed=True,
            dirty_after=True,
            verified=False,
            verification_output="x" * 2_000,
            prompt_path=".milhouse/runs/bd-e.1/iter-999.prompt",
            transcript_path=".milhouse/runs/bd-e.1/iter-999.term",
        )
    )

    assert len(fake_proc.stdins[-1] or "") < SAFE_ENTRY_BYTES


def test_a_bd_that_will_not_take_the_entry_does_not_lose_the_turn(
    audit: AuditLog, fake_proc: FakeProc
) -> None:
    """The turn already happened. Losing a history line beats raising over it."""
    fake_proc.expect("bd", Reply(stderr="no beads database found\n", returncode=1))

    audit.record(iteration())


# -- reading -------------------------------------------------------------------


def append(audit: AuditLog, *entries: dict) -> None:
    """Write entries to the trail the way bd would."""
    with audit.path.open("a", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(json.dumps(entry) + "\n")


def entry(kind: str, issue_id: str, **extra: object) -> dict:
    return {"kind": kind, "issue_id": issue_id, "extra": extra}


def recorded(number: int, issue_id: str, **extra: object) -> dict:
    return entry("iteration", issue_id, number=number, outcome="success", **extra)


def test_iterations_are_read_back_oldest_first(audit: AuditLog) -> None:
    append(
        audit,
        recorded(1, "bd-e.1", detail="first"),
        recorded(2, "bd-e.2", detail="second"),
    )

    assert [item.detail for item in audit.iterations()] == ["first", "second"]


def test_bd_s_own_entries_are_not_iterations(audit: AuditLog) -> None:
    """This file is shared with bd, which writes a field_change per mutation."""
    append(
        audit,
        {"kind": "field_change", "issue_id": "bd-e.1", "extra": {"field": "status"}},
        recorded(1, "bd-e.1", detail="mine"),
    )

    assert [item.detail for item in audit.iterations()] == ["mine"]


def test_a_torn_line_is_skipped_rather_than_raising(audit: AuditLog) -> None:
    """Half a history beats a traceback: this trail is what a post-mortem reads."""
    append(audit, recorded(1, "bd-e.1", detail="good"))
    with audit.path.open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "iteration", "extra"\n')
    append(audit, recorded(3, "bd-e.1", detail="also good"))

    assert [item.detail for item in audit.iterations()] == ["good", "also good"]


def test_an_iteration_entry_missing_its_fields_is_skipped(audit: AuditLog) -> None:
    append(audit, {"kind": "iteration", "issue_id": "bd-e.1", "extra": {"number": 1}})

    assert audit.iterations() == []


def test_history_can_be_filtered_to_one_issue(audit: AuditLog) -> None:
    append(
        audit,
        recorded(1, "bd-e.1"),
        recorded(2, "bd-e.2"),
        recorded(3, "bd-e.1"),
    )

    assert [item.number for item in audit.iterations_for("bd-e.1")] == [1, 3]


def test_iteration_numbers_keep_counting_across_invocations(audit: AuditLog) -> None:
    """The number names iter-NNN.prompt, so it cannot restart at 1 on a resume."""
    assert audit.next_number() == 1
    append(audit, recorded(1, "bd-e.1"), recorded(2, "bd-e.2"))

    assert audit.next_number() == 3


def test_a_missing_trail_reads_as_no_history(tmp_path: Path) -> None:
    assert AuditLog(tmp_path).iterations() == []


# -- unsettled claims ----------------------------------------------------------


def test_a_claim_with_no_iteration_after_it_is_unsettled(audit: AuditLog) -> None:
    """A run killed mid-turn leaves the issue in_progress and out of bd ready."""
    append(audit, entry("claim", "bd-e.1"))

    assert audit.unsettled_claims() == ["bd-e.1"]


def test_a_claim_the_turn_finished_is_settled(audit: AuditLog) -> None:
    append(audit, entry("claim", "bd-e.1"), recorded(1, "bd-e.1"))

    assert audit.unsettled_claims() == []


def test_a_reclaimed_issue_is_unsettled_again(audit: AuditLog) -> None:
    append(
        audit,
        entry("claim", "bd-e.1"),
        recorded(1, "bd-e.1"),
        entry("claim", "bd-e.1"),
    )

    assert audit.unsettled_claims() == ["bd-e.1"]


def test_bd_s_own_entries_never_look_like_a_claim(audit: AuditLog) -> None:
    append(audit, {"kind": "field_change", "issue_id": "bd-e.1", "extra": {}})

    assert audit.unsettled_claims() == []
