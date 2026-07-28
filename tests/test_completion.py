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


@pytest.fixture
def tree(repo: Path) -> Path:
    """A repo with a directory, some files, and a dotfile, as the cwd."""
    (repo / "docs" / "tasks").mkdir(parents=True)
    (repo / "README.md").write_text("# readme\n", encoding="utf-8")
    (repo / ".hidden.md").write_text("# hidden\n", encoding="utf-8")
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


def test_workspace_has_no_callback() -> None:
    """Nothing local knows a workspace id any more, and herdr is a server call."""
    assert _params("step")["workspace"]._custom_shell_complete is None


@pytest.mark.parametrize(
    ("command", "params"),
    [
        ("step", ["agent", "repo"]),
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
