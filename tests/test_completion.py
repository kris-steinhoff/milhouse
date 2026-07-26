"""Tests for shell completion.

Completion runs on a keypress, so two things matter beyond offering the right
values: a callback never raises, and it never reaches a server. The tests below
cover both, and check that every parameter worth completing is actually wired
to a callback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest
import typer.main
from typer.core import TyperGroup

from milhouse import cli, completion
from milhouse.config import BranchStrategy
from milhouse.models import RunState
from milhouse.state import RunStore


@pytest.fixture
def tree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with task definitions, other files, and a dotfile, as the cwd."""
    (repo / "docs" / "tasks").mkdir(parents=True)
    (repo / "docs" / "tasks" / "hello.md").write_text("# Hello\n", encoding="utf-8")
    (repo / "docs" / "tasks" / "farewell.markdown").write_text("# Bye\n", encoding="utf-8")
    (repo / "docs" / "tasks" / "notes.txt").write_text("not a task\n", encoding="utf-8")
    (repo / "README.md").write_text("# readme\n", encoding="utf-8")
    (repo / ".hidden.md").write_text("# hidden\n", encoding="utf-8")
    monkeypatch.setattr(completion, "find_repo_root", lambda start=None: repo)
    return repo


def test_task_offers_markdown_and_directories(tree: Path) -> None:
    assert completion.complete_task("") == ["README.md", "docs/"]


def test_task_ignores_files_that_are_not_task_definitions(tree: Path) -> None:
    offered = completion.complete_task("docs/tasks/")

    assert offered == ["docs/tasks/farewell.markdown", "docs/tasks/hello.md"]


def test_task_filters_by_what_has_been_typed(tree: Path) -> None:
    assert completion.complete_task("docs/tasks/h") == ["docs/tasks/hello.md"]


def test_task_keeps_the_file_prefix_it_was_given(tree: Path) -> None:
    """Only values starting with the incomplete text survive typer's own filter."""
    assert completion.complete_task("file:docs/tasks/h") == ["file:docs/tasks/hello.md"]


def test_task_keeps_a_leading_dot_slash(tree: Path) -> None:
    assert completion.complete_task("./docs/tasks/h") == ["./docs/tasks/hello.md"]


def test_task_leaves_github_specs_alone(tree: Path) -> None:
    """Completing `gh:` would mean a network call on a keypress."""
    assert completion.complete_task("gh:owner/repo#") == []


def test_task_hides_dotfiles_until_a_dot_is_typed(tree: Path) -> None:
    assert ".hidden.md" not in completion.complete_task("")
    assert completion.complete_task(".hidden") == [".hidden.md"]


def test_task_says_nothing_about_a_directory_that_is_not_there(tree: Path) -> None:
    assert completion.complete_task("nope/what") == []


def test_repo_offers_directories_only(tree: Path) -> None:
    assert completion.complete_repo("") == ["docs/"]


def test_agent_offers_the_common_herdr_kinds() -> None:
    assert completion.complete_agent("c") == ["claude", "codex", "copilot", "cursor"]
    assert completion.complete_agent("zzz") == []


def test_branch_strategy_offers_every_strategy_the_config_accepts() -> None:
    """The values come from the config Literal, so the two cannot drift apart."""
    offered = completion.complete_branch_strategy("")

    assert [value for value, _ in offered] == list(get_args(BranchStrategy))
    assert all(help_text for _, help_text in offered)


def test_workspace_offers_ids_from_this_repo_s_runs(tree: Path) -> None:
    _save_run(tree, "hello", workspace_id="wG")
    _save_run(tree, "farewell", workspace_id="wY")

    offered = completion.complete_workspace("")

    assert offered == [("wY", "file:farewell.md"), ("wG", "file:hello.md")]


def test_workspace_reports_each_id_once(tree: Path) -> None:
    _save_run(tree, "hello", workspace_id="wG")
    _save_run(tree, "farewell", workspace_id="wG")

    assert completion.complete_workspace("w") == [("wG", "file:farewell.md")]


def test_workspace_skips_runs_that_never_opened_one(tree: Path) -> None:
    _save_run(tree, "hello", workspace_id=None)

    assert completion.complete_workspace("") == []


def test_workspace_skips_an_unreadable_state_file(tree: Path) -> None:
    _save_run(tree, "hello", workspace_id="wG")
    (tree / ".milhouse" / "runs" / "broken").mkdir(parents=True)
    (tree / ".milhouse" / "runs" / "broken" / "state.json").write_text("{", encoding="utf-8")

    assert completion.complete_workspace("") == [("wG", "file:hello.md")]


def test_workspace_says_nothing_outside_a_repository(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(start: Path | None = None) -> Path:
        raise RuntimeError("not inside a git repository")

    monkeypatch.setattr(completion, "find_repo_root", explode)

    assert completion.complete_workspace("") == []


@pytest.mark.parametrize(
    ("command", "params"),
    [
        ("step", ["task", "agent", "workspace", "branch_strategy", "repo"]),
        ("plan", ["task", "agent", "workspace", "repo"]),
        ("status", ["task", "repo"]),
        ("doctor", ["repo"]),
    ],
)
def test_every_completable_parameter_is_wired_up(command: str, params: list[str]) -> None:
    """A new flag that names a path, a repo, or an enum should complete too."""
    wired = _params(command)

    for name in params:
        assert wired[name]._custom_shell_complete is not None, f"{command} {name} has no completion"


def test_the_completion_flags_are_installed() -> None:
    """`add_completion=True` is what puts the install/show flags on the app."""
    names = {param.name for param in typer.main.get_command(cli.app).params}

    assert {"install_completion", "show_completion"} <= names


def _params(command: str) -> dict[str, Any]:
    """The click parameters of one milhouse subcommand, by name."""
    group = typer.main.get_command(cli.app)
    assert isinstance(group, TyperGroup)
    return {str(param.name): param for param in group.commands[command].params}


def _save_run(repo: Path, slug: str, *, workspace_id: str | None) -> None:
    """Write a run state, the way a real run leaves one behind."""
    RunStore(repo / ".milhouse" / "runs" / slug).save(
        RunState(task_id=f"file:{slug}.md", task_slug=slug, workspace_id=workspace_id)
    )
