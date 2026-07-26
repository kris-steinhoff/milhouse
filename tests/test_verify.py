"""Tests for the verification pass over a closed issue."""

from __future__ import annotations

from pathlib import Path

from milhouse import verify as verify_module
from milhouse.config import Config
from milhouse.models import Issue
from milhouse.outcome import classify
from milhouse.verify import OUTPUT_TAIL, Verification, verify

from .fakes import FakeProc, Reply

CLOSED = Issue(id="bd-e.1", title="Add it", status="closed")


def test_no_command_configured_means_no_verification(config: Config, fake_proc: FakeProc) -> None:
    """Out of the box milhouse takes the agent at its word."""
    assert verify(config) is None
    assert fake_proc.calls == []


def test_a_passing_command_verifies(config: Config, fake_proc: FakeProc) -> None:
    config.verify.command = ["uv", "run", "pytest", "-q"]
    fake_proc.expect("uv run pytest", Reply(stdout="42 passed"))

    checked = verify(config)

    assert checked is not None
    assert checked.ok
    assert checked.command == "uv run pytest -q"
    assert checked.output == ""


def test_a_failing_command_keeps_its_output(config: Config, fake_proc: FakeProc) -> None:
    config.verify.command = ["uv", "run", "pytest"]
    fake_proc.expect(
        "uv run pytest", Reply(stdout="E   assert 1 == 2", stderr="1 failed", returncode=1)
    )

    checked = verify(config)

    assert checked is not None
    assert not checked.ok
    assert "assert 1 == 2" in checked.output
    assert "1 failed" in checked.output


def test_the_command_runs_in_the_repository(config: Config, fake_proc: FakeProc) -> None:
    config.verify.command = ["make", "check"]
    seen: dict[str, object] = {}

    def record(argv: tuple[str, ...]) -> Reply:
        seen["argv"] = argv
        return Reply()

    fake_proc.expect("make", record)
    verify(config)

    assert seen["argv"] == ("make", "check")


def test_a_command_that_cannot_run_is_a_failed_verification(
    config: Config, monkeypatch: object
) -> None:
    """A missing gate must not take the run down with it."""
    config.verify.command = ["definitely-not-a-command"]

    checked = verify(config)

    assert checked is not None
    assert not checked.ok
    assert "definitely-not-a-command" in checked.output


def test_long_output_is_truncated_to_its_tail(config: Config, fake_proc: FakeProc) -> None:
    """Test runners put the summary at the end, and a bead is not a log file."""
    config.verify.command = ["noisy"]
    fake_proc.expect("noisy", Reply(stdout="x" * 50_000 + "THE ACTUAL FAILURE", returncode=1))

    checked = verify(config)

    assert checked is not None
    assert len(checked.output) <= OUTPUT_TAIL + 2
    assert checked.output.endswith("THE ACTUAL FAILURE")


def test_the_tail_marker_is_dropped_for_short_output(config: Config, fake_proc: FakeProc) -> None:
    config.verify.command = ["quiet"]
    fake_proc.expect("quiet", Reply(stdout="  one line  ", returncode=1))

    checked = verify(config)

    assert checked is not None
    assert checked.output == "one line"


# -- how the verdict uses it ---------------------------------------------------


def test_a_closed_issue_with_a_passing_verification_is_a_success() -> None:
    verdict = classify(
        issue_after=CLOSED,
        head_before="a",
        head_after="b",
        agent_state="done",
        verification=Verification(ok=True, command="pytest"),
    )

    assert verdict.outcome == "success"


def test_a_closed_issue_with_a_failing_verification_is_rejected() -> None:
    """`bd` saying closed is the agent grading its own exam."""
    verdict = classify(
        issue_after=CLOSED,
        head_before="a",
        head_after="b",
        agent_state="done",
        verification=Verification(ok=False, command="pytest", output="1 failed"),
    )

    assert verdict.outcome == "rejected"
    assert "pytest" in verdict.detail


def test_no_verification_leaves_a_closed_issue_a_success() -> None:
    verdict = classify(
        issue_after=CLOSED, head_before="a", head_after="b", agent_state="done", verification=None
    )

    assert verdict.outcome == "success"


def test_the_module_exposes_its_truncation_limit() -> None:
    """The cap is documented in ADR 0016, so it is a constant rather than a magic number."""
    assert verify_module.OUTPUT_TAIL > 0


def test_verification_needs_no_repo_on_disk(tmp_path: Path) -> None:
    """`verify` reads config only; nothing is created before the command runs."""
    assert verify(Config(repo_root=tmp_path / "nowhere")) is None
