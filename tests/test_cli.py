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
NO_LANES = wrapped("worktree:list", {"worktrees": []})
WORKSPACE_CREATED = wrapped(
    "workspace:create",
    {"workspace": {"workspace_id": "wG"}, "root_pane": {"pane_id": "wG:p1"}},
)

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
    fake_proc.expect("herdr worktree list", Reply(stdout=NO_LANES))
    fake_proc.expect("herdr workspace create", Reply(stdout=WORKSPACE_CREATED))
    (repo / ".beads").mkdir()
    return repo


def invoke(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def test_help_lists_every_command() -> None:
    result = invoke("--help")

    assert result.exit_code == 0
    for command in ("doctor", "step", "run", "dispatch", "reap", "status"):
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
        (
            "run",
            [
                "--max-iterations",
                "--max-attempts",
                "--agent",
                "--workspace",
                "--dry-run",
                "--attach",
                "--repo",
            ],
        ),
        (
            "dispatch",
            ["--count", "--agent", "--workspace", "--parent", "--label", "--attach", "--repo"],
        ),
        ("reap", ["--repo"]),
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


@pytest.mark.parametrize("command", [None, "step", "run", "dispatch", "reap", "status", "doctor"])
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


def test_status_lists_the_lanes_herdr_is_holding(worked_repo: Path, fake_proc: FakeProc) -> None:
    """No lane state is kept, so this is a read of herdr's registry."""
    fake_proc.expect("bd", Reply(stdout="[]"))
    fake_proc.expect(
        "herdr workspace list",
        Reply(
            stdout=wrapped(
                "workspace:list", {"workspaces": [{"workspace_id": "wL1", "label": "bd-e.1"}]}
            )
        ),
    )
    fake_proc.expect(
        "herdr worktree list",
        Reply(
            stdout=wrapped(
                "worktree:list",
                {
                    "worktrees": [
                        {"path": str(worked_repo), "branch": "main", "open_workspace_id": "wG"},
                        {
                            "path": "/worktrees/milhouse-bd-e.1",
                            "branch": "milhouse/bd-e.1",
                            "open_workspace_id": "wL1",
                        },
                    ]
                },
            )
        ),
    )

    result = invoke("status")

    assert "lanes (1)" in result.output
    assert "bd-e.1  milhouse/bd-e.1  /worktrees/milhouse-bd-e.1" in result.output


def test_status_flags_a_claim_left_by_an_unfinished_run(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))
    FakeAudit(worked_repo).claimed("bd-e.2")

    result = invoke("status")

    assert "bd-e.2 is claimed by an unfinished run" in result.output


def bd_reads_the_tree(argv: tuple[str, ...]) -> Reply:
    """Answer `bd show <id>` from the fixture tree, and anything else with the ready issue."""
    if "show" in argv:
        target = argv[argv.index("show") + 1]
        found = EPIC if target == "bd-e" else next(c for c in CHILDREN if c["id"] == target)
        return Reply(stdout=json.dumps([found]))
    return Reply(stdout=json.dumps([CHILDREN[1]]))


def test_dry_run_shows_the_next_iteration_prompt(worked_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", bd_reads_the_tree)

    result = invoke("step", "--dry-run")

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert "would work bd-e.2" in result.output
    assert "bd close bd-e.2" in result.output
    # The epic's description is the background the prompt carries now.
    assert "It should greet." in result.output
    assert not fake_proc.ran("herdr", "agent")


def test_dry_run_names_the_lane_the_next_step_would_use(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", bd_reads_the_tree)

    result = invoke("step", "--dry-run")

    assert "lane      milhouse/bd-e.2  (a new lane)" in result.output


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


def test_reap_says_so_when_there_is_nothing_to_collect(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    result = invoke("reap")

    assert result.exit_code == 0
    assert "nothing to reap" in result.output
    assert not fake_proc.ran("herdr", "agent")


def test_reap_exits_nine_while_a_turn_is_still_running(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))
    audit = FakeAudit(worked_repo)
    audit.claimed("bd-e.2")
    audit.dispatched("bd-e.2", {"number": 1, "head_before": "abc"})

    result = invoke("reap")

    assert result.exit_code == 9
    assert "still running" in result.output


def test_dispatch_exits_nine_when_the_queue_is_stuck(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect(
        "bd", lambda argv: Reply(stdout=json.dumps([] if "ready" in argv else CHILDREN))
    )

    result = invoke("dispatch")

    assert result.exit_code == 9
    assert "unfinished" in result.output


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


# -- run -----------------------------------------------------------------------


def test_run_needs_a_target() -> None:
    """The target is the scope, so there is nothing sensible to default it to."""
    result = invoke("run")

    assert result.exit_code != 0


@pytest.mark.parametrize("flag", ["--parent", "--label"])
def test_run_takes_no_scope_flags(flag: str) -> None:
    """They would be a second answer to the question the target already answers."""
    result = invoke("run", "bd-e", flag, "bd-x")

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_run_dry_run_shows_the_target_scope_and_caps(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", bd_reads_the_tree)

    result = invoke("run", "bd-e", "--dry-run", "--max-iterations", "9")

    assert result.exit_code == 0
    assert "target    bd-e  Add a hello command" in result.output
    assert "every ready issue under bd-e" in result.output
    assert "caps      9 iterations, 3 attempts per issue" in result.output
    assert "would work bd-e.2" in result.output
    assert not fake_proc.ran("herdr", "agent")


def test_run_dry_run_names_the_one_lane_the_whole_run_uses(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """Named after the target, not the issue, so the run lands on one branch."""
    fake_proc.expect("bd", bd_reads_the_tree)

    result = invoke("run", "bd-e", "--dry-run")

    assert "lane      milhouse/bd-e  (one lane for the whole run)" in result.output


def test_run_refuses_a_closed_target(worked_repo: Path, fake_proc: FakeProc) -> None:
    closed = dict(EPIC, status="closed")
    fake_proc.expect("bd", Reply(stdout=json.dumps([closed])))

    result = invoke("run", "bd-e")

    assert result.exit_code == 1
    assert "already closed" in result.output


def test_run_refuses_a_target_that_does_not_exist(worked_repo: Path, fake_proc: FakeProc) -> None:
    fake_proc.expect("bd", Reply(stdout="[]"))

    result = invoke("run", "bd-nope")

    assert result.exit_code == 4
    assert "no such target: bd-nope" in result.output
    assert "bd list" in result.output


def test_run_reports_a_finished_target_and_exits_zero(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """Everything under the epic is closed, so the run stops without a turn."""
    fake_proc.expect("bd", bd_is_all_closed)

    result = invoke("run", "bd-e")

    assert result.exit_code == 0
    assert "bd-e: 0 issue(s) closed" in result.output
    assert "everything in scope is closed" in result.output
    assert not fake_proc.ran("herdr", "agent")


def test_run_exits_nine_when_work_is_left(worked_repo: Path, fake_proc: FakeProc) -> None:
    """Nothing ready, but the tree still has an open issue in it."""
    fake_proc.expect("bd", bd_is_stuck)

    result = invoke("run", "bd-e")

    assert result.exit_code == 9
    assert "unfinished" in result.output


def bd_is_all_closed(argv: tuple[str, ...]) -> Reply:
    """`bd show` answers from the tree; `ready` is empty and `list` is all closed."""
    if "show" in argv:
        return Reply(stdout=json.dumps([EPIC]))
    if "ready" in argv:
        return Reply(stdout="[]")
    return Reply(stdout=json.dumps([dict(child, status="closed") for child in CHILDREN]))


def bd_is_stuck(argv: tuple[str, ...]) -> Reply:
    """Nothing is ready, and one issue is still open, which is a deadlock."""
    if "show" in argv:
        return Reply(stdout=json.dumps([EPIC]))
    if "ready" in argv:
        return Reply(stdout="[]")
    return Reply(stdout=json.dumps(CHILDREN))
