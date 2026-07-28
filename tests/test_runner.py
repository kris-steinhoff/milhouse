"""Tests for the per-iteration agent lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from milhouse.config import Config
from milhouse.errors import AgentError, HerdrError
from milhouse.herdr import HerdrClient
from milhouse.runner import AgentRunner

from .fakes import FakeProc, Reply
from .test_herdr import AGENT_STARTED, error, wrapped

PANE_WITH_AGENT = wrapped("pane:get", {"pane": {"agent": "claude", "agent_status": "done"}})
PANE_AT_SHELL = wrapped("pane:get", {"pane": {"agent_status": "unknown"}})
TURN_DONE = wrapped("agent:prompt", {"agent": {"agent_status": "done"}})
TURN_BLOCKED = wrapped("agent:prompt", {"agent": {"agent_status": "blocked"}})


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "hello"


@pytest.fixture
def runner(config: Config, run_dir: Path) -> AgentRunner:
    return AgentRunner(
        HerdrClient(),
        config,
        run_dir=run_dir,
        pane_id="wG:p1",
        agent_name="milhouse-hello",
    )


@pytest.fixture
def happy(fake_proc: FakeProc) -> FakeProc:
    """A pane that starts at a shell, runs a turn, and exits cleanly."""
    panes = [
        Reply(stdout=PANE_AT_SHELL),
        Reply(stdout=PANE_WITH_AGENT),
        Reply(stdout=PANE_AT_SHELL),
    ]
    fake_proc.expect("herdr pane get", panes)
    fake_proc.expect("herdr agent start", Reply(stdout=AGENT_STARTED))
    fake_proc.expect("herdr agent prompt", Reply(stdout=TURN_DONE))
    fake_proc.expect("herdr agent read", Reply(stdout="claude did the thing\n"))
    fake_proc.expect("herdr pane send-keys", Reply(stdout=""))
    return fake_proc


def test_a_turn_starts_prompts_captures_and_exits(
    runner: AgentRunner, happy: FakeProc, run_dir: Path
) -> None:
    result = runner.run_turn("do the thing", iteration=7)

    assert result.agent_state == "done"
    assert not result.timed_out
    assert result.error is None
    assert (run_dir / "iter-007.prompt").read_text() == "do the thing"
    assert (run_dir / "iter-007.term").read_text() == "claude did the thing\n"
    assert happy.ran("herdr", "agent", "start")
    assert happy.ran("herdr", "agent", "prompt")
    assert happy.ran("herdr", "pane", "send-keys")


def test_an_issues_artifacts_go_in_a_directory_of_its_own(
    runner: AgentRunner, happy: FakeProc, run_dir: Path
) -> None:
    """Two agents working different issues must not collide on a filename."""
    result = runner.run_turn("do the thing", iteration=7, issue_id="bd-42")

    assert (run_dir / "bd-42" / "iter-007.prompt").read_text() == "do the thing"
    assert (run_dir / "bd-42" / "iter-007.term").read_text() == "claude did the thing\n"
    assert not (run_dir / "iter-007.prompt").exists()
    assert result.prompt_path == run_dir / "bd-42" / "iter-007.prompt"
    assert result.transcript_path == run_dir / "bd-42" / "iter-007.term"


def test_every_attempt_at_one_issue_lands_together(
    runner: AgentRunner, happy: FakeProc, run_dir: Path
) -> None:
    """The point of the directory: one issue's history is one listing."""
    runner.run_turn("first go", iteration=3, issue_id="bd-42")
    happy.expect("herdr agent read", Reply(stdout="second\n"))
    runner.run_turn("second go", iteration=9, issue_id="bd-42")

    assert sorted(path.name for path in (run_dir / "bd-42").iterdir()) == [
        "iter-003.prompt",
        "iter-003.term",
        "iter-009.prompt",
        "iter-009.term",
    ]


def test_the_transcript_is_captured_before_the_agent_exits(
    runner: AgentRunner, happy: FakeProc
) -> None:
    """The exit path can lose the pane, so the read has to come first."""
    runner.run_turn("do the thing", iteration=1)

    order = [call[:3] for call in happy.calls]
    assert order.index(("herdr", "agent", "read")) < order.index(("herdr", "pane", "send-keys"))


def test_the_configured_exit_keys_are_sent(runner: AgentRunner, happy: FakeProc) -> None:
    runner.config.agent.exit_keys = ["ctrl+c", "ctrl+d"]

    runner.run_turn("x", iteration=1)

    keys = next(happy.commands("herdr", "pane", "send-keys"))
    assert keys[4:] == ("ctrl+c", "ctrl+d")


def test_a_blocked_turn_is_reported_not_raised(runner: AgentRunner, happy: FakeProc) -> None:
    happy.expect("herdr agent prompt", Reply(stdout=TURN_BLOCKED))

    assert runner.run_turn("x", iteration=1).agent_state == "blocked"


def test_a_turn_timeout_is_reported_not_raised(runner: AgentRunner, happy: FakeProc) -> None:
    happy.expect("herdr agent prompt", Reply(stdout=error("agent:prompt", "timeout", "timed out")))
    happy.expect("herdr agent get", Reply(stdout=wrapped("agent:get", {"agent": {}})))

    result = runner.run_turn("x", iteration=1)

    assert result.timed_out
    assert result.agent_state == "unknown"


def test_a_failed_start_is_reported_as_an_error(runner: AgentRunner, fake_proc: FakeProc) -> None:
    fake_proc.expect("herdr pane get", Reply(stdout=PANE_AT_SHELL))
    fake_proc.expect(
        "herdr agent start", Reply(stdout=error("agent:start", "agent_not_detected", "no agent"))
    )

    result = runner.run_turn("x", iteration=1)

    assert result.error is not None
    assert "could not start the agent" in result.error
    assert not fake_proc.ran("herdr", "agent", "prompt")


def test_a_pane_that_will_not_release_is_replaced(runner: AgentRunner, fake_proc: FakeProc) -> None:
    """Falling back to a fresh pane is unambiguous, if more expensive."""
    fake_proc.expect("herdr pane get", Reply(stdout=PANE_WITH_AGENT))
    fake_proc.expect("herdr pane send-keys", Reply(stdout=""))
    fake_proc.expect(
        "herdr pane split", Reply(stdout=wrapped("pane:split", {"pane": {"pane_id": "wG:p9"}}))
    )
    fake_proc.expect("herdr pane close", Reply(stdout=""))

    runner.config.agent.exit_keys = ["ctrl+c"]
    runner.exit_agent()

    assert runner.pane_id == "wG:p9"
    assert fake_proc.ran("herdr", "pane", "close", "wG:p1")


def test_exiting_is_a_no_op_when_no_agent_is_running(
    runner: AgentRunner, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr pane get", Reply(stdout=PANE_AT_SHELL))

    runner.exit_agent()

    assert not fake_proc.ran("herdr", "pane", "send-keys")
    assert not fake_proc.ran("herdr", "pane", "split")


def test_an_unreplaceable_pane_is_an_agent_error(runner: AgentRunner, fake_proc: FakeProc) -> None:
    fake_proc.expect("herdr pane get", Reply(stdout=PANE_WITH_AGENT))
    fake_proc.expect("herdr pane send-keys", Reply(stdout=""))
    fake_proc.expect("herdr pane split", Reply(stdout=error("pane:split", "no_space", "too small")))

    runner.config.agent.exit_keys = ["ctrl+c"]

    with pytest.raises(AgentError, match="could not replace pane"):
        runner.exit_agent()


def test_an_empty_transcript_writes_no_file(
    runner: AgentRunner, happy: FakeProc, run_dir: Path
) -> None:
    happy.expect("herdr agent read", Reply(stdout="   \n"))

    result = runner.run_turn("x", iteration=1)

    assert result.transcript_path is None
    assert not (run_dir / "iter-001.term").exists()


def test_a_transcript_read_failure_does_not_fail_the_turn(
    runner: AgentRunner, happy: FakeProc
) -> None:
    happy.expect("herdr agent read", Reply(stderr="boom\n", returncode=1))

    result = runner.run_turn("x", iteration=1)

    assert result.agent_state == "done"
    assert result.transcript_path is None


def test_agent_args_reach_herdr(runner: AgentRunner, happy: FakeProc) -> None:
    runner.config.agent.args = ["--dangerously-skip-permissions"]

    runner.run_turn("x", iteration=1)

    argv = next(happy.commands("herdr", "agent", "start"))
    assert argv[argv.index("--") + 1] == "--dangerously-skip-permissions"


def test_a_leftover_agent_is_cleared_before_the_next_turn_starts(
    runner: AgentRunner, fake_proc: FakeProc
) -> None:
    """`herdr agent start` needs a shell prompt, so a stale agent is exited first."""
    fake_proc.expect(
        "herdr pane get",
        [
            Reply(stdout=PANE_WITH_AGENT),  # _ensure_shell finds a leftover agent
            Reply(stdout=PANE_WITH_AGENT),  # exit_agent sees it too
            Reply(stdout=PANE_WITH_AGENT),  # keys did not work
            Reply(stdout=PANE_AT_SHELL),  # the replacement pane is clean
        ],
    )
    fake_proc.expect("herdr pane send-keys", Reply(stdout=""))
    fake_proc.expect(
        "herdr pane split", Reply(stdout=wrapped("pane:split", {"pane": {"pane_id": "wG:p9"}}))
    )
    fake_proc.expect("herdr pane close", Reply(stdout=""))
    fake_proc.expect("herdr agent start", Reply(stdout=AGENT_STARTED))
    fake_proc.expect("herdr agent prompt", Reply(stdout=TURN_DONE))
    fake_proc.expect("herdr agent read", Reply(stdout="output"))

    result = runner.run_turn("x", iteration=1)

    assert result.error is None
    assert runner.pane_id == "wG:p9"


def test_a_pane_that_never_clears_is_an_agent_error(
    runner: AgentRunner, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr pane get", Reply(stdout=PANE_WITH_AGENT))
    fake_proc.expect("herdr pane send-keys", Reply(stdout=""))
    fake_proc.expect(
        "herdr pane split", Reply(stdout=wrapped("pane:split", {"pane": {"pane_id": "wG:p9"}}))
    )
    fake_proc.expect("herdr pane close", Reply(stdout=""))

    with pytest.raises(AgentError, match="will not return to a shell prompt"):
        runner._ensure_shell()


def test_herdr_client_errors_are_not_confused_with_json(
    runner: AgentRunner, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr pane get", Reply(stdout=json.dumps({"garbage": True})))
    fake_proc.expect("herdr pane send-keys", Reply(stdout=""))
    fake_proc.expect("herdr agent start", Reply(stdout=AGENT_STARTED))
    fake_proc.expect("herdr agent prompt", Reply(stdout=TURN_DONE))
    fake_proc.expect("herdr agent read", Reply(stdout="x"))

    # A response with neither `result` nor `error` yields an empty result, so the
    # pane reads as having no agent rather than crashing.
    assert runner.run_turn("x", iteration=1).agent_state == "done"


def test_the_client_surfaces_a_dead_server(runner: AgentRunner, fake_proc: FakeProc) -> None:
    fake_proc.expect("herdr", Reply(stderr="connection refused\n", returncode=1))

    with pytest.raises(HerdrError):
        runner.client.pane_agent("wG:p1")
