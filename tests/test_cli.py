"""Tests for the command line: help text, exit codes, and the read-only commands.

What `step` actually does is covered in `test_step.py`, where the decisions are.
What matters here is that every flag is documented, errors map to their exit
codes, and the commands that promise not to start anything really do not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from milhouse import cli, proc
from milhouse.models import Iteration

from .doubles import FakeAudit
from .fakes import FakeProc, Reply
from .test_herdr import wrapped

runner = CliRunner()

NO_WORKSPACES = wrapped("workspace:list", {"workspaces": []})

EPIC = {
    "id": "bd-e",
    "title": "Add a hello command",
    "status": "open",
    "issue_type": "epic",
    "description": "It should greet.",
}
CHILDREN = [
    {"id": "bd-e.1", "title": "Add the subcommand", "status": "closed", "parent": "bd-e"},
    {"id": "bd-e.2", "title": "Document it", "status": "open", "parent": "bd-e"},
]


@pytest.fixture
def worked_repo(repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository milhouse can be pointed at, on `main`, with git discovery stubbed."""
    monkeypatch.setattr(cli, "find_repo_root", lambda path: repo)
    fake_proc.expect("git", Reply(stdout="main\n"))
    fake_proc.expect("herdr workspace list", Reply(stdout=NO_WORKSPACES))
    (repo / ".beads").mkdir()
    return repo


def invoke(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def test_help_lists_every_command() -> None:
    result = invoke("--help")

    assert result.exit_code == 0
    for command in ("doctor", "step", "status"):
        assert command in result.output


def test_there_is_no_plan_command() -> None:
    """Getting work into the tracker is somebody else's job now (ADR 0018)."""
    result = invoke("--help")

    assert "plan" not in result.output


@pytest.mark.parametrize(
    ("command", "flags"),
    [
        (
            "step",
            ["--agent", "--workspace", "--parent", "--label", "--dry-run", "--attach", "--repo"],
        ),
        ("status", ["--repo"]),
        ("doctor", ["--repo"]),
    ],
)
def test_every_flag_is_documented(command: str, flags: list[str]) -> None:
    """`milhouse --help` has to be a usable reference on its own."""
    result = invoke(command, "--help")

    assert result.exit_code == 0
    output = " ".join(result.output.split())
    for flag in flags:
        assert flag in output, f"{command} is missing help for {flag}"


@pytest.mark.parametrize("command", [None, "step", "status", "doctor"])
def test_short_help_flag_matches_long_one(command: str | None) -> None:
    """``-h`` is the same help as ``--help``, on the app and every subcommand."""
    args = [command] if command else []

    short = invoke(*args, "-h")
    long = invoke(*args, "--help")

    assert short.exit_code == 0
    assert short.output == long.output


def test_version_prints_and_exits() -> None:
    result = invoke("--version")

    assert result.exit_code == 0
    assert result.output.startswith("milhouse ")


def test_status_names_an_unfiltered_scope(worked_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    result = invoke("status")

    assert result.exit_code == 0
    assert "every ready issue in the repository" in result.output
    assert "(no issues)" in result.output


def test_status_shows_the_tree_and_the_history(worked_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout=json.dumps(CHILDREN)))
    FakeAudit(worked_repo).record(
        Iteration(number=1, issue_id="bd-e.1", outcome="success", detail="bd-e.1 closed in beads")
    )

    result = invoke("status")

    assert result.exit_code == 0
    assert "Document it" in result.output
    assert "branch  main" in result.output
    assert "success" in result.output


def test_status_names_the_workspace_herdr_still_has_open(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))
    fake_proc.expect(
        "herdr workspace list",
        Reply(
            stdout=wrapped(
                "workspace:list",
                {"workspaces": [{"workspace_id": "wY", "label": f"milhouse:{worked_repo.name}"}]},
            )
        ),
    )

    result = invoke("status")

    assert f"workspace wY (labelled milhouse:{worked_repo.name})" in result.output


def test_status_flags_a_claim_left_by_an_unfinished_run(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))
    FakeAudit(worked_repo).claimed("bd-e.2")

    result = invoke("status")

    assert "bd-e.2 is claimed by an unfinished run" in result.output


def test_dry_run_shows_the_next_iteration_prompt(worked_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect(
        "bd",
        lambda argv: Reply(stdout=json.dumps([EPIC] if "show" in argv else [CHILDREN[1]])),
    )

    result = invoke("step", "--dry-run")

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert "would work bd-e.2" in result.output
    assert "bd close bd-e.2" in result.output
    # The epic's description is the background the prompt carries now.
    assert "It should greet." in result.output
    assert not fake_proc.ran("herdr")


def test_dry_run_reports_an_empty_ready_queue(worked_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    result = invoke("step", "--dry-run")

    assert "a step would do nothing" in result.output


def test_dry_run_reports_the_resolved_config(worked_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    result = invoke("step", "--dry-run", "--agent", "codex", "--parent", "bd-e")

    assert "agent     codex" in result.output
    assert "verify    (none" in result.output
    assert "under bd-e" in result.output


def test_the_scope_flags_fence_the_ready_query(worked_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    invoke("step", "--dry-run", "--parent", "bd-e", "--label", "agent")

    ready = next(call for call in fake_proc.calls if "ready" in call)
    assert "--parent" in ready
    assert "bd-e" in ready
    assert "--label" in ready
    assert "agent" in ready


def test_a_bad_config_value_exits_two(
    worked_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MILHOUSE_TURN_TIMEOUT_MS", "soon")

    result = invoke("status")

    assert result.exit_code == 2


def test_doctor_exits_seven_when_a_required_tool_is_missing(
    worked_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc, "have", lambda tool: None)

    result = invoke("doctor")

    assert result.exit_code == 7
    assert "required checks failed" in result.output
