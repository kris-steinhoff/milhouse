"""Tests for the shared data types."""

from __future__ import annotations

import pytest

from milhouse.models import Issue, Iteration, slugify


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello", "hello"),
        ("Add a Hello Command", "add-a-hello-command"),
        ("docs/tasks/hello.md", "docs-tasks-hello-md"),
        ("  --weird__name--  ", "weird-name"),
        ("!!!", "task"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_issue_reports_closed_status() -> None:
    assert Issue(id="bd-1", title="t", status="closed").is_closed
    assert not Issue(id="bd-1", title="t", status="in_progress").is_closed


def test_iteration_detects_a_commit() -> None:
    committed = Iteration(number=1, issue_id="bd-1", outcome="partial", commits=["abc1234"])
    still = Iteration(number=1, issue_id="bd-1", outcome="stalled", head_before="a", head_after="a")

    assert committed.made_commit
    assert not still.made_commit
