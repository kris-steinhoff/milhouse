"""Tests for resolving task specs into task definitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from milhouse import proc, sources
from milhouse.errors import MissingDependencyError, SourceError

from .fakes import FakeProc, Reply

TASK_BODY = """\
# Add a hello command

Add a `hello` subcommand that prints a greeting, with a test and a docs entry.
"""


@pytest.fixture
def task_file(repo: Path) -> Path:
    path = repo / "docs" / "tasks" / "hello.md"
    path.parent.mkdir(parents=True)
    path.write_text(TASK_BODY, encoding="utf-8")
    return path


def test_a_repo_relative_path_resolves(repo: Path, task_file: Path) -> None:
    task = sources.resolve("docs/tasks/hello.md", repo)

    assert task.task_id == "file:docs/tasks/hello.md"
    assert task.title == "Add a hello command"
    assert task.slug == "hello"
    assert task.kind == "file"
    assert task.body == TASK_BODY
    assert task.external_ref is None


def test_the_file_prefix_is_accepted(repo: Path, task_file: Path) -> None:
    assert sources.resolve("file:docs/tasks/hello.md", repo).task_id == "file:docs/tasks/hello.md"


def test_an_absolute_path_resolves(repo: Path, task_file: Path) -> None:
    assert sources.resolve(str(task_file), repo).task_id == "file:docs/tasks/hello.md"


def test_the_filename_is_the_title_without_a_heading(repo: Path) -> None:
    path = repo / "plain.md"
    path.write_text("just a paragraph\n", encoding="utf-8")

    assert sources.resolve("plain.md", repo).title == "plain"


def test_a_missing_file_is_a_source_error(repo: Path) -> None:
    with pytest.raises(SourceError, match="no such task definition"):
        sources.resolve("nope.md", repo)


def test_a_directory_is_a_source_error(repo: Path, task_file: Path) -> None:
    with pytest.raises(SourceError, match="is a directory"):
        sources.resolve("docs", repo)


def test_an_empty_file_is_a_source_error(repo: Path) -> None:
    (repo / "empty.md").write_text("\n\n", encoding="utf-8")

    with pytest.raises(SourceError, match="empty"):
        sources.resolve("empty.md", repo)


def test_an_empty_spec_is_a_source_error(repo: Path) -> None:
    with pytest.raises(SourceError, match="no task definition given"):
        sources.resolve("   ", repo)


ISSUE_JSON = json.dumps(
    {
        "title": "Add a hello command",
        "body": "It should print a greeting.",
        "number": 123,
        "url": "https://github.com/kris-steinhoff/milhouse/issues/123",
    }
)


@pytest.fixture
def gh_available(monkeypatch: pytest.MonkeyPatch, fake_proc: FakeProc) -> FakeProc:
    monkeypatch.setattr(proc, "have", lambda tool: f"/usr/bin/{tool}")
    fake_proc.expect("gh issue view", Reply(stdout=ISSUE_JSON))
    return fake_proc


def test_a_qualified_github_spec_resolves(repo: Path, gh_available: FakeProc) -> None:
    task = sources.resolve("gh:kris-steinhoff/milhouse#123", repo)

    assert task.task_id == "gh:kris-steinhoff/milhouse#123"
    assert task.external_ref == "gh-123"
    assert task.kind == "github"
    assert task.title == "Add a hello command"
    assert task.url == "https://github.com/kris-steinhoff/milhouse/issues/123"
    assert next(gh_available.commands("gh"))[:4] == ("gh", "issue", "view", "123")
    assert "--repo" in next(gh_available.commands("gh"))


def test_a_bare_number_uses_the_current_repo(repo: Path, gh_available: FakeProc) -> None:
    task = sources.resolve("gh:123", repo)

    # No --repo flag: gh infers it from the working directory.
    assert "--repo" not in next(gh_available.commands("gh"))
    # The owner and repo come back from the issue URL instead.
    assert task.task_id == "gh:kris-steinhoff/milhouse#123"


def test_an_issue_url_resolves(repo: Path, gh_available: FakeProc) -> None:
    spec = "gh:https://github.com/kris-steinhoff/milhouse/issues/123"

    assert sources.resolve(spec, repo).task_id == "gh:kris-steinhoff/milhouse#123"


def test_a_malformed_github_spec_is_a_source_error(repo: Path, gh_available: FakeProc) -> None:
    with pytest.raises(SourceError, match="expected gh:owner/repo#123"):
        sources.resolve("gh:not-a-number", repo)


def test_a_gh_failure_is_a_source_error(repo: Path, gh_available: FakeProc) -> None:
    gh_available.expect("gh issue view", Reply(stderr="issue not found\n", returncode=1))

    with pytest.raises(SourceError, match="could not read GitHub issue"):
        sources.resolve("gh:kris-steinhoff/milhouse#404", repo)


def test_a_missing_gh_is_a_dependency_error(
    repo: Path, fake_proc: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc, "have", lambda tool: None)

    with pytest.raises(MissingDependencyError, match="gh is required"):
        sources.resolve("gh:kris-steinhoff/milhouse#123", repo)
