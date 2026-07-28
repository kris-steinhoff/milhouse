"""Tests for shell completion.

Completion runs on a keypress, so two things matter beyond offering the right
values: a callback never raises, and it never reaches a server. The tests below
cover both, and check that every parameter worth completing is actually wired
to a callback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer.core import TyperGroup

from milhouse import cli, completion
from milhouse.models import RunState
from milhouse.state import RunStore


@pytest.fixture
def tree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with a directory, some files, and a dotfile, as the cwd."""
    (repo / "docs" / "tasks").mkdir(parents=True)
    (repo / "README.md").write_text("# readme\n", encoding="utf-8")
    (repo / ".hidden.md").write_text("# hidden\n", encoding="utf-8")
    monkeypatch.setattr(completion, "find_repo_root", lambda start=None: repo)
    return repo


def test_repo_offers_directories_only(tree: Path) -> None:
    assert completion.complete_repo("") == ["docs/"]


def test_repo_filters_by_what_has_been_typed(tree: Path) -> None:
    assert completion.complete_repo("docs/") == ["docs/tasks/"]


def test_repo_hides_dotfiles_until_a_dot_is_typed(tree: Path) -> None:
    assert ".hidden.md" not in completion.complete_repo("")


def test_repo_says_nothing_about_a_directory_that_is_not_there(tree: Path) -> None:
    assert completion.complete_repo("nope/what") == []


def test_agent_offers_the_common_herdr_kinds() -> None:
    assert completion.complete_agent("c") == ["claude", "codex", "copilot", "cursor"]
    assert completion.complete_agent("zzz") == []


def test_workspace_offers_the_id_this_repo_s_run_used(tree: Path) -> None:
    _save_run(tree, workspace_id="wG", branch="main")

    assert completion.complete_workspace("") == [("wG", "main")]


def test_workspace_filters_by_what_has_been_typed(tree: Path) -> None:
    _save_run(tree, workspace_id="wG")

    assert completion.complete_workspace("wY") == []


def test_workspace_says_nothing_when_no_run_opened_one(tree: Path) -> None:
    _save_run(tree, workspace_id=None)

    assert completion.complete_workspace("") == []


def test_workspace_says_nothing_before_the_first_run(tree: Path) -> None:
    assert completion.complete_workspace("") == []


def test_workspace_skips_an_unreadable_state_file(tree: Path) -> None:
    (tree / ".milhouse" / "runs").mkdir(parents=True)
    (tree / ".milhouse" / "runs" / "state.json").write_text("{", encoding="utf-8")

    assert completion.complete_workspace("") == []


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
        ("step", ["agent", "workspace", "repo"]),
        ("status", ["repo"]),
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


def _save_run(repo: Path, *, workspace_id: str | None, branch: str | None = None) -> None:
    """Write a run state, the way a real run leaves one behind."""
    RunStore(repo / ".milhouse" / "runs").save(RunState(workspace_id=workspace_id, branch=branch))
