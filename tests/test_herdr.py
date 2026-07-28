"""Tests for the herdr client: recorded JSON, plus a live-server suite."""

from __future__ import annotations

import json
import shutil
import subprocess
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

    client.send_keys("wG:p1", ["ctrl+c", "ctrl+c", "ctrl+d"])

    assert fake_proc.calls[0] == (
        "herdr",
        "pane",
        "send-keys",
        "wG:p1",
        "ctrl+c",
        "ctrl+c",
        "ctrl+d",
    )


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


def _pane_list(*panes: dict[str, object]) -> Reply:
    return Reply(stdout=wrapped("pane:list", {"panes": list(panes)}))


def test_the_pane_to_work_in_is_never_the_callers_own(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """Herdr exports HERDR_PANE_ID, so the caller's pane is in this workspace too.

    Taking it would send the exit keys to the terminal milhouse was typed into.
    """
    fake_proc.expect(
        "herdr pane list",
        _pane_list(
            {"pane_id": "wE:p4", "workspace_id": "wE", "agent": "claude"},
            {"pane_id": "wE:p5", "workspace_id": "wE"},
        ),
    )

    assert client.pane_to_work_in("wE", Path("/repo"), avoid="wE:p4") == "wE:p5"


def test_a_pane_running_an_agent_is_left_alone(client: HerdrClient, fake_proc: FakeProc) -> None:
    fake_proc.expect(
        "herdr pane list",
        _pane_list(
            {"pane_id": "wE:p4", "workspace_id": "wE", "agent": "codex"},
            {"pane_id": "wE:p5", "workspace_id": "wE"},
        ),
    )

    assert client.pane_to_work_in("wE", Path("/repo")) == "wE:p5"


def test_a_workspace_with_no_free_pane_gets_a_new_one(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect(
        "herdr pane list",
        _pane_list({"pane_id": "wE:p4", "workspace_id": "wE", "agent": "claude"}),
    )
    fake_proc.expect(
        "herdr pane split",
        Reply(stdout=wrapped("pane:split", {"pane": {"pane_id": "wE:p9"}})),
    )

    assert client.pane_to_work_in("wE", Path("/repo"), avoid="wE:p4") == "wE:p9"


def test_an_empty_workspace_is_reported_rather_than_split(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr pane list", _pane_list())

    with pytest.raises(HerdrError, match="has no panes"):
        client.pane_to_work_in("wZ", Path("/repo"))


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

        client.send_keys(workspace.pane_id, ["ctrl+c"])
        assert isinstance(client.read_pane(workspace.pane_id, lines=20), str)

        second = client.split_pane(workspace.pane_id, tmp_path)
        assert second != workspace.pane_id
        client.close_pane(second)
    finally:
        client.close_workspace(workspace.workspace_id)

    assert not client.workspace_exists(workspace.workspace_id)


@pytest.mark.herdr
def test_the_pane_to_work_in_avoids_the_caller_against_the_live_server(tmp_path: Path) -> None:
    """The one that matters: a real workspace really does hand back another pane.

    The recorded test proves the filtering. This proves the field it filters on
    is the field herdr actually sends, which is the half that made this a bug.
    """
    if shutil.which("herdr") is None:
        pytest.skip("herdr is not installed")
    client = HerdrClient()
    try:
        workspace = client.create_workspace(tmp_path, "milhouse:test-pane-choice")
    except HerdrError as exc:
        pytest.skip(f"herdr server unavailable: {exc}")

    try:
        # The only pane is the caller's, so milhouse has to make itself one.
        chosen = client.pane_to_work_in(workspace.workspace_id, tmp_path, avoid=workspace.pane_id)
        assert chosen != workspace.pane_id
        assert chosen in {str(pane["pane_id"]) for pane in client.panes_in(workspace.workspace_id)}
        # With a free pane available it is reused rather than multiplying panes.
        assert (
            client.pane_to_work_in(workspace.workspace_id, tmp_path, avoid=workspace.pane_id)
            == chosen
        )
    finally:
        client.close_workspace(workspace.workspace_id)


@pytest.mark.herdr
def test_the_lane_registry_against_the_live_server(tmp_path: Path) -> None:
    """A worktree, its workspace label, and a tab in it, through the real herdr.

    The recorded tests prove the argv. This proves the fields lane assignment
    reads back — the workspace label, the checkout path, and the tab label — are
    the fields herdr actually sends.
    """
    if shutil.which("herdr") is None:
        pytest.skip("herdr is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "i"], check=True)

    client = HerdrClient()
    try:
        source = client.create_workspace(repo, "milhouse:test-lanes")
    except HerdrError as exc:
        pytest.skip(f"herdr server unavailable: {exc}")

    lane = None
    try:
        lane = client.create_worktree(
            source_workspace=source.workspace_id,
            branch="milhouse/bd-e.1",
            base="main",
            label="bd-e.1",
        )
        assert lane.branch == "milhouse/bd-e.1"
        assert lane.path.is_dir()
        # A lane herdr chose the path for is outside the repository, so no
        # amount of untracked files in it can dirty another lane's checkout.
        assert not lane.path.is_relative_to(repo)
        assert client.workspace_labels()[lane.workspace_id] == "bd-e.1"

        registry = {item.path: item for item in client.worktrees(repo)}
        assert registry[lane.path].workspace_id == lane.workspace_id
        assert registry[repo].workspace_id == source.workspace_id

        # A stacked issue is a labelled tab in the same lane.
        pane = client.create_tab(lane.workspace_id, lane.path, "bd-e.2")
        stacked = next(
            tab for tab in client.tabs(lane.workspace_id) if tab.get("label") == "bd-e.2"
        )
        assert pane in {
            str(item["pane_id"])
            for item in client.panes_in(lane.workspace_id, tab_id=str(stacked["tab_id"]))
        }
    finally:
        if lane is not None:
            client.close_workspace(lane.workspace_id)
            shutil.rmtree(lane.path, ignore_errors=True)
        client.close_workspace(source.workspace_id)


def test_the_agents_own_pane_is_reported(fake_proc: FakeProc) -> None:
    """Reaping sends the exit keys here, not to whatever pane happens to be free."""
    fake_proc.expect(
        "herdr agent get",
        Reply(stdout=wrapped("agent:get", {"agent": {"pane_id": "wL1:p3"}})),
    )

    assert HerdrClient().agent_pane("milhouse-bd-e.1") == "wL1:p3"


def test_an_agent_herdr_has_lost_has_no_pane(fake_proc: FakeProc) -> None:
    fake_proc.expect("herdr agent get", Reply(stdout=error("agent:get", "agent_not_found", "gone")))

    assert HerdrClient().agent_pane("milhouse-bd-e.1") is None
