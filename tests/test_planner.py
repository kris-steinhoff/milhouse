"""Tests for decomposition: the plan format, its validation, and creation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from milhouse.config import Config
from milhouse.errors import UserAbortError
from milhouse.herdr import HerdrClient
from milhouse.models import TaskDefinition
from milhouse.planner import Plan, PlanError, Planner
from milhouse.runner import AgentRunner
from milhouse.tracker import BeadsTracker

from .fakes import FakeProc, Reply
from .test_herdr import AGENT_STARTED
from .test_runner import PANE_AT_SHELL, PANE_WITH_AGENT, TURN_DONE

GOOD_PLAN = {
    "issues": [
        {
            "key": "add-command",
            "title": "Add the hello subcommand",
            "type": "task",
            "priority": 1,
            "description": "Add `hello` to cli.py.",
            "acceptance": "It prints a greeting.",
            "blocked_by": [],
        },
        {
            "key": "document",
            "title": "Document the hello subcommand",
            "blocked_by": ["add-command"],
        },
    ]
}


@pytest.fixture
def task() -> TaskDefinition:
    return TaskDefinition(
        task_id="file:docs/tasks/hello.md",
        title="Add a hello command",
        body="It should greet.",
        kind="file",
        slug="hello",
    )


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "hello"


@pytest.fixture
def planner(config: Config, run_dir: Path) -> Planner:
    runner = AgentRunner(
        HerdrClient(), config, run_dir=run_dir, pane_id="wG:p1", agent_name="milhouse-hello"
    )
    return Planner(config, BeadsTracker(config.repo_root), runner, run_dir=run_dir)


# -- the plan format -----------------------------------------------------------


def test_a_good_plan_parses() -> None:
    plan = Plan.parse(GOOD_PLAN)

    assert [issue.key for issue in plan.issues] == ["add-command", "document"]
    assert plan.issues[0].priority == 1
    assert plan.issues[1].type == "task"
    assert plan.issues[1].blocked_by == ["add-command"]


def test_defaults_fill_in_the_optional_fields() -> None:
    plan = Plan.parse({"issues": [{"key": "a", "title": "Do it"}]})

    assert plan.issues[0].type == "task"
    assert plan.issues[0].priority is None
    assert plan.issues[0].blocked_by == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "must be an object with an `issues` array"),
        ({"issues": "nope"}, "must be an object with an `issues` array"),
        ({"issues": []}, "proposes no issues"),
        ({"issues": ["nope"]}, "is not an object"),
        ({"issues": [{"key": "a"}]}, "has no title"),
        ({"issues": [{"title": "t"}]}, "has no key"),
        ({"issues": [{"key": "a", "title": "t", "type": "saga"}]}, "unknown type"),
        ({"issues": [{"key": "a", "title": "t", "priority": "high"}]}, "non-integer priority"),
        ({"issues": [{"key": "a", "title": "t", "blocked_by": "b"}]}, "malformed blocked_by"),
    ],
)
def test_a_malformed_plan_is_rejected_with_a_reason(payload: dict, message: str) -> None:
    with pytest.raises(PlanError, match=message):
        Plan.parse(payload)


def test_duplicate_keys_are_rejected() -> None:
    payload = {"issues": [{"key": "a", "title": "one"}, {"key": "a", "title": "two"}]}

    with pytest.raises(PlanError, match="duplicate issue key 'a'"):
        Plan.parse(payload)


def test_a_dangling_dependency_is_rejected() -> None:
    payload = {"issues": [{"key": "a", "title": "one", "blocked_by": ["ghost"]}]}

    with pytest.raises(PlanError, match="unknown key 'ghost'"):
        Plan.parse(payload)


def test_a_dependency_cycle_is_rejected() -> None:
    """A cycle would leave `bd ready` empty forever and the loop would claim success."""
    payload = {
        "issues": [
            {"key": "a", "title": "one", "blocked_by": ["b"]},
            {"key": "b", "title": "two", "blocked_by": ["a"]},
        ]
    }

    with pytest.raises(PlanError, match="form a cycle"):
        Plan.parse(payload)


def test_a_self_dependency_is_a_cycle() -> None:
    payload = {"issues": [{"key": "a", "title": "one", "blocked_by": ["a"]}]}

    with pytest.raises(PlanError, match="form a cycle"):
        Plan.parse(payload)


def test_a_diamond_is_not_a_cycle() -> None:
    payload = {
        "issues": [
            {"key": "a", "title": "one"},
            {"key": "b", "title": "two", "blocked_by": ["a"]},
            {"key": "c", "title": "three", "blocked_by": ["a"]},
            {"key": "d", "title": "four", "blocked_by": ["b", "c"]},
        ]
    }

    assert len(Plan.parse(payload).issues) == 4


def test_the_tree_renders_for_approval() -> None:
    rendered = Plan.parse(GOOD_PLAN).render_tree()

    assert "[task P1] Add the hello subcommand" in rendered
    assert "(after add-command)" in rendered


# -- running the planning agent ------------------------------------------------


@pytest.fixture
def planning_turn(fake_proc: FakeProc, planner: Planner) -> FakeProc:
    """A planning agent that starts, runs, and writes a valid plan."""
    fake_proc.expect(
        "herdr pane get",
        [Reply(stdout=PANE_AT_SHELL), Reply(stdout=PANE_WITH_AGENT), Reply(stdout=PANE_AT_SHELL)],
    )
    fake_proc.expect("herdr agent start", Reply(stdout=AGENT_STARTED))
    fake_proc.expect("herdr pane send-keys", Reply(stdout=""))
    fake_proc.expect("herdr agent read", Reply(stdout="wrote the plan\n"))

    def write_plan(argv: tuple[str, ...]) -> Reply:
        planner.plan_path.parent.mkdir(parents=True, exist_ok=True)
        planner.plan_path.write_text(json.dumps(GOOD_PLAN), encoding="utf-8")
        return Reply(stdout=TURN_DONE)

    fake_proc.expect("herdr agent prompt", write_plan)
    return fake_proc


def test_proposing_runs_one_turn_and_reads_the_plan(
    planner: Planner, task: TaskDefinition, planning_turn: FakeProc
) -> None:
    plan = planner.propose(task)

    assert [issue.key for issue in plan.issues] == ["add-command", "document"]
    assert len(list(planning_turn.commands("herdr", "agent", "prompt"))) == 1
    # The prompt the agent got is kept, like every other iteration's.
    assert (planner.run_dir / "iter-000.prompt").exists()


def test_the_prompt_names_the_plan_path(
    planner: Planner, task: TaskDefinition, planning_turn: FakeProc
) -> None:
    planner.propose(task)

    prompt = (planner.run_dir / "iter-000.prompt").read_text()
    assert str(planner.plan_path) in prompt


def test_a_stale_plan_is_removed_before_planning(
    planner: Planner, task: TaskDefinition, fake_proc: FakeProc
) -> None:
    """Otherwise an agent that writes nothing looks like one that succeeded."""
    planner.run_dir.mkdir(parents=True)
    planner.plan_path.write_text(json.dumps(GOOD_PLAN), encoding="utf-8")
    fake_proc.expect(
        "herdr pane get",
        [Reply(stdout=PANE_AT_SHELL), Reply(stdout=PANE_AT_SHELL)],
    )
    fake_proc.expect("herdr agent start", Reply(stdout=AGENT_STARTED))
    fake_proc.expect("herdr agent prompt", Reply(stdout=TURN_DONE))
    fake_proc.expect("herdr agent read", Reply(stdout=""))

    with pytest.raises(PlanError, match="did not write"):
        planner.propose(task)


def test_an_invalid_plan_file_is_kept_for_inspection(
    planner: Planner, task: TaskDefinition, fake_proc: FakeProc
) -> None:
    fake_proc.expect(
        "herdr pane get",
        [Reply(stdout=PANE_AT_SHELL), Reply(stdout=PANE_AT_SHELL)],
    )
    fake_proc.expect("herdr agent start", Reply(stdout=AGENT_STARTED))
    fake_proc.expect("herdr agent read", Reply(stdout=""))

    def write_garbage(argv: tuple[str, ...]) -> Reply:
        planner.plan_path.parent.mkdir(parents=True, exist_ok=True)
        planner.plan_path.write_text("{not json", encoding="utf-8")
        return Reply(stdout=TURN_DONE)

    fake_proc.expect("herdr agent prompt", write_garbage)

    with pytest.raises(PlanError, match="not valid JSON"):
        planner.propose(task)
    assert planner.plan_path.exists()


def test_a_failed_planning_turn_is_a_plan_error(
    planner: Planner, task: TaskDefinition, fake_proc: FakeProc
) -> None:
    fake_proc.expect("herdr pane get", Reply(stdout=PANE_AT_SHELL))
    fake_proc.expect(
        "herdr agent start",
        Reply(stdout=json.dumps({"error": {"code": "no_agent", "message": "not detected"}})),
    )

    with pytest.raises(PlanError, match="could not be run"):
        planner.propose(task)


# -- creating the issues -------------------------------------------------------


def test_creating_makes_the_epic_then_the_children(
    planner: Planner, task: TaskDefinition, planning_turn: FakeProc
) -> None:
    ids = iter(["bd-4rt", "bd-4rt.1", "bd-4rt.2"])
    planning_turn.expect(
        "bd",
        lambda argv: Reply(
            stdout=json.dumps({"id": next(ids), "title": "t", "status": "open"})
            if "create" in argv
            else ""
        ),
    )

    epic, children = planner.plan(task, confirm=None)

    assert epic.id == "bd-4rt"
    assert [issue.id for issue in children] == ["bd-4rt.1", "bd-4rt.2"]
    epic_call = next(planning_turn.commands("bd"))
    assert epic_call[epic_call.index("--type") + 1] == "epic"
    # The dependency is wired after both children exist.
    assert planning_turn.ran("bd", "-C", str(planner.config.repo_root), "dep", "add")


def test_declining_the_plan_creates_nothing(
    planner: Planner, task: TaskDefinition, planning_turn: FakeProc
) -> None:
    with pytest.raises(UserAbortError, match="nothing was created"):
        planner.plan(task, confirm=lambda plan: False)

    assert not planning_turn.ran("bd")
