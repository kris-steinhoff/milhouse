"""In-memory stand-ins for the tracker, herdr, git, and the agent.

These fake at the collaborator boundary rather than at :mod:`milhouse.proc`,
because what the session and step tests are about is decisions — what gets
claimed, what happens after a turn, when a run stops — not the argv anyone
builds. The argv is covered where it is written, in ``test_tracker.py`` and
``test_herdr.py``.

:class:`FakeRunner` is the interesting one: it is a scripted agent, so a test
says ``["close", "stall"]`` and gets a turn that finishes an issue followed by
one that does nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from milhouse.audit import AuditLog
from milhouse.config import Config
from milhouse.errors import MilhouseError
from milhouse.herdr import Workspace
from milhouse.models import Issue
from milhouse.runner import TurnResult
from milhouse.session import Session

__all__ = ["FakeAudit", "FakeClient", "FakeRepo", "FakeRunner", "FakeTracker", "build"]


class FakeAudit(AuditLog):
    """A real audit log that appends to the file instead of shelling out to bd.

    Only the write is replaced. Everything a test then asserts on — the entry
    shape, the parsing, the unsettled-claim rule — is the production code
    reading back what production code wrote.
    """

    def _record(self, kind: str, issue_id: str, extra: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "issue_id": issue_id, "extra": extra}) + "\n")


@dataclass
class FakeTracker:
    """An in-memory tracker holding an epic and the issues under it."""

    epic: Issue | None = None
    issues: list[Issue] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)

    def ready(self, *, claim: bool) -> Issue | None:
        for issue in self.issues:
            if issue.status == "open":
                if claim:
                    issue.status = "in_progress"
                return issue
        return None

    def get(self, issue_id: str) -> Issue:
        if self.epic is not None and self.epic.id == issue_id:
            return self.epic
        for issue in self.issues:
            if issue.id == issue_id:
                return issue
        raise MilhouseError(f"no such issue: {issue_id}")

    def children(self, parent_id: str | None = None) -> list[Issue]:
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

    workspaces: dict[str, str] = field(default_factory=dict)
    """Open workspaces, as ``{workspace_id: label}``."""

    focused: bool = False
    avoided: str | None = None

    def workspace_exists(self, workspace_id: str) -> bool:
        return workspace_id in self.workspaces

    def find_workspace(self, label: str) -> str | None:
        for workspace_id, existing in self.workspaces.items():
            if existing == label:
                return workspace_id
        return None

    def create_workspace(self, cwd: Path, label: str, *, focus: bool = False) -> Workspace:
        self.focused = focus
        self.workspaces["wG"] = label
        return Workspace(workspace_id="wG", pane_id="wG:p1", label=label)

    def first_pane(self, workspace_id: str) -> str:
        return f"{workspace_id}:p1"

    def pane_to_work_in(self, workspace_id: str, cwd: Path, *, avoid: str | None = None) -> str:
        """Hand out ``:p1``, or ``:p2`` when ``:p1`` is the caller's own pane."""
        self.avoided = avoid
        if avoid == f"{workspace_id}:p1":
            return f"{workspace_id}:p2"
        return f"{workspace_id}:p1"


@dataclass
class FakeRepo:
    """A git repository whose HEAD only moves when a turn says so.

    ``messages`` holds one line per commit, so a test can decide whether a
    commit names the issue it was supposed to be for.
    """

    branch: str | None = "main"
    dirty: bool = False
    commits: int = 0
    messages: list[str] = field(default_factory=list)
    scoped_to: list[Path] = field(default_factory=list)
    """Every path this repo was asked to scope itself to, in call order."""

    def at(self, path: Path) -> FakeRepo:
        """Record the scoping and keep answering, since there is one fake tree."""
        self.scoped_to.append(path)
        return self

    def head(self) -> str | None:
        return f"sha{self.commits}"

    def current_branch(self) -> str | None:
        return self.branch

    def ensure_branch(self, name: str) -> str:
        self.branch = name
        return name

    def commits_between(
        self, before: str | None, after: str | None, *, grep: str = ""
    ) -> list[str]:
        first = int(before.removeprefix("sha")) if before else 0
        last = int(after.removeprefix("sha")) if after else 0
        landed = range(first + 1, last + 1)
        if not grep:
            return [f"sha{n}" for n in landed]
        return [f"sha{n}" for n in landed if grep in self._message(n)]

    def _message(self, number: int) -> str:
        index = number - 1
        return self.messages[index] if index < len(self.messages) else ""

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
    workdir: Path = Path("/repo")
    turns: list[str] = field(default_factory=list)
    issue_ids: list[str | None] = field(default_factory=list)

    def run_turn(self, prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        self.turns.append(prompt)
        self.issue_ids.append(issue_id)
        action = self.script.pop(0) if self.script else "stall"
        if action == "close":
            self._commit()
            for issue in self.tracker.issues:
                if issue.status == "in_progress":
                    issue.status = "closed"
                    break
            return TurnResult(agent_state="done")
        if action == "commit":
            self._commit()
            return TurnResult(agent_state="done")
        if action == "commit-unrelated":
            self._commit(message="chore: something else entirely")
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

    def _commit(self, message: str | None = None) -> None:
        """Move HEAD, recording a message naming the claimed issue by default."""
        claimed = next(
            (issue.id for issue in self.tracker.issues if issue.status == "in_progress"), ""
        )
        self.repo.commits += 1
        self.repo.messages.append(message or f"feat: do the thing ({claimed})")


def build(
    config: Config,
    *,
    tracker: FakeTracker,
    script: list[str],
    repo: FakeRepo | None = None,
    client: FakeClient | None = None,
) -> tuple[Session, FakeRunner]:
    """Wire a session with fakes and a scripted runner already installed."""
    repo = repo or FakeRepo()
    runner = FakeRunner(tracker=tracker, repo=repo, script=script, workdir=config.repo_root)
    session = Session(
        config,
        tracker=tracker,
        client=client or FakeClient(),  # ty: ignore[invalid-argument-type]
        repo=repo,  # ty: ignore[invalid-argument-type]
        audit=FakeAudit(config.repo_root),
        runner=runner,
    )
    return session, runner
