"""Tests for the command line: help text, exit codes, and the read-only commands.

What `step` actually does is covered in `test_step.py`, where the decisions are.
What matters here is that every flag is documented, errors map to their exit
codes, and the commands that promise not to start anything really do not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from milhouse import cli, proc
from milhouse.models import Issue, Iteration, MergeRecord
from milhouse.parallel import Parallel
from milhouse.run import Draining, Halt, RunResult

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
                "--count",
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


def test_status_lists_a_runs_worker_lanes_under_its_integration_lane(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """Five sibling rows would leave the reader to sort out which run is which.

    The branch is what says so: a worker lane is namespaced under its
    integration branch, and a `dispatch` lane carrying the same label is not
    (ADR 0024).
    """
    fake_proc.expect("bd", Reply(stdout="[]"))
    fake_proc.expect(
        "herdr workspace list",
        Reply(
            stdout=wrapped(
                "workspace:list",
                {
                    "workspaces": [
                        {"workspace_id": "wI", "label": "bd-e"},
                        {"workspace_id": "wW1", "label": "bd-e.1"},
                        {"workspace_id": "wW2", "label": "bd-e.2"},
                        {"workspace_id": "wD", "label": "bd-x.9"},
                    ]
                },
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
                            "path": "/worktrees/milhouse-bd-e",
                            "branch": "milhouse/bd-e",
                            "open_workspace_id": "wI",
                        },
                        {
                            "path": "/worktrees/milhouse-bd-e--bd-e.1",
                            "branch": "milhouse/bd-e--bd-e.1",
                            "open_workspace_id": "wW1",
                        },
                        {
                            "path": "/worktrees/milhouse-bd-e--bd-e.2",
                            "branch": "milhouse/bd-e--bd-e.2",
                            "open_workspace_id": "wW2",
                        },
                        {
                            "path": "/worktrees/milhouse-bd-x.9",
                            "branch": "milhouse/bd-x.9",
                            "open_workspace_id": "wD",
                        },
                    ]
                },
            )
        ),
    )

    result = invoke("status")
    lines = [line for line in result.output.splitlines() if "milhouse/" in line]

    assert "lanes (4)" in result.output
    assert lines[0].startswith("  bd-e  milhouse/bd-e ")
    assert lines[1].startswith("      bd-e.1  milhouse/bd-e--bd-e.1 ")
    assert lines[2].startswith("      bd-e.2  milhouse/bd-e--bd-e.2 ")
    # A `dispatch` lane belongs to nobody's run, so it stays at the top level.
    assert lines[3].startswith("  bd-x.9  milhouse/bd-x.9 ")


def test_status_leaves_a_worker_lane_whose_run_is_gone_at_the_top_level(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """It is the leftover of a run that stopped, which is what status is read for."""
    fake_proc.expect("bd", Reply(stdout="[]"))
    fake_proc.expect(
        "herdr workspace list",
        Reply(
            stdout=wrapped(
                "workspace:list", {"workspaces": [{"workspace_id": "wW1", "label": "bd-e.1"}]}
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
                            "path": "/worktrees/milhouse-bd-e--bd-e.1",
                            "branch": "milhouse/bd-e--bd-e.1",
                            "open_workspace_id": "wW1",
                        },
                    ]
                },
            )
        ),
    )

    result = invoke("status")
    lines = [line for line in result.output.splitlines() if "milhouse/" in line]

    assert "lanes (1)" in result.output
    assert lines == ["  bd-e.1  milhouse/bd-e--bd-e.1  /worktrees/milhouse-bd-e--bd-e.1"]


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


EPIC_F = {
    "id": "bd-f",
    "title": "Add a goodbye command",
    "status": "open",
    "issue_type": "epic",
    "description": "It should also say goodbye.",
}
CHILDREN_F = [{"id": "bd-f.1", "title": "Add the subcommand", "status": "closed", "parent": "bd-f"}]


def bd_reads_two_epics(argv: tuple[str, ...]) -> Reply:
    """`bd show` and `bd list --parent` answer for whichever of two epics is asked."""
    if "show" in argv:
        target = argv[argv.index("show") + 1]
        beads = {issue["id"]: issue for issue in (EPIC, EPIC_F, *CHILDREN, *CHILDREN_F)}
        return Reply(stdout=json.dumps([beads[target]]))
    if "--parent" in argv:
        parent = argv[argv.index("--parent") + 1]
        children = {"bd-e": CHILDREN, "bd-f": CHILDREN_F}[parent]
        return Reply(stdout=json.dumps(children))
    return Reply(stdout=json.dumps([CHILDREN[1]]))


def test_run_dry_run_shows_several_targets_and_their_union(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """Given more than one target, the union of both is what the run would work."""
    fake_proc.expect("bd", bd_reads_two_epics)

    result = invoke("run", "bd-e", "bd-f", "--dry-run")

    assert result.exit_code == 0
    assert "targets (2)" in result.output
    assert "  bd-e  Add a hello command" in result.output
    assert "  bd-f  Add a goodbye command" in result.output
    assert "scope     bd-e, bd-f (3 issue(s) total)" in result.output
    assert "lane      milhouse/bd-e+bd-f  (one lane for the whole run)" in result.output


# -- what the count is worth ---------------------------------------------------

WIDE = [
    {"id": "bd-e.1", "title": "Left", "status": "open", "parent": "bd-e"},
    {"id": "bd-e.2", "title": "Right", "status": "open", "parent": "bd-e"},
    {"id": "bd-e.3", "title": "The join", "status": "open", "parent": "bd-e"},
]
JOIN = [
    {"issue_id": "bd-e.3", "depends_on_id": "bd-e.1", "type": "blocks"},
    {"issue_id": "bd-e.3", "depends_on_id": "bd-e.2", "type": "blocks"},
]


def bd_has_a_join(argv: tuple[str, ...]) -> Reply:
    """Two independent issues and one waiting on both: two waves, widest two."""
    if "show" in argv:
        target = argv[argv.index("show") + 1]
        found = EPIC if target == "bd-e" else next(c for c in WIDE if c["id"] == target)
        return Reply(stdout=json.dumps([found]))
    if "dep" in argv:
        return Reply(stdout=json.dumps(JOIN))
    if "ready" in argv:
        return Reply(stdout=json.dumps([WIDE[0]]))
    return Reply(stdout=json.dumps(WIDE))


def test_run_dry_run_prints_the_waves_and_the_widest_one(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """The one place the dependency graph earns its keep on the run path."""
    fake_proc.expect("bd", bd_has_a_join)

    result = invoke("run", "bd-e", "--dry-run", "--count", "2")

    assert result.exit_code == 0
    assert "waves     3 unfinished issue(s) in 2 wave(s), widest 2" in result.output
    assert "  1  bd-e.1, bd-e.2" in result.output
    assert "  2  bd-e.3" in result.output
    assert "count     2 turns at once, and the widest wave is 2" in result.output
    assert not fake_proc.ran("herdr", "agent")


def test_run_dry_run_says_a_count_the_target_cannot_use_buys_nothing(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """`--count 8` against a chain of eight is `--count 1` with extra words."""
    fake_proc.expect("bd", bd_has_a_join)

    result = invoke("run", "bd-e", "--dry-run", "--count", "8")

    assert "8 requested, but no wave is wider than 2" in result.output
    assert "--count 2 with extra words" in result.output


def test_run_dry_run_at_the_default_count_says_what_the_width_would_be(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """Serial is the default, so the dry run is where a wider target is noticed."""
    fake_proc.expect("bd", bd_has_a_join)

    result = invoke("run", "bd-e", "--dry-run")

    assert "count     1 turn at a time, in the integration lane, with no worker lanes" in (
        result.output
    )
    assert "--count up to 2 would fit this target" in result.output


def test_run_dry_run_names_the_worker_lane_the_next_issue_would_work_in(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """Above a width of one the target's lane is the integration lane (ADR 0024)."""
    fake_proc.expect("bd", bd_has_a_join)

    result = invoke("run", "bd-e", "--dry-run", "--count", "2")

    assert (
        "lane      milhouse/bd-e  (the integration lane; "
        "bd-e.1 would work on milhouse/bd-e--bd-e.1)" in result.output
    )
    # And the prompt names the branch the turn would really commit to.
    assert "milhouse/bd-e--bd-e.1" in result.output.split("would work bd-e.1 and send")[1]


def test_run_dry_run_reads_the_width_from_the_config_file(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    """`--count` and `[run] max_parallel` are the same setting under two names."""
    fake_proc.expect("bd", bd_has_a_join)
    config = worked_repo / ".milhouse"
    config.mkdir(exist_ok=True)
    (config / "config.toml").write_text("[run]\nmax_parallel = 2\n", encoding="utf-8")

    result = invoke("run", "bd-e", "--dry-run")

    assert "count     2 turns at once, and the widest wave is 2" in result.output


def test_a_step_dry_run_says_nothing_about_waves(worked_repo: Path, fake_proc: FakeProc) -> None:
    """A step has no width to plan, so the graph has nothing to tell it."""
    fake_proc.expect("bd", bd_has_a_join)

    result = invoke("step", "--dry-run")

    assert "waves" not in result.output


def test_run_dry_run_says_so_when_nothing_in_scope_is_unfinished(
    worked_repo: Path, fake_proc: FakeProc
) -> None:
    fake_proc.expect("bd", bd_is_all_closed)

    result = invoke("run", "bd-e", "--dry-run")

    assert "waves     (none — nothing in scope is unfinished)" in result.output


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


def test_run_with_several_targets_opens_one_lane_keyed_by_both(
    worked_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The union is one scope worked in one lane, not two runs (ADR 0025)."""
    fake_proc.expect("bd", bd_reads_two_epics)
    seen = watch_the_run(monkeypatch)

    result = invoke("run", "bd-e", "bd-f")

    assert result.exit_code == 0
    assert seen["session"]["lane_key"] == "bd-e+bd-f"
    assert "targets (2)" in result.output
    assert "  bd-e  Add a hello command" in result.output
    assert "  bd-f  Add a goodbye command" in result.output
    assert "bd-e, bd-f: 0 issue(s) closed — everything closed" in result.output


# -- what --count wires up -----------------------------------------------------


class SessionSpy:
    """A session that opens nothing, so the wiring can be read without a herdr."""

    def __init__(self, **kwargs: object) -> None:
        """Remember how `milhouse run` asked for a session."""
        self.kwargs = kwargs
        self.lanes = self

    def locate(self, key: str) -> None:
        """No lane, so the report has no branch to name."""
        return None

    def __enter__(self) -> SessionSpy:
        """Open nothing: no workspace, no worktree, no lock."""
        return self

    def __exit__(self, *exc: object) -> bool:
        """Close nothing, and swallow nothing."""
        return False


def watch_the_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the session and the loop body `milhouse run` assembles."""
    seen: dict[str, Any] = {}

    def session(config: object, **kwargs: object) -> SessionSpy:
        seen["session"] = kwargs
        return SessionSpy(**kwargs)

    def loop(opened: object, targets: tuple[Issue, ...], **kwargs: Any) -> RunResult:
        seen.update(kwargs)
        return RunResult(targets=targets, halt=Halt("finished", "everything closed", finished=True))

    monkeypatch.setattr(cli, "_session", session)
    monkeypatch.setattr(cli, "run_loop", loop)
    return seen


def test_a_count_above_one_opens_worker_lanes_and_runs_a_concurrent_body(
    worked_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The width is a mode for the session and a different body for the loop."""
    fake_proc.expect("bd", bd_reads_the_tree)
    seen = watch_the_run(monkeypatch)

    result = invoke("run", "bd-e", "--count", "4")

    assert result.exit_code == 0
    assert seen["session"]["worker_lanes"] is True
    assert isinstance(seen["body"], Parallel)
    assert seen["body"].count == 4
    assert "count   4 turns in flight at once, each in a worker lane" in result.output


def test_the_poll_interval_reaches_the_concurrent_body(
    worked_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_proc.expect("bd", bd_reads_the_tree)
    monkeypatch.setenv("MILHOUSE_RUN_POLL_MS", "250")
    seen = watch_the_run(monkeypatch)

    invoke("run", "bd-e", "--count", "2")

    assert isinstance(seen["body"], Parallel)
    assert seen["body"].poll_ms == 250


def test_count_one_is_the_serial_run_unchanged(
    worked_repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worker lanes, nothing to drain, and no line about a width (ADR 0023)."""
    fake_proc.expect("bd", bd_reads_the_tree)
    seen = watch_the_run(monkeypatch)

    result = invoke("run", "bd-e", "--count", "1")

    assert seen["session"]["worker_lanes"] is False
    assert not isinstance(seen["body"], Draining)
    assert "in flight at once" not in result.output


def merged_turn(
    number: int,
    issue_id: str,
    merge: MergeRecord,
    *,
    integration_verified: bool | None = None,
) -> Iteration:
    """One successful turn of a concurrent run, with what became of its branch."""
    return Iteration(
        number=number,
        issue_id=issue_id,
        outcome="success",
        detail="closed and verified",
        merge=merge,
        integration_verified=integration_verified,
    )


def test_the_run_report_says_what_landed_on_the_integration_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Closed and on the branch you are about to review stop being the same thing."""
    result = RunResult(
        targets=(Issue(id="bd-e", title="Add a hello command", status="open"),),
        halt=Halt("conflict", "bd-e.2 is closed but its work is not on milhouse/bd-e"),
        iterations=[
            merged_turn(
                1,
                "bd-e.1",
                MergeRecord(source="milhouse/bd-e--bd-e.1", target="milhouse/bd-e", sha="a" * 40),
            ),
            merged_turn(
                2,
                "bd-e.2",
                MergeRecord(
                    source="milhouse/bd-e--bd-e.2",
                    target="milhouse/bd-e",
                    conflicts=["src/a.py"],
                ),
            ),
        ],
    )

    cli._print_run(result, lane=None)

    output = capsys.readouterr().out
    assert "merged (1)" in output
    assert "bd-e.1  merged milhouse/bd-e--bd-e.1 into milhouse/bd-e" in output
    assert "not merged (1)" in output
    assert "src/a.py" in output
    assert "Land it by hand." in output
    # Two issues closed, one of them on the branch and one of them not. The
    # summary says all three numbers, because "2 closed, 1 merged" leaves the
    # reader to notice the missing one.
    assert "bd-e: 2 issue(s) closed, 1 merged, 1 not merged —" in output


def test_the_run_report_tells_the_two_kinds_of_unlanded_branch_apart(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fifth watched run's report: four closed issues, one of them on the branch.

    One conflict stopped the run, and the two turns the drain finished were
    never merged. A reader has to be able to tell which is which and what to do
    about each (ADR 0024).
    """
    conflicted = MergeRecord(
        source="milhouse/bd-e--bd-e.2",
        target="milhouse/bd-e",
        conflicts=["src/a.py", "src/b.py", "tests/t.py"],
    )
    result = RunResult(
        targets=(Issue(id="bd-e", title="Add a hello command", status="open"),),
        halt=Halt("conflict", "bd-e.2 is closed but its work is not on milhouse/bd-e"),
        iterations=[
            merged_turn(
                1,
                "bd-e.1",
                MergeRecord(
                    source="milhouse/bd-e--bd-e.1",
                    target="milhouse/bd-e",
                    sha="a" * 40,
                    fast_forwarded=True,
                ),
            ),
            merged_turn(2, "bd-e.2", conflicted),
            *[
                merged_turn(
                    number,
                    f"bd-e.{number}",
                    MergeRecord(
                        source=f"milhouse/bd-e--bd-e.{number}",
                        target="milhouse/bd-e",
                        skipped="milhouse/bd-e--bd-e.2 did not land in milhouse/bd-e",
                    ),
                )
                for number in (3, 4)
            ],
        ],
    )

    cli._print_run(result, lane=None)

    output = capsys.readouterr().out
    assert "merged (1)" in output
    assert "not merged (3)" in output
    # The one git refused says so, and names every file.
    assert "bd-e.2  milhouse/bd-e--bd-e.2 conflicts with milhouse/bd-e in 3 file(s)" in output
    # The two nobody attempted say which branch has to be landed first.
    assert "bd-e.3  milhouse/bd-e--bd-e.3 was not merged into milhouse/bd-e" in output
    assert output.count("because milhouse/bd-e--bd-e.2 did not land") == 2
    assert "Land them by hand in the order above" in output
    assert "bd-e: 4 issue(s) closed, 1 merged, 3 not merged —" in output


def test_the_run_report_says_which_merge_made_the_branch_red(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The line above says the merge succeeded, which is true and not the whole story."""
    result = RunResult(
        targets=(Issue(id="bd-e", title="Add a hello command", status="open"),),
        halt=Halt("integration", "the gate failed on milhouse/bd-e once bd-e.1 was merged"),
        iterations=[
            merged_turn(
                1,
                "bd-e.1",
                MergeRecord(source="milhouse/bd-e--bd-e.1", target="milhouse/bd-e", sha="a" * 40),
                integration_verified=False,
            )
        ],
    )

    cli._print_run(result, lane=None)

    output = capsys.readouterr().out
    assert "merged (1)" in output
    assert "integration gate failed (1)" in output
    assert "bd-e.1  the gate failed once this merge was on the branch" in output
    assert "Nothing was reverted" in output


def test_the_run_report_names_the_agents_that_were_still_working(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Numbers that look complete while two agents are still running is the worse report."""
    result = RunResult(
        targets=(Issue(id="bd-e", title="Add a hello command", status="open"),),
        halt=Halt("blocked", "the agent stopped waiting on a human"),
        iterations=[merged_turn(1, "bd-e.1", MergeRecord(source="w", target="i", sha="b" * 40))],
        still_running=["bd-e.4", "bd-e.5"],
    )

    cli._print_run(result, lane=None)

    output = capsys.readouterr().out
    assert "still running (2)" in output
    assert "bd-e.4" in output
    assert "does not merge it" in output
    assert "1 issue(s) closed, 1 merged, 2 still running —" in output


def test_a_serial_run_report_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    """Nothing merges into the lane a serial run works in, so nothing is said about it."""
    result = RunResult(
        targets=(Issue(id="bd-e", title="Add a hello command", status="open"),),
        halt=Halt("finished", "everything in scope is closed", finished=True),
        iterations=[
            Iteration(number=1, issue_id="bd-e.1", outcome="success", detail="closed"),
        ],
    )

    cli._print_run(result, lane=None)

    output = capsys.readouterr().out
    assert "merged" not in output
    assert "still running" not in output
    assert "bd-e: 1 issue(s) closed — everything in scope is closed" in output


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
