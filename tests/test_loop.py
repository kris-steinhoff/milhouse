"""Tests for the ralph loop and its guardrails.

These fake the tracker and the runner rather than `proc`, because what is under
test here is the loop's decisions — what it claims, when it stops, what it does
with a failure — not the argv anyone builds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from milhouse.config import Config
from milhouse.errors import LoopAbortedError, MilhouseError, UserAbortError
from milhouse.herdr import AgentStatus, Workspace
from milhouse.loop import RalphLoop
from milhouse.models import Issue, RunState, TaskDefinition
from milhouse.runner import TurnResult


@dataclass
class FakeTracker:
    """An in-memory tracker holding one epic and its children."""

    epic: Issue | None = None
    issues: list[Issue] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    def find_epic(self, task: TaskDefinition) -> Issue | None:
        return self.epic

    def create_epic(self, task: TaskDefinition) -> Issue:
        self.epic = Issue(id="bd-e", title=task.title, status="open", issue_type="epic")
        return self.epic

    def create_children(self, epic_id: str, issues: list) -> list[Issue]:
        self.issues = [
            Issue(id=f"bd-e.{n}", title=planned.title, status="open", parent=epic_id)
            for n, planned in enumerate(issues, start=1)
        ]
        return self.issues

    def ready(self, epic_id: str, *, claim: bool) -> Issue | None:
        for issue in self.issues:
            if issue.status == "open":
                if claim:
                    issue.status = "in_progress"
                return issue
        return None

    def get(self, issue_id: str) -> Issue:
        for issue in self.issues:
            if issue.id == issue_id:
                return issue
        raise MilhouseError(f"no such issue: {issue_id}")

    def children(self, epic_id: str) -> list[Issue]:
        return list(self.issues)

    def release(self, issue_id: str, *, note: str | None = None) -> None:
        self.released.append(issue_id)
        self.get(issue_id).status = "open"
        if note:
            self.notes.append((issue_id, note))

    def block(self, issue_id: str, note: str) -> None:
        self.blocked.append(issue_id)
        self.get(issue_id).status = "blocked"
        self.notes.append((issue_id, note))

    def note(self, issue_id: str, text: str) -> None:
        self.notes.append((issue_id, text))


@dataclass
class FakeClient:
    """A herdr client that hands out one workspace and never fails."""

    workspaces: set[str] = field(default_factory=set)
    focused: bool = False

    def workspace_exists(self, workspace_id: str) -> bool:
        return workspace_id in self.workspaces

    def create_workspace(self, cwd: Path, label: str, *, focus: bool = False) -> Workspace:
        self.focused = focus
        self.workspaces.add("wG")
        return Workspace(workspace_id="wG", pane_id="wG:p1", label=label)

    def first_pane(self, workspace_id: str) -> str:
        return f"{workspace_id}:p1"


@dataclass
class FakeRepo:
    """A git repository whose HEAD only moves when a test says so."""

    branch: str | None = "main"
    dirty: bool = False
    commits: int = 0

    def head(self) -> str | None:
        return f"sha{self.commits}"

    def current_branch(self) -> str | None:
        return self.branch

    def ensure_branch(self, name: str) -> str:
        self.branch = name
        return name

    def is_dirty(self) -> bool:
        return self.dirty


@dataclass
class FakeRunner:
    """A scripted agent: each turn closes an issue, commits, blocks, or stalls."""

    tracker: FakeTracker
    repo: FakeRepo
    script: list[str] = field(default_factory=list)
    pane_id: str = "wG:p1"
    agent_name: str = "milhouse-hello"
    turns: list[str] = field(default_factory=list)
    unblock_to: AgentStatus = "done"

    def run_turn(self, prompt: str, *, iteration: int) -> TurnResult:
        self.turns.append(prompt)
        action = self.script.pop(0) if self.script else "stall"
        if action == "close":
            self.repo.commits += 1
            for issue in self.tracker.issues:
                if issue.status == "in_progress":
                    issue.status = "closed"
                    break
            return TurnResult(agent_state="done")
        if action == "commit":
            self.repo.commits += 1
            return TurnResult(agent_state="done")
        if action == "block":
            return TurnResult(agent_state="blocked")
        if action == "timeout":
            return TurnResult(agent_state="working", timed_out=True)
        if action == "error":
            return TurnResult(agent_state="unknown", error="herdr fell over")
        return TurnResult(agent_state="done")

    def wait_for_unblock(self) -> AgentStatus:
        return self.unblock_to

    def exit_agent(self) -> None:
        return None


@pytest.fixture
def task() -> TaskDefinition:
    return TaskDefinition(
        task_id="file:docs/tasks/hello.md",
        title="Add a hello command",
        body="It should greet.",
        kind="file",
        slug="hello",
    )


def build(
    config: Config,
    task: TaskDefinition,
    *,
    tracker: FakeTracker,
    script: list[str],
    repo: FakeRepo | None = None,
) -> tuple[RalphLoop, FakeRunner]:
    """Wire a loop with fakes and a scripted runner already installed."""
    repo = repo or FakeRepo()
    loop = RalphLoop(
        config,
        task,
        tracker=tracker,  # type: ignore[arg-type]
        client=FakeClient(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
    )
    runner = FakeRunner(tracker=tracker, repo=repo, script=script)
    original = loop._open_workspace

    def open_workspace(state: RunState) -> None:
        original(state)
        loop._session.runner = runner  # type: ignore[assignment]

    loop._open_workspace = open_workspace  # type: ignore[method-assign]
    return loop, runner


@pytest.fixture
def decomposed() -> FakeTracker:
    """A tracker with an epic and two open issues."""
    tracker = FakeTracker(epic=Issue(id="bd-e", title="Add a hello command", status="open"))
    tracker.issues = [
        Issue(id="bd-e.1", title="Add the subcommand", status="open", parent="bd-e"),
        Issue(id="bd-e.2", title="Document it", status="open", parent="bd-e"),
    ]
    return tracker


def test_a_run_works_every_issue_and_reports_completion(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    loop, _ = build(config, task, tracker=decomposed, script=["close", "close"])

    result = loop.run()

    assert result.completed
    assert result.iterations == 2
    assert [item.outcome for item in result.state.iterations] == ["success", "success"]
    assert all(issue.is_closed for issue in decomposed.issues)


def test_the_iteration_ceiling_stops_the_run(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    config.loop.max_iterations = 1
    loop, _ = build(config, task, tracker=decomposed, script=["close", "close"])

    result = loop.run()

    assert not result.completed
    assert result.iterations == 1
    assert "1-iteration ceiling" in result.reason


def test_a_stalled_issue_is_retried_then_blocked(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Three failures on one issue mark it blocked; the loop moves on."""
    config.loop.max_attempts = 3
    decomposed.issues = decomposed.issues[:1]
    loop, _ = build(config, task, tracker=decomposed, script=["stall", "stall", "stall"])

    result = loop.run()

    assert [item.outcome for item in result.state.iterations] == ["stalled"] * 3
    assert decomposed.blocked == ["bd-e.1"]
    # Nothing is ready, but the issue is blocked rather than done, so the run
    # did not complete. Reporting completion here would exit 0 on a failed run.
    assert not result.completed
    assert "bd-e.1" in result.reason
    assert result.state.attempts_for("bd-e.1") == 3


def test_an_epic_whose_issues_all_blocked_is_not_reported_as_finished(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """An empty ready queue means "finished" or "stuck", and they are opposites.

    A dogfood run hit a permission prompt on every issue, blocked them all, and
    then reported "the epic is finished" and exited 0 with nothing done.
    """
    for issue in decomposed.issues:
        issue.status = "blocked"
    loop, _ = build(config, task, tracker=decomposed, script=[])

    result = loop.run()

    assert not result.completed
    assert result.iterations == 0
    assert "unfinished" in result.reason
    assert "blocked" in result.reason
    for issue in decomposed.issues:
        assert issue.id in result.reason


def test_an_epic_with_every_issue_closed_is_finished(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    for issue in decomposed.issues:
        issue.status = "closed"
    loop, _ = build(config, task, tracker=decomposed, script=[])

    result = loop.run()

    assert result.completed
    assert result.reason == "no issues are ready; the epic is finished"


def test_a_commit_without_a_close_is_partial_and_retried(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    decomposed.issues = decomposed.issues[:1]
    loop, _ = build(config, task, tracker=decomposed, script=["commit", "close"])

    result = loop.run()

    assert [item.outcome for item in result.state.iterations] == ["partial", "success"]
    assert decomposed.blocked == []
    # Claiming set the issue in_progress, which `bd ready` excludes. Without an
    # explicit re-open there would be no second attempt at all.
    assert decomposed.released == ["bd-e.1"]


def test_a_blocked_agent_does_not_burn_an_attempt(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """`skip` blocks the issue in beads and moves on to the next one."""
    config.loop.on_blocked = "skip"
    loop, _ = build(config, task, tracker=decomposed, script=["block", "close"])

    result = loop.run()

    assert [item.outcome for item in result.state.iterations] == ["blocked", "success"]
    assert result.state.attempts_for("bd-e.1") == 0
    assert decomposed.blocked == ["bd-e.1"]
    assert decomposed.issues[1].is_closed


def test_on_blocked_wait_waits_for_a_human(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    config.loop.on_blocked = "wait"
    decomposed.issues = decomposed.issues[:1]
    loop, runner = build(config, task, tracker=decomposed, script=["block"])
    runner.unblock_to = "done"

    result = loop.run()

    # The human unblocked it but the issue is still open, so it reads as stalled.
    assert result.state.iterations[0].outcome == "stalled"


def test_on_blocked_abort_stops_the_run(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    config.loop.on_blocked = "abort"
    loop, _ = build(config, task, tracker=decomposed, script=["block"])

    with pytest.raises(LoopAbortedError, match="waiting on a human"):
        loop.run()


def test_a_timeout_is_recorded_and_retried(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    decomposed.issues = decomposed.issues[:1]
    loop, _ = build(config, task, tracker=decomposed, script=["timeout", "close"])

    result = loop.run()

    assert [item.outcome for item in result.state.iterations] == ["timeout", "success"]


def test_a_herdr_failure_is_recorded_as_an_error(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    decomposed.issues = decomposed.issues[:1]
    loop, _ = build(config, task, tracker=decomposed, script=["error", "close"])

    result = loop.run()

    assert result.state.iterations[0].outcome == "error"
    assert "herdr fell over" in result.state.iterations[0].detail


# -- state, resume, and reconciliation ----------------------------------------


def test_state_is_persisted_as_the_run_goes(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    loop, _ = build(config, task, tracker=decomposed, script=["close", "close"])

    loop.run()

    saved = RunState.load(loop.state_path)
    assert saved is not None
    assert saved.epic_id == "bd-e"
    assert saved.workspace_id == "wG"
    assert saved.branch == "milhouse/hello"
    assert saved.claimed_issue is None
    assert len(saved.iterations) == 2


def test_a_stale_claim_is_reopened_on_the_next_run(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """SIGKILL leaves a claim behind; re-running is the recovery mechanism."""
    loop, _ = build(config, task, tracker=decomposed, script=["close", "close"])
    decomposed.issues[0].status = "in_progress"
    state = RunState(task_id=task.task_id, task_slug=task.slug, claimed_issue="bd-e.1")
    state.save(loop.state_path)

    result = loop.run()

    assert decomposed.released == ["bd-e.1"]
    assert result.completed


def test_attempt_counts_survive_a_resume(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """A resumed run does not grant three fresh attempts on a failing issue."""
    config.loop.max_attempts = 3
    decomposed.issues = decomposed.issues[:1]
    loop, _ = build(config, task, tracker=decomposed, script=["stall"])
    state = RunState(task_id=task.task_id, task_slug=task.slug, attempts={"bd-e.1": 2})
    state.save(loop.state_path)

    loop.run()

    assert decomposed.blocked == ["bd-e.1"]


def test_a_state_file_from_a_different_task_is_refused(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Two task definitions sharing a filename would otherwise share a run dir."""
    loop, _ = build(config, task, tracker=decomposed, script=[])
    RunState(task_id="file:elsewhere/hello.md", task_slug="hello").save(loop.state_path)

    with pytest.raises(MilhouseError, match="share a slug"):
        loop.run()


# -- branching -----------------------------------------------------------------


def test_the_run_gets_its_own_branch(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    repo = FakeRepo()
    loop, _ = build(config, task, tracker=decomposed, script=["close", "close"], repo=repo)

    loop.run()

    assert repo.branch == "milhouse/hello"


def test_a_dirty_working_tree_stops_the_run_before_anything_starts(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    """Losing someone's uncommitted work to an unattended loop is the worst failure."""
    repo = FakeRepo(dirty=True)
    loop, runner = build(config, task, tracker=decomposed, script=["close"], repo=repo)

    with pytest.raises(MilhouseError, match="uncommitted changes"):
        loop.run()

    assert runner.turns == []


def test_the_current_branch_strategy_leaves_the_repo_alone(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    config.git.branch_strategy = "current"
    repo = FakeRepo(branch="some-worktree-branch", dirty=True)
    loop, _ = build(config, task, tracker=decomposed, script=["close", "close"], repo=repo)

    result = loop.run()

    assert repo.branch == "some-worktree-branch"
    assert result.state.branch == "some-worktree-branch"


# -- the prompt the loop builds ------------------------------------------------


def test_the_prompt_carries_the_issue_the_branch_and_the_attempt(
    config: Config, task: TaskDefinition, decomposed: FakeTracker
) -> None:
    decomposed.issues = decomposed.issues[:1]
    loop, runner = build(config, task, tracker=decomposed, script=["stall", "close"])

    loop.run()

    first, second = runner.turns
    assert "bd-e.1" in first
    assert "milhouse/hello" in first
    assert "attempt 2" in second
    assert "stalled" in second


def test_decomposition_runs_when_there_is_no_epic(
    config: Config, task: TaskDefinition, tmp_path: Path
) -> None:
    tracker = FakeTracker()
    loop, runner = build(config, task, tracker=tracker, script=["plan"])

    def propose_then_close(prompt: str, *, iteration: int) -> TurnResult:
        runner.turns.append(prompt)
        if iteration == 0:
            loop.run_dir.mkdir(parents=True, exist_ok=True)
            (loop.run_dir / "plan.json").write_text(
                '{"issues": [{"key": "a", "title": "Add it"}]}', encoding="utf-8"
            )
            return TurnResult(agent_state="done")
        for issue in tracker.issues:
            if issue.status == "in_progress":
                issue.status = "closed"
        return TurnResult(agent_state="done")

    runner.run_turn = propose_then_close  # type: ignore[method-assign]

    result = loop.run()

    assert tracker.epic is not None
    assert [issue.title for issue in tracker.issues] == ["Add it"]
    assert result.completed
    assert "Do not run `bd`" in runner.turns[0]


def test_declining_the_decomposition_creates_nothing(config: Config, task: TaskDefinition) -> None:
    tracker = FakeTracker()
    loop, runner = build(config, task, tracker=tracker, script=[])

    def propose(prompt: str, *, iteration: int) -> TurnResult:
        loop.run_dir.mkdir(parents=True, exist_ok=True)
        (loop.run_dir / "plan.json").write_text(
            '{"issues": [{"key": "a", "title": "Add it"}]}', encoding="utf-8"
        )
        return TurnResult(agent_state="done")

    runner.run_turn = propose  # type: ignore[method-assign]

    with pytest.raises(UserAbortError):
        loop.run(confirm=lambda plan: False)

    assert tracker.epic is None
