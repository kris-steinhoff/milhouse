"""Tests for the subprocess chokepoint: error mapping and JSON handling."""

from __future__ import annotations

import pytest

from milhouse import proc
from milhouse.errors import ProcessError

from .fakes import FakeProc, Reply


def test_run_returns_captured_output(fake_proc: FakeProc) -> None:
    fake_proc.expect("bd version", Reply(stdout="bd version 1.1.0\n"))

    result = proc.run(["bd", "version"])

    assert result.ok
    assert result.stdout == "bd version 1.1.0\n"
    assert result.argv == ("bd", "version")


def test_run_raises_on_nonzero_with_stderr_in_the_message(fake_proc: FakeProc) -> None:
    fake_proc.expect("bd list", Reply(stderr="no database found\n", returncode=1))

    with pytest.raises(ProcessError) as caught:
        proc.run(["bd", "list"])

    assert "exited 1" in str(caught.value)
    assert "no database found" in str(caught.value)
    assert caught.value.returncode == 1
    assert caught.value.exit_code == 8


def test_run_can_tolerate_a_nonzero_exit(fake_proc: FakeProc) -> None:
    fake_proc.expect("git show-ref", Reply(returncode=1))

    result = proc.run(["git", "show-ref"], check=False)

    assert not result.ok


def test_run_json_parses_stdout(fake_proc: FakeProc) -> None:
    fake_proc.expect("bd list", Reply(stdout='[{"id": "bd-1"}]'))

    assert proc.run_json(["bd", "list"]) == [{"id": "bd-1"}]


def test_run_json_rejects_invalid_json(fake_proc: FakeProc) -> None:
    fake_proc.expect("bd list", Reply(stdout="not json"))

    with pytest.raises(ProcessError, match="invalid JSON"):
        proc.run_json(["bd", "list"])


def test_run_json_rejects_empty_output_by_default(fake_proc: FakeProc) -> None:
    fake_proc.expect("bd list", Reply(stdout="   \n"))

    with pytest.raises(ProcessError, match="no JSON output"):
        proc.run_json(["bd", "list"])


def test_run_json_allows_empty_output_when_asked(fake_proc: FakeProc) -> None:
    fake_proc.expect("herdr pane run", Reply(stdout=""))

    assert proc.run_json(["herdr", "pane", "run"], allow_empty=True) is None


def test_missing_executable_is_reported_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ProcessError, match="command not found: definitely-not-a-real-tool"):
        proc.run(["definitely-not-a-real-tool"])


def test_have_reports_a_present_tool() -> None:
    assert proc.have("git") is not None
    assert proc.have("definitely-not-a-real-tool") is None
