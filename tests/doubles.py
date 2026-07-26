"""In-memory stand-ins for the tracker, herdr, git, and the agent.

These fake at the collaborator boundary rather than at :mod:`milhouse.proc`,
because what the session, step, and loop tests are about is decisions — what gets
claimed, what happens after a turn, when a run stops — not the argv anyone
builds. The argv is covered where it is written, in ``test_tracker.py`` and
``test_herdr.py``.

:class:`FakeRunner` is the interesting one: it is a scripted agent, so a test
says ``["close", "stall"]`` and gets a turn that finishes an issue followed by
one that does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from milhouse.config import Config
from milhouse.errors import MilhouseError
from milhouse.herdr import Workspace
from milhouse.models import Issue, TaskDefinition
from milhouse.runner import TurnResult
from milhouse.session import Session

__all__ = ["FakeClient", "FakeRepo", "FakeRunner", "FakeTracker", "build"]


@dataclass
class FakeTracker:
    """An in-memory tracker holding one epic and its children."""

    epic: Issue | None = None
    issues: list[Issue] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

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
    """A git repository whose HEAD only moves when a turn says so."""

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

    def exit_agent(self) -> None:
        return None


def build(
    config: Config,
    task: TaskDefinition,
    *,
    tracker: FakeTracker,
    script: list[str],
    repo: FakeRepo | None = None,
) -> tuple[Session, FakeRunner]:
    """Wire a session with fakes and a scripted runner already installed."""
    repo = repo or FakeRepo()
    runner = FakeRunner(tracker=tracker, repo=repo, script=script)
    session = Session(
        config,
        task,
        tracker=tracker,
        client=FakeClient(),  # ty: ignore[invalid-argument-type]
        repo=repo,  # ty: ignore[invalid-argument-type]
        runner=runner,
    )
    return session, runner
