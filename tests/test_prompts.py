"""Tests for prompt rendering.

These assert on the *contract* the templates impose, not on their wording, so
tuning the prose does not break the suite but dropping a promise does.
"""

from __future__ import annotations

import pytest
from jinja2 import UndefinedError

from milhouse import prompts
from milhouse.models import Issue, TaskDefinition


@pytest.fixture
def task() -> TaskDefinition:
    return TaskDefinition(
        task_id="file:docs/tasks/hello.md",
        title="Add a hello command",
        body="# Add a hello command\n\nIt should greet.",
        kind="file",
        slug="hello",
    )


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


def test_the_plan_prompt_forbids_creating_issues(task: TaskDefinition) -> None:
    """The approval guardrail only works if the agent does not create them."""
    rendered = prompts.render_plan(task, plan_path="/repo/.milhouse/runs/hello/plan.json")

    assert "Do not run `bd`" in rendered
    assert "/repo/.milhouse/runs/hello/plan.json" in rendered
    assert "Do not implement anything" in rendered


def test_the_plan_prompt_includes_the_task_and_the_format(task: TaskDefinition) -> None:
    rendered = prompts.render_plan(task, plan_path="/p.json", max_issues=7)

    assert task.title in rendered
    assert "It should greet." in rendered
    assert '"blocked_by"' in rendered
    assert "7 issues or fewer" in rendered


def test_the_plan_prompt_shows_a_source_url_when_there_is_one() -> None:
    task = TaskDefinition(
        task_id="gh:o/r#1",
        title="t",
        body="b",
        kind="github",
        slug="gh-1",
        url="https://github.com/o/r/issues/1",
    )

    assert "https://github.com/o/r/issues/1" in prompts.render_plan(task, plan_path="/p.json")


def test_the_iterate_prompt_states_the_whole_done_contract(
    task: TaskDefinition, issue: Issue
) -> None:
    rendered = prompts.render_iterate(task, issue, branch="milhouse/hello")

    assert issue.id in rendered
    assert issue.title in rendered
    assert "Add `hello` to cli.py." in rendered
    # Verify, document, commit, close — all four, or the loop's signals lie.
    assert "the tests pass and the linter is clean" in rendered
    assert "same commit" in rendered
    assert f"bd close {issue.id}" in rendered
    assert "milhouse/hello" in rendered


def test_the_iterate_prompt_forbids_closing_an_unfinished_issue(
    task: TaskDefinition, issue: Issue
) -> None:
    """The one failure milhouse cannot detect, so the prompt has to bind it."""
    rendered = prompts.render_iterate(task, issue)

    assert "Leave the issue open" in rendered
    assert f"bd note {issue.id}" in rendered


def test_the_iterate_prompt_scopes_the_agent_to_one_issue(
    task: TaskDefinition, issue: Issue
) -> None:
    rendered = prompts.render_iterate(task, issue)

    assert f"Work only {issue.id}" in rendered
    assert "Do not start on the next issue" in rendered


def test_acceptance_criteria_and_notes_are_carried_through(
    task: TaskDefinition, issue: Issue
) -> None:
    """Notes are the only memory a fresh context window gets."""
    rendered = prompts.render_iterate(task, issue)

    assert "`milhouse hello` prints a greeting." in rendered
    assert "typer wants a decorator" in rendered


def test_a_first_attempt_says_nothing_about_attempts(task: TaskDefinition, issue: Issue) -> None:
    rendered = prompts.render_iterate(task, issue, attempt=1)

    assert "attempt 1" not in rendered.lower()


def test_a_retry_tells_the_agent_where_it_stands(task: TaskDefinition, issue: Issue) -> None:
    rendered = prompts.render_iterate(
        task,
        issue,
        attempt=3,
        previous=[{"outcome": "stalled", "detail": "nothing committed"}],
    )

    assert "attempt 3" in rendered
    assert "a human looked at the run in between" in rendered
    assert "stalled (nothing committed)" in rendered
    assert "try a different approach" in rendered


def test_the_branch_section_is_omitted_when_there_is_no_branch(
    task: TaskDefinition, issue: Issue
) -> None:
    rendered = prompts.render_iterate(task, issue, branch=None)

    assert "You are on branch" not in rendered


def test_the_task_body_is_marked_as_background(task: TaskDefinition, issue: Issue) -> None:
    """Included so the agent knows what the issue is for, not as a second assignment."""
    rendered = prompts.render_iterate(task, issue)

    assert "This is context, not your assignment." in rendered
    assert "It should greet." in rendered


def test_an_issue_with_no_notes_renders_cleanly(task: TaskDefinition) -> None:
    bare = Issue(id="bd-1", title="Do it", status="open")

    rendered = prompts.render_iterate(task, bare)

    assert "Notes from earlier attempts" not in rendered
    assert "Acceptance criteria" not in rendered


def test_a_missing_variable_fails_loudly(task: TaskDefinition) -> None:
    """StrictUndefined: a typo must not silently send an agent a prompt with a hole."""
    with pytest.raises(UndefinedError):
        prompts.render("iterate.md.j2", task=task)
