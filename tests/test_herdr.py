"""Tests for the herdr client: recorded JSON, plus a live-server suite."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from milhouse.errors import HerdrError, TurnTimeoutError
from milhouse.herdr import AGENT_NAME, AGENT_NAME_RULE, HerdrClient

from .fakes import FakeProc, Reply


def wrapped(call: str, result: dict) -> str:
    """Wrap a result the way the herdr CLI does."""
    return json.dumps({"id": f"cli:{call}", "result": result})


def failed(call: str, code: str, message: str) -> Reply:
    """Reply the way herdr reports a failure: an envelope on stderr, exiting 1.

    Verified against 0.7.5 across `workspace get/close`, `agent get/start/prompt`,
    `pane get/close/send-keys`, `tab list` and `worktree list/open`. Every one of
    them leaves stdout empty, so a client reading stdout alone sees nothing at
    all and a client trusting the status never gets as far as the code.
    """
    envelope = {"id": f"cli:{call}", "error": {"code": code, "message": message}}
    return Reply(stderr=json.dumps(envelope), returncode=1)


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


def test_a_herdr_error_reads_as_a_sentence(client: HerdrClient, fake_proc: FakeProc) -> None:
    """The whole point of reading the envelope before the exit status.

    Every herdr failure milhouse can hit passes through here, so this is what
    decides whether one reads as herdr's own words or as JSON quoted inside a
    subprocess failure. The code is carried separately because callers branch on
    it, and prose is the wrong thing to match on.
    """
    fake_proc.expect(
        "herdr workspace get",
        failed("workspace:get", "workspace_not_found", "workspace wZZ not found"),
    )

    with pytest.raises(HerdrError) as caught:
        client._call(["workspace", "get", "wZZ"])

    assert str(caught.value) == "herdr workspace get: workspace_not_found: workspace wZZ not found"
    assert caught.value.code == "workspace_not_found"


def test_an_error_on_stdout_is_read_the_same_way(client: HerdrClient, fake_proc: FakeProc) -> None:
    """0.7.5 uses stderr, and milhouse pins no herdr version.

    Reading the envelope from either stream costs one loop and means a server
    that answers differently is understood rather than quoted.
    """
    envelope = json.dumps(
        {"id": "cli:workspace:get", "error": {"code": "workspace_not_found", "message": "no"}}
    )
    fake_proc.expect("herdr workspace get", Reply(stdout=envelope, returncode=0))

    with pytest.raises(HerdrError, match="workspace_not_found"):
        client._call(["workspace", "get", "wZZ"])


def test_a_failure_with_no_envelope_reports_what_herdr_said(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """An argv herdr rejects itself: exit 2, plain text on stderr, no envelope.

    Nothing here is a herdr error code, so there is none to report and the exit
    status is all there is to go on. That path has to stay loud.
    """
    fake_proc.expect(
        "herdr workspace get",
        Reply(stderr="unknown command: nope\nrun 'herdr --help' for usage", returncode=2),
    )

    with pytest.raises(HerdrError) as caught:
        client._call(["workspace", "get", "wZZ"])

    assert "exited 2: unknown command: nope" in str(caught.value)
    assert caught.value.code == ""


def test_workspace_exists_turns_that_error_into_false(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr workspace get", failed("workspace:get", "workspace_not_found", "no"))

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


@pytest.mark.parametrize(
    "name",
    ["milhouse-bd-e.1", "milhouse-BD-E", "1milhouse", "milhouse-" + "x" * 24, ""],
    ids=["dot", "uppercase", "leading-digit", "thirty-three", "empty"],
)
def test_a_name_herdr_would_refuse_never_reaches_herdr(
    client: HerdrClient, fake_proc: FakeProc, name: str
) -> None:
    """These arrived as CLI JSON, after the lane was opened and the issue claimed.

    The empty `calls` is the assertion: `fake_proc` is strict, so dropping the
    check turns this into an unmatched-command failure rather than a pass.
    """
    with pytest.raises(HerdrError, match="invalid herdr agent name"):
        client.start_agent(name, kind="claude", pane_id="wG:p1")

    assert fake_proc.calls == []


def test_the_refusal_names_the_name_and_the_rule(client: HerdrClient, fake_proc: FakeProc) -> None:
    """Nothing else will: herdr, which would have said it, was never asked."""
    with pytest.raises(HerdrError) as caught:
        client.start_agent("milhouse-bd-e.1", kind="claude", pane_id="wG:p1")

    assert "milhouse-bd-e.1" in str(caught.value)
    assert AGENT_NAME_RULE in str(caught.value)


@pytest.mark.parametrize(
    "name",
    ["milhouse-bd_e_1", "milhouse-bd-e-", "m" * 32],
    ids=["underscores", "trailing-hyphen", "thirty-two"],
)
def test_a_name_herdr_takes_is_not_refused_here(
    client: HerdrClient, fake_proc: FakeProc, name: str
) -> None:
    """Refusing one of these would be the same bug facing the other way.

    A lane that cannot start an agent, for a reason herdr does not hold. All three
    reached pane resolution on the live 0.7.5 server, so all three are herdr's.
    """
    fake_proc.expect("herdr agent start", Reply(stdout=AGENT_STARTED))

    client.start_agent(name, kind="claude", pane_id="wG:p1")

    assert fake_proc.calls[0][:4] == ("herdr", "agent", "start", name)


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
    """`timeout` is the code `herdr agent prompt --help` names for a short --timeout."""
    fake_proc.expect(
        "herdr agent prompt",
        failed("agent:prompt", "timeout", "timed out waiting for agent status"),
    )

    with pytest.raises(TurnTimeoutError, match="did not finish its turn"):
        client.prompt("milhouse-hello", "x", timeout_ms=1000)


@pytest.mark.parametrize(
    "code",
    ["agent_not_found", "invalid_agent_timeout"],
    ids=["unrelated", "contains-the-word"],
)
def test_a_non_timeout_prompt_failure_is_not_swallowed(
    client: HerdrClient, fake_proc: FakeProc, code: str
) -> None:
    """The second is why the code is matched exactly rather than searched for.

    `invalid_agent_timeout` is a real 0.7.5 code (probed by asking `agent start`
    for a 1ms readiness timeout). It means herdr refused an argument and waited
    on nothing, which is the opposite of a turn that ran out — but it contains
    the word, so a message search calls it a turn timeout and the step records a
    turn that never happened.
    """
    fake_proc.expect("herdr agent prompt", failed("agent:prompt", code, "no"))

    with pytest.raises(HerdrError) as caught:
        client.prompt("milhouse-hello", "x", timeout_ms=1000)

    assert not isinstance(caught.value, TurnTimeoutError)


# -- confirming that a prompt was actually submitted ---------------------------


def agent_with(seq: int, *, status: str = "idle") -> Reply:
    """`herdr agent get` for an agent herdr has observed ``seq`` changes for."""
    return Reply(
        stdout=wrapped("agent:get", {"agent": {"agent_status": status, "state_change_seq": seq}})
    )


SUBMISSION_TOOK = Reply(stdout=wrapped("agent:prompt", {"agent": {"agent_status": "working"}}))
STALLED = failed("agent:prompt", "agent_prompt_stalled", "no state change observed")


def test_a_submission_waits_only_for_the_agent_to_react(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """`working` in the `--until` set is what keeps this from waiting out the turn.

    herdr requires an observed state change before it matches anything, so a wait
    over every state it can report ends at the change itself. Dropping `working`
    would turn a submission check into `milhouse step`.
    """
    fake_proc.expect("herdr agent get", agent_with(7))
    fake_proc.expect("herdr agent prompt", SUBMISSION_TOOK)

    assert client.submit("milhouse-hello", "do the thing", timeout_ms=15_000) == "working"

    argv = next(fake_proc.commands("herdr", "agent", "prompt"))
    assert [argv[i + 1] for i, word in enumerate(argv) if word == "--until"] == [
        "working",
        "idle",
        "done",
        "blocked",
    ]
    assert "--wait" in argv
    assert argv[argv.index("--timeout") + 1] == "15000"


def test_a_prompt_herdr_never_saw_the_agent_take_is_submitted_again(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """The observed failure, and the observed fix.

    Three cold-started claude agents in a row swallowed their first prompt and
    stalled at herdr's five-second floor. All three took it on the re-submission,
    in about a third of a second.
    """
    fake_proc.expect("herdr agent get", agent_with(7))
    fake_proc.expect("herdr agent prompt", [STALLED, SUBMISSION_TOOK])

    assert client.submit("milhouse-hello", "do the thing", timeout_ms=15_000) == "working"

    assert len(list(fake_proc.commands("herdr", "agent", "prompt"))) == 2


def test_a_submission_herdr_will_not_confirm_is_reported_rather_than_assumed(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """The turn is refused at the seam rather than recorded and reaped as stalled."""
    fake_proc.expect("herdr agent get", agent_with(7))
    fake_proc.expect("herdr agent prompt", STALLED)

    with pytest.raises(HerdrError) as caught:
        client.submit("milhouse-hello", "do the thing", timeout_ms=15_000, attempts=3)

    assert "did not observe agent milhouse-hello react" in str(caught.value)
    assert caught.value.code == "agent_prompt_stalled"
    assert len(list(fake_proc.commands("herdr", "agent", "prompt"))) == 3


def test_a_state_change_herdr_missed_is_not_prompted_a_second_time(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """`state_change_seq` is the guard against a retry re-running a live turn.

    herdr reported that it observed nothing, and its own count of what it has
    observed says otherwise. The count wins: the prompt landed, and submitting it
    again would give the agent the same work twice.
    """
    fake_proc.expect("herdr agent get", [agent_with(7), agent_with(8, status="working")])
    fake_proc.expect("herdr agent prompt", STALLED)

    assert client.submit("milhouse-hello", "do the thing", timeout_ms=15_000) == "working"

    assert len(list(fake_proc.commands("herdr", "agent", "prompt"))) == 1


def test_a_timeout_below_herdrs_floor_is_read_as_the_same_thing(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """`herdr agent prompt --help`: a --timeout under 5000ms returns `timeout` instead.

    Same condition, different code, so retrying has to key on both or a short
    timeout quietly loses the retry.
    """
    fake_proc.expect("herdr agent get", agent_with(7))
    fake_proc.expect(
        "herdr agent prompt",
        [failed("agent:prompt", "timeout", "timed out"), SUBMISSION_TOOK],
    )

    assert client.submit("milhouse-hello", "x", timeout_ms=1_000) == "working"


def test_a_prompt_failure_that_is_not_about_observation_is_not_retried(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """An agent herdr does not know will not know itself any better next time."""
    fake_proc.expect("herdr agent get", agent_with(7))
    fake_proc.expect("herdr agent prompt", failed("agent:prompt", "agent_not_found", "gone"))

    with pytest.raises(HerdrError, match="agent_not_found"):
        client.submit("milhouse-hello", "x", timeout_ms=15_000)

    assert len(list(fake_proc.commands("herdr", "agent", "prompt"))) == 1


def test_the_change_seq_is_read_from_the_agent_herdr_reports(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr agent get", agent_with(93))

    assert client.change_seq("milhouse-hello") == 93


def test_an_agent_herdr_has_lost_has_no_change_seq(
    client: HerdrClient, fake_proc: FakeProc
) -> None:
    """`None` is "no answer", which is what stops the retry guard acting on one."""
    fake_proc.expect("herdr agent get", failed("agent:get", "agent_not_found", "gone"))

    assert client.change_seq("milhouse-hello") is None


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


# Names along every edge of the rule herdr states: its character set, its leading
# character, thirty-two against thirty-three, and no name at all. What each one
# should do is deliberately not written down here. It is derived from AGENT_NAME,
# which is the claim on trial.
AGENT_NAME_PROBES = (
    "milhouse-bd-e_1",
    "milhouse-bd-e-",
    "m" * 32,
    "m" * 33,
    "milhouse-bd-e.1",
    "milhouse-BD-E",
    "1milhouse",
    "",
)

# A pane herdr cannot resolve. herdr checks the name before it looks for the
# pane, so a name it accepts gets exactly this far, which is what keeps this test
# from starting an agent or spending a token.
NO_SUCH_PANE = "wZZZ:pZZZ"

# Any name at all, standing in for AGENT_NAME so that herdr does the refusing.
ANY_NAME = re.compile(r"(?s).*")


@pytest.mark.herdr
def test_herdr_refuses_exactly_the_names_milhouse_refuses_against_the_live_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AGENT_NAME` is the grammar of the herdr that is installed, not one recalled.

    The recorded tests assert what milhouse thinks an agent name is, so they
    stayed green while the real `herdr agent start` could not start an agent at
    all. This one submits each probe name to the live server and asserts the
    verdict `AGENT_NAME` predicts: `invalid_agent_name` for a name outside it,
    and `agent_pane_not_found` for a name inside it, which is herdr getting past
    the name and on to the pane. Both directions matter — a name milhouse refuses
    that herdr would take is the same bug facing the other way — so the
    expectation is derived from the pattern rather than listed beside it.

    Nothing is started and no tokens are spent: every call names a pane that does
    not exist, and herdr validates the name first.

    `start_agent` refuses a bad name itself now, so it cannot be the thing that
    asks herdr. For the calls that guard would block, and only those, the pattern
    it reads is swapped for one that matches anything. That leaves the production
    guard exactly as it is, reuses herdr's real argv rather than rebuilding it
    here, and puts the answer where the test wants it: herdr's, not milhouse's.
    The `AGENT_NAME` imported at the top of this module keeps pointing at the real
    pattern, which is what the expectation is still derived from.

    herdr 0.7.5 is what reported this rule and the wording `AGENT_NAME_RULE`
    quotes. milhouse pins no herdr version, so whether an earlier server enforced
    it is unknown.

    The verdict is read from `HerdrError.code`, which is also the live proof that
    `_call` reaches its error branch: the code is only there if the envelope was
    parsed, and against 0.7.5 that means it was found on stderr after a non-zero
    exit. `--timeout 1000` is below the 3000ms floor `agent start` enforces, and
    stays that way because herdr resolves the pane before it looks at the
    timeout — so these calls end at the pane, as the docstring above promises.
    """
    if shutil.which("herdr") is None:
        pytest.skip("herdr is not installed")
    client = HerdrClient()
    try:
        client.workspace_labels()
    except HerdrError as exc:
        pytest.skip(f"herdr server unavailable: {exc}")

    failures = {}
    for name in AGENT_NAME_PROBES:
        with monkeypatch.context() as relaxed:
            if not AGENT_NAME.fullmatch(name):
                relaxed.setattr("milhouse.herdr.AGENT_NAME", ANY_NAME)
            with pytest.raises(HerdrError) as caught:
                client.start_agent(name, kind="claude", pane_id=NO_SUCH_PANE, timeout_ms=1000)
        failures[name] = caught.value

    expected = {
        name: "agent_pane_not_found" if AGENT_NAME.fullmatch(name) else "invalid_agent_name"
        for name in AGENT_NAME_PROBES
    }
    assert {name: failure.code for name, failure in failures.items()} == expected
    # AGENT_NAME_RULE claims to be herdr's own sentence, so that milhouse's early
    # refusal and herdr's own read alike. Only herdr can confirm that.
    refused = [
        str(failures[name]) for name, code in expected.items() if code == "invalid_agent_name"
    ]
    assert all(AGENT_NAME_RULE in failure for failure in refused), refused


@pytest.mark.herdr
def test_an_agent_that_was_never_prompted_against_the_live_server(tmp_path: Path) -> None:
    """The premise of the whole submission check, asked of the server that holds it.

    An agent that has been started and never prompted has done nothing at all,
    and herdr reports it `idle` — the same word it reports for one that has
    finished a turn. `state_change_seq` is the field that tells them apart, and
    the recorded tests can only prove milhouse reads it, not that herdr sends it.
    This asks herdr.

    No prompt is submitted and no tokens are spent: starting an agent launches
    the binary and nothing else. The agent is exited and its workspace closed on
    the way out, both created here.
    """
    if shutil.which("herdr") is None:
        pytest.skip("herdr is not installed")
    if shutil.which("claude") is None:
        pytest.skip("the claude agent binary is not installed")
    client = HerdrClient()
    try:
        workspace = client.create_workspace(tmp_path, "milhouse:test-change-seq")
    except HerdrError as exc:
        pytest.skip(f"herdr server unavailable: {exc}")

    name = "milhouse-test-change-seq"
    try:
        started = client.start_agent(name, kind="claude", pane_id=workspace.pane_id)
        # Idle, having done nothing: the state a finished turn also reports.
        assert started.status != "working"
        assert client.agent_status(name) != "working"
        # And the counter that says which of the two this is.
        first = client.change_seq(name)
        assert isinstance(first, int)
        # Nothing was submitted, so nothing moved it.
        assert client.change_seq(name) == first
    finally:
        client.send_keys(workspace.pane_id, ["ctrl+c", "ctrl+c", "ctrl+d"])
        client.wait_for_shell(workspace.pane_id, timeout_s=8.0)
        client.close_workspace(workspace.workspace_id)


def test_the_agents_own_pane_is_reported(fake_proc: FakeProc) -> None:
    """Reaping sends the exit keys here, not to whatever pane happens to be free."""
    fake_proc.expect(
        "herdr agent get",
        Reply(stdout=wrapped("agent:get", {"agent": {"pane_id": "wL1:p3"}})),
    )

    assert HerdrClient().agent_pane("milhouse-bd-e_1") == "wL1:p3"


def test_an_agent_herdr_has_lost_has_no_pane(fake_proc: FakeProc) -> None:
    fake_proc.expect("herdr agent get", failed("agent:get", "agent_not_found", "gone"))

    assert HerdrClient().agent_pane("milhouse-bd-e_1") is None
