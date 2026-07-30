"""Tests for the small amount of git milhouse needs.

The reads check the argv, because that is where a wrong flag turns into a wrong
outcome: `commits_between` is what decides `partial` from `stalled`.

`merge` is checked against a real repository in `tmp_path` instead. What it
reports — fast-forward or merge commit, and what conflicted — is git's answer to
a question about two histories, and a fake that returned those answers would be
asserting on itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from milhouse.errors import MilhouseError
from milhouse.gitrepo import GitRepo

from .fakes import FakeProc, Reply

REPO = Path("/repo")


def repo() -> GitRepo:
    return GitRepo(REPO)


def git(path: Path, *args: str) -> None:
    """Run a git command in `path`, failing the test if it does not succeed."""
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def rev(path: Path, ref: str) -> str:
    """Full sha of `ref` in the repository at `path`."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit(path: Path, message: str, **files: str) -> None:
    """Write `files` into `path` and commit them."""
    for name, text in files.items():
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)


def a_repo(tmp_path: Path) -> GitRepo:
    """A real repository on `main` with one commit, bound to its own root."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "config", "user.email", "milhouse@example.invalid")
    git(root, "config", "user.name", "milhouse tests")
    # git's own default, pinned so a developer whose global config sets
    # merge.ff = false does not read a different answer than CI does.
    git(root, "config", "merge.ff", "true")
    commit(root, "first", shared="one\n")
    return GitRepo(root)


def a_branch(repo: GitRepo, name: str, **files: str) -> None:
    """Branch off the current commit, commit `files` on it, and come back."""
    was = repo.current_branch()
    assert was is not None
    repo.ensure_branch(name)
    commit(repo.path, f"work on {name}", **files)
    repo.ensure_branch(was)


def test_every_read_is_scoped_to_the_bound_directory(fake_proc: FakeProc) -> None:
    """`git -C <path>`, so a worktree answers for itself and not for the root."""
    fake_proc.expect("git", Reply(stdout="abc1234\n"))

    GitRepo(Path("/repo/.lanes/bd-e.1")).head()

    assert fake_proc.calls[0][:3] == ("git", "-C", "/repo/.lanes/bd-e.1")


def test_at_rebinds_to_another_working_directory(fake_proc: FakeProc) -> None:
    fake_proc.expect("git", Reply(stdout="?? scratch.py\n"))
    lane = Path("/repo/.lanes/bd-e.1")

    assert repo().at(lane).is_dirty()
    assert fake_proc.calls[0][:3] == ("git", "-C", str(lane))


def test_at_the_same_directory_hands_back_the_same_repo() -> None:
    original = repo()

    assert original.at(REPO) is original


def test_commits_between_two_revisions_uses_a_range(fake_proc: FakeProc) -> None:
    fake_proc.expect("git", Reply(stdout="abc1234\ndef5678\n"))

    assert repo().commits_between("aaa", "bbb") == ["abc1234", "def5678"]
    assert "aaa..bbb" in fake_proc.calls[0]


def test_commits_are_returned_oldest_first(fake_proc: FakeProc) -> None:
    """A history reads forwards, and so should the shas recorded against a turn."""
    fake_proc.expect("git", Reply(stdout="abc1234\n"))

    repo().commits_between("aaa", "bbb")

    assert "--reverse" in fake_proc.calls[0]


def test_an_empty_repository_has_everything_reachable_as_new(fake_proc: FakeProc) -> None:
    """With no commits yet there is no `before`, so the first commit still counts."""
    fake_proc.expect("git", Reply(stdout="abc1234\n"))

    assert repo().commits_between(None, "bbb") == ["abc1234"]
    assert "bbb" in fake_proc.calls[0]
    assert not any(word.endswith("..bbb") for word in fake_proc.calls[0])


def test_no_head_after_means_no_commits(fake_proc: FakeProc) -> None:
    assert repo().commits_between("aaa", None) == []
    assert fake_proc.calls == []


def test_grep_matches_the_issue_id_literally(fake_proc: FakeProc) -> None:
    """Bead ids contain dots, which are wildcards to git's default regex."""
    fake_proc.expect("git", Reply(stdout="abc1234\n"))

    repo().commits_between("aaa", "bbb", grep="bd-e.1")

    argv = fake_proc.calls[0]
    assert "--fixed-strings" in argv
    assert "--grep=bd-e.1" in argv


def test_a_failed_log_reports_no_commits_rather_than_raising(fake_proc: FakeProc) -> None:
    """A bad range should not take a run down; it means "no evidence"."""
    fake_proc.expect("git", Reply(stderr="fatal: bad revision", returncode=128))

    assert repo().commits_between("aaa", "bbb") == []


def test_a_dirty_tree_counts_untracked_files(fake_proc: FakeProc) -> None:
    fake_proc.expect("git", Reply(stdout="?? scratch.py\n"))

    assert repo().is_dirty()


def test_a_clean_tree_is_not_dirty(fake_proc: FakeProc) -> None:
    fake_proc.expect("git", Reply(stdout="\n"))

    assert not repo().is_dirty()


def test_a_merge_into_a_branch_that_has_not_moved_fast_forwards(tmp_path: Path) -> None:
    """ADR 0024's common case: nothing to join, so nothing is verified again."""
    integration = a_repo(tmp_path)
    a_branch(integration, "worker", added="two\n")
    tip = rev(integration.path, "worker")

    result = integration.merge("worker")

    assert result.sha == tip
    assert result.fast_forwarded
    assert not result.joined
    assert not result.conflicts
    assert integration.head() == tip


def test_a_diverged_branch_makes_a_merge_commit(tmp_path: Path) -> None:
    """Two histories git had to join, which is what earns a second gate run."""
    integration = a_repo(tmp_path)
    a_branch(integration, "worker", added="two\n")
    commit(integration.path, "meanwhile", elsewhere="three\n")
    tip = rev(integration.path, "worker")

    result = integration.merge("worker", message="Merge worker")

    assert result.sha == integration.head()
    assert result.sha not in (tip, None)
    assert not result.fast_forwarded
    assert result.joined
    assert (integration.path / "added").read_text() == "two\n"
    assert (integration.path / "elsewhere").read_text() == "three\n"


def test_a_conflict_is_reported_and_leaves_nothing_behind(tmp_path: Path) -> None:
    """The integration lane has to be exactly where it was, or a person cannot land it."""
    integration = a_repo(tmp_path)
    a_branch(integration, "worker", shared="theirs\n")
    commit(integration.path, "meanwhile", shared="ours\n")
    before = integration.head()

    result = integration.merge("worker")

    assert result.conflicts == ("shared",)
    assert result.sha is None
    assert not result.fast_forwarded
    assert not result.joined
    assert integration.head() == before
    assert not integration.is_dirty()
    assert (integration.path / "shared").read_text() == "ours\n"


def test_an_already_merged_branch_merges_nothing(tmp_path: Path) -> None:
    """Nothing was merged, so there is no sha to record and no fast-forward to report."""
    integration = a_repo(tmp_path)
    a_branch(integration, "worker", added="two\n")
    integration.merge("worker")
    landed = integration.head()

    result = integration.merge("worker")

    assert result.sha is None
    assert not result.fast_forwarded
    assert not result.conflicts
    assert integration.head() == landed


def test_a_failure_that_is_not_a_conflict_raises(tmp_path: Path) -> None:
    """A branch that does not exist is a bug in the caller, not an outcome to report."""
    integration = a_repo(tmp_path)

    with pytest.raises(MilhouseError, match="could not merge worker"):
        integration.merge("worker")
