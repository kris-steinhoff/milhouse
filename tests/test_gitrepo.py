"""Tests for the small amount of git milhouse needs.

These check the argv, because that is where a wrong flag turns into a wrong
outcome: `commits_between` is what decides `partial` from `stalled`.
"""

from __future__ import annotations

from pathlib import Path

from milhouse.gitrepo import GitRepo

from .fakes import FakeProc, Reply

REPO = Path("/repo")


def repo() -> GitRepo:
    return GitRepo(REPO)


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
