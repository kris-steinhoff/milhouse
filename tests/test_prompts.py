"""Tests for prompt rendering.

These assert on the *contract* the template imposes, not on its wording, so
tuning the prose does not break the suite but dropping a promise does.
"""

from __future__ import annotations

import pytest
from jinja2 import UndefinedError

from milhouse import prompts
from milhouse.models import Issue

BACKGROUND = "# Add a hello command\n\nIt should greet."


@pytest.fixture
def issue() -> Issue:
    return Issue(
        id="bd-4rt.1",
        title="Add the subcommand",
        status="in_progress",
        description="Add `hello` to cli.py.",
        parent="bd-4rt",
        raw={
            "parent": "bd-4rt",
            "acceptance_criteria": "`milhouse hello` prints a greeting.",
            "notes": "Tried wiring it into main(); typer wants a decorator.",
        },
    )


def test_the_iterate_prompt_states_the_whole_done_contract(issue: Issue) -> None:
    rendered = prompts.render_iterate(issue, branch="milhouse/hello")

    assert issue.id in rendered
    assert issue.title in rendered
    assert "Add `hello` to cli.py." in rendered
    # Verify, document, commit, close — all four, or the loop's signals lie.
    assert "the tests pass and the linter is clean" in rendered
    assert "same commit" in rendered
    assert f"bd close {issue.id}" in rendered
    assert "milhouse/hello" in rendered


def test_the_iterate_prompt_makes_preparing_the_lane_part_of_the_turn(
    issue: Issue,
) -> None:
    """A lane is a fresh worktree, and only the agent knows how to build one.

    milhouse re-runs the gate in that same tree after the agent exits and cannot
    tell a worktree nobody set up from code that is broken, so an unprepared lane
    reads as a turn that failed. The prompt is where that is prevented, because
    what "built" means varies per project (ADR 0013).
    """
    rendered = prompts.render_iterate(issue, branch="milhouse/hello")

    assert "fresh worktree" in rendered
    assert "Preparing it is part of this" in rendered
    # And it has to land before the agent judges the tests, not after.
    assert rendered.index("fresh worktree") < rendered.index(
        "the tests pass and the linter is clean"
    )


def test_the_iterate_prompt_forbids_closing_an_unfinished_issue(issue: Issue) -> None:
    """The one failure milhouse cannot detect, so the prompt has to bind it."""
    rendered = prompts.render_iterate(issue)

    assert "Leave the issue open" in rendered
    assert f"bd note {issue.id}" in rendered


def test_the_iterate_prompt_scopes_the_agent_to_one_issue(issue: Issue) -> None:
    rendered = prompts.render_iterate(issue)

    assert f"Work only {issue.id}" in rendered
    assert "Do not start on the next issue" in rendered


def test_acceptance_criteria_and_notes_are_carried_through(issue: Issue) -> None:
    """Notes are the only memory a fresh context window gets."""
    rendered = prompts.render_iterate(issue)

    assert "`milhouse hello` prints a greeting." in rendered
    assert "typer wants a decorator" in rendered


def test_a_first_attempt_says_nothing_about_attempts(issue: Issue) -> None:
    rendered = prompts.render_iterate(issue, attempt=1)

    assert "attempt 1" not in rendered.lower()


def test_a_retry_tells_the_agent_where_it_stands(issue: Issue) -> None:
    rendered = prompts.render_iterate(
        issue,
        attempt=3,
        previous=[{"outcome": "stalled", "detail": "nothing committed"}],
    )

    assert "attempt 3" in rendered
    assert "a human looked at the run in between" in rendered
    assert "stalled (nothing committed)" in rendered
    assert "try a different approach" in rendered


def test_the_branch_section_is_omitted_when_there_is_no_branch(issue: Issue) -> None:
    rendered = prompts.render_iterate(issue, branch=None)

    assert "You are on branch" not in rendered


def test_the_parents_description_is_marked_as_background(issue: Issue) -> None:
    """Included so the agent knows what the issue is for, not as a second assignment."""
    rendered = prompts.render_iterate(issue, background=BACKGROUND)

    assert "This is context, not your assignment." in rendered
    assert "It should greet." in rendered


def test_no_background_means_no_background_section(issue: Issue) -> None:
    """An issue with no parent gets a prompt about the issue and nothing else."""
    rendered = prompts.render_iterate(issue, background="")

    assert "Background" not in rendered
    assert "This is context, not your assignment." not in rendered


def test_an_issue_with_no_notes_renders_cleanly() -> None:
    bare = Issue(id="bd-1", title="Do it", status="open")

    rendered = prompts.render_iterate(bare)

    assert "Notes from earlier attempts" not in rendered
    assert "Acceptance criteria" not in rendered


def test_a_missing_variable_fails_loudly(issue: Issue) -> None:
    """StrictUndefined: a typo must not silently send an agent a prompt with a hole."""
    with pytest.raises(UndefinedError):
        prompts.render("iterate.md.j2", issue=issue)
