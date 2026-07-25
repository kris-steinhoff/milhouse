"""Tests for the herdr client: recorded JSON, plus a live-server suite."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from milhouse.errors import HerdrError, TurnTimeoutError
from milhouse.herdr import HerdrClient

from .fakes import FakeProc, Reply


def wrapped(call: str, result: dict) -> str:
    """Wrap a result the way the herdr CLI does."""
    return json.dumps({"id": f"cli:{call}", "result": result})


def error(call: str, code: str, message: str) -> str:
    """Render a herdr error payload, which arrives with exit status 0."""
    return json.dumps({"id": f"cli:{call}", "error": {"code": code, "message": message}})


WORKSPACE_CREATED = wrapped(
    "workspace:create",
    {
        "type": "workspace_created",
        "workspace": {"workspace_id": "wG", "label": "milhouse:hello"},
        "root_pane": {"pane_id": "wG:p1", "workspace_id": "wG"},
        "tab": {"tab_id": "wG:t1"},
    },
)

AGENT_STARTED = wrapped(
    "agent:start",
    {
        "type": "agent_started",
        "argv": ["claude"],
        "agent": {
            "name": "milhouse-hello",
            "agent": "claude",
            "agent_status": "idle",
            "pane_id": "wG:p1",
            "interactive_ready": True,
        },
    },
)


@pytest.fixture
def client() -> HerdrClient:
    return HerdrClient()


def test_creating_a_workspace_returns_the_root_pane(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr workspace create", Reply(stdout=WORKSPACE_CREATED))

    workspace = client.create_workspace(Path("/repo"), "milhouse:hello")

    assert workspace.workspace_id == "wG"
    assert workspace.pane_id == "wG:p1"
    argv = fake_proc.calls[0]
    assert argv[argv.index("--cwd") + 1] == "/repo"
    assert argv[argv.index("--label") + 1] == "milhouse:hello"
    assert "--no-focus" in argv


def test_focus_is_opt_in(client: HerdrClient, fake_proc: FakeProc) -> None:
    fake_proc.expect("herdr workspace create", Reply(stdout=WORKSPACE_CREATED))

    client.create_workspace(Path("/repo"), "milhouse:hello", focus=True)

    assert "--focus" in fake_proc.calls[0]
    assert "--no-focus" not in fake_proc.calls[0]


def test_a_herdr_error_payload_is_raised_despite_exit_zero(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """Failures arrive in the payload, not the exit status."""
    fake_proc.expect(
        "herdr workspace get",
        Reply(
            stdout=error("workspace:get", "workspace_not_found", "workspace wZZ not found"),
            returncode=0,
        ),
    )

    with pytest.raises(HerdrError, match="workspace_not_found"):
        client._call(["workspace", "get", "wZZ"])


def test_workspace_exists_turns_that_error_into_false(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect(
        "herdr workspace get",
        Reply(stdout=error("workspace:get", "workspace_not_found", "no")),
    )

    assert client.workspace_exists("wZZ") is False


def test_starting_an_agent_passes_kind_pane_and_args(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr agent start", Reply(stdout=AGENT_STARTED))

    agent = client.start_agent(
        "milhouse-hello",
        kind="claude",
        pane_id="wG:p1",
        args=["--dangerously-skip-permissions"],
        timeout_ms=60_000,
    )

    assert agent.status == "idle"
    assert agent.pane_id == "wG:p1"
    argv = fake_proc.calls[0]
    assert argv[argv.index("--kind") + 1] == "claude"
    assert argv[argv.index("--pane") + 1] == "wG:p1"
    assert argv[argv.index("--") + 1] == "--dangerously-skip-permissions"


def test_prompt_waits_for_the_settled_states_including_done(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """`done`, not `idle`, is what claude actually reaches when a turn ends."""
    fake_proc.expect(
        "herdr agent prompt",
        Reply(stdout=wrapped("agent:prompt", {"agent": {"agent_status": "done"}})),
    )

    status = client.prompt("milhouse-hello", "do the thing", timeout_ms=1000)

    assert status == "done"
    argv = fake_proc.calls[0]
    assert [argv[i + 1] for i, word in enumerate(argv) if word == "--until"] == [
        "idle",
        "done",
        "blocked",
    ]
    assert argv[argv.index("--timeout") + 1] == "1000"
    assert "--wait" in argv


def test_a_blocked_agent_is_reported_as_such(client: HerdrClient, fake_proc: FakeProc) -> None:
    fake_proc.expect(
        "herdr agent prompt",
        Reply(stdout=wrapped("agent:prompt", {"agent": {"agent_status": "blocked"}})),
    )

    assert client.prompt("milhouse-hello", "x", timeout_ms=1000) == "blocked"


def test_a_prompt_timeout_becomes_a_turn_timeout(client: HerdrClient, fake_proc: FakeProc) -> None:
    fake_proc.expect(
        "herdr agent prompt",
        Reply(stdout=error("agent:prompt", "timeout", "timed out waiting for agent status")),
    )

    with pytest.raises(TurnTimeoutError, match="did not finish its turn"):
        client.prompt("milhouse-hello", "x", timeout_ms=1000)


def test_a_non_timeout_prompt_failure_is_not_swallowed(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect(
        "herdr agent prompt",
        Reply(stdout=error("agent:prompt", "agent_not_found", "agent target not found")),
    )

    with pytest.raises(HerdrError) as caught:
        client.prompt("milhouse-hello", "x", timeout_ms=1000)

    assert not isinstance(caught.value, TurnTimeoutError)


def test_send_keys_addresses_the_pane_not_the_agent(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """The exit sequence makes the agent vanish partway through."""
    fake_proc.expect("herdr pane send-keys", Reply(stdout=""))

    client.send_keys("wG:p1", ["c-c", "c-c", "c-d"])

    assert fake_proc.calls[0] == ("herdr", "pane", "send-keys", "wG:p1", "c-c", "c-c", "c-d")


def test_pane_agent_is_none_at_a_shell_prompt(client: HerdrClient, fake_proc: FakeProc) -> None:
    fake_proc.expect(
        "herdr pane get",
        Reply(
            stdout=wrapped("pane:get", {"pane": {"pane_id": "wG:p1", "agent_status": "unknown"}})
        ),
    )

    assert client.pane_agent("wG:p1") is None


def test_pane_agent_reports_the_running_agent(client: HerdrClient, fake_proc: FakeProc) -> None:
    fake_proc.expect(
        "herdr pane get",
        Reply(stdout=wrapped("pane:get", {"pane": {"agent": "claude", "agent_status": "done"}})),
    )

    assert client.pane_agent("wG:p1") == "claude"


def test_wait_for_shell_gives_up_after_the_timeout(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect(
        "herdr pane get",
        Reply(stdout=wrapped("pane:get", {"pane": {"agent": "claude"}})),
    )

    assert client.wait_for_shell("wG:p1", timeout_s=0.0) is False


def test_first_pane_finds_the_workspace_pane(client: HerdrClient, fake_proc: FakeProc) -> None:
    fake_proc.expect(
        "herdr pane list",
        Reply(
            stdout=wrapped(
                "pane:list",
                {
                    "panes": [
                        {"pane_id": "wD:p6", "workspace_id": "wD"},
                        {"pane_id": "wG:p1", "workspace_id": "wG"},
                    ]
                },
            )
        ),
    )

    assert client.first_pane("wG") == "wG:p1"

    with pytest.raises(HerdrError, match="has no panes"):
        client.first_pane("wZ")


def test_a_missing_field_is_reported_rather_than_crashing(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr workspace create", Reply(stdout=wrapped("workspace:create", {})))

    with pytest.raises(HerdrError, match=r"missing workspace\.workspace_id"):
        client.create_workspace(Path("/repo"), "milhouse:hello")


# -- against the live herdr server ---------------------------------------------


@pytest.mark.herdr
def test_workspace_and_pane_lifecycle_against_the_live_server(tmp_path: Path) -> None:
    """Create a workspace, drive its pane with a shell command, then close it.

    No agent is started: this covers everything the client does around one,
    which is the part that can be tested without spending tokens. Skipped when
    `herdr` is not installed or its server is not running.
    """
    if shutil.which("herdr") is None:
        pytest.skip("herdr is not installed")
    client = HerdrClient()
    try:
        workspace = client.create_workspace(tmp_path, "milhouse:test-lifecycle")
    except HerdrError as exc:
        pytest.skip(f"herdr server unavailable: {exc}")

    try:
        assert client.workspace_exists(workspace.workspace_id)
        assert client.first_pane(workspace.workspace_id) == workspace.pane_id
        # An empty pane is at a shell prompt, so no agent occupies it.
        assert client.pane_agent(workspace.pane_id) is None
        assert client.wait_for_shell(workspace.pane_id, timeout_s=2.0)

        client.send_keys(workspace.pane_id, ["c-c"])
        assert isinstance(client.read_pane(workspace.pane_id, lines=20), str)

        second = client.split_pane(workspace.pane_id, tmp_path)
        assert second != workspace.pane_id
        client.close_pane(second)
    finally:
        client.close_workspace(workspace.workspace_id)

    assert not client.workspace_exists(workspace.workspace_id)
