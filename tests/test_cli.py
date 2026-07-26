"""Tests for the command line: help text, exit codes, and the read-only commands.

What `run` and `step` actually do is covered in `test_loop.py` and
`test_step.py`, where the decisions are. What matters here is that every flag is
documented, errors map to their exit codes, and the commands that promise not to
start anything really do not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from milhouse import cli, proc
from milhouse.models import Iteration, RunState
from milhouse.state import RunStore

from .fakes import FakeProc, Reply

runner = CliRunner()

EPIC = {
    "id": "bd-e",
    "title": "Add a hello command",
    "status": "open",
    "issue_type": "epic",
    "metadata": {"milhouse_task": "file:hello.md"},
}
CHILDREN = [
    {"id": "bd-e.1", "title": "Add the subcommand", "status": "closed", "parent": "bd-e"},
    {"id": "bd-e.2", "title": "Document it", "status": "open", "parent": "bd-e"},
]


@pytest.fixture
def task_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo containing one task definition, with git discovery stubbed out."""
    (repo / "hello.md").write_text("# Add a hello command\n\nGreet.\n", encoding="utf-8")
    monkeypatch.setattr(cli, "find_repo_root", lambda path: repo)
    return repo


def invoke(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def test_help_lists_every_command() -> None:
    result = invoke("--help")

    assert result.exit_code == 0
    for command in ("doctor", "run", "step", "plan", "status"):
        assert command in result.output


@pytest.mark.parametrize(
    ("command", "flags"),
    [
        (
            "run",
            [
                "--max-iterations",
                "--agent",
                "--workspace",
                "--branch-strategy",
                "--dry-run",
                "--attach",
                "--yes",
                "--repo",
            ],
        ),
        (
            "step",
            ["--agent", "--workspace", "--branch-strategy", "--attach", "--yes", "--repo"],
        ),
        ("plan", ["--yes", "--workspace", "--agent", "--repo"]),
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


@pytest.mark.parametrize("command", [None, "run", "step", "plan", "status", "doctor"])
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


def test_a_missing_task_definition_exits_three(task_repo: Path, fake_proc: FakeProc) -> None:
    result = invoke("status", "nope.md")

    assert result.exit_code == 3
    assert "no such task definition" in result.output


def test_status_reports_an_undecomposed_task(task_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    result = invoke("status", "hello.md")

    assert result.exit_code == 0
    assert "file:hello.md" in result.output
    assert "not decomposed yet" in result.output


def test_status_shows_the_tree_and_the_history(
    task_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_proc.expect(
        "bd",
        lambda argv: Reply(stdout=json.dumps(CHILDREN if "--parent" in argv else [EPIC])),
    )
    store = RunStore(task_repo / ".milhouse" / "runs" / "hello")
    store.save(RunState(task_id="file:hello.md", task_slug="hello", branch="milhouse/hello"))
    store.append(
        Iteration(number=1, issue_id="bd-e.1", outcome="success", detail="bd-e.1 closed in beads")
    )

    result = invoke("status", "hello.md")

    assert result.exit_code == 0
    assert "bd-e" in result.output
    assert "Document it" in result.output
    assert "milhouse/hello" in result.output
    assert "success" in result.output


def test_status_flags_a_claim_left_by_an_unfinished_run(
    task_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))
    RunStore(task_repo / ".milhouse" / "runs" / "hello").save(
        RunState(task_id="file:hello.md", task_slug="hello", claimed_issue="bd-e.2")
    )

    result = invoke("status", "hello.md")

    assert "bd-e.2 is claimed by an unfinished run" in result.output


def test_dry_run_shows_the_planning_prompt_and_starts_nothing(
    task_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    result = invoke("run", "hello.md", "--dry-run")

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert "milhouse/hello" in result.output
    assert "Do not run `bd`" in result.output
    assert not fake_proc.ran("herdr")


def test_dry_run_shows_the_next_iteration_prompt_when_decomposed(
    task_repo: Path, fake_proc: FakeProc
) -> None:
    ready = [dict(CHILDREN[1])]
    fake_proc.expect(
        "bd",
        lambda argv: Reply(stdout=json.dumps(ready if "ready" in argv else [EPIC])),
    )

    result = invoke("run", "hello.md", "--dry-run")

    assert result.exit_code == 0
    assert "would work bd-e.2" in result.output
    assert "bd close bd-e.2" in result.output
    assert not fake_proc.ran("herdr")


def test_dry_run_reports_an_epic_with_nothing_ready(task_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect(
        "bd",
        lambda argv: Reply(stdout=json.dumps([] if "ready" in argv else [EPIC])),
    )

    result = invoke("run", "hello.md", "--dry-run")

    assert "would finish immediately" in result.output


def test_dry_run_honours_the_budget_it_reports(task_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    result = invoke("run", "hello.md", "--dry-run", "--max-iterations", "3", "--agent", "codex")

    assert "3 iterations for one run" in result.output
    assert "agent     codex" in result.output


def test_plan_prints_the_existing_tree_without_replanning(
    task_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect(
        "bd",
        lambda argv: Reply(stdout=json.dumps(CHILDREN if "--parent" in argv else [EPIC])),
    )

    result = invoke("plan", "hello.md")

    assert result.exit_code == 0
    assert "already decomposed as bd-e" in result.output
    assert "Document it" in result.output
    assert not fake_proc.ran("herdr")


def test_a_bad_config_value_exits_two(
    task_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MILHOUSE_BRANCH_STRATEGY", "sideways")

    result = invoke("status", "hello.md")

    assert result.exit_code == 2


def test_doctor_exits_seven_when_a_required_tool_is_missing(
    task_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc, "have", lambda tool: None)

    result = invoke("doctor")

    assert result.exit_code == 7
    assert "required checks failed" in result.output
