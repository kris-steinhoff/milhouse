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
from milhouse.herdr import AgentStatus, Workspace, Worktree
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
    deferred: list[tuple[str, str]] = field(default_factory=list)
    """Issues set aside, as ``(issue_id, reason)``."""

    def ready(self, *, claim: bool) -> Issue | None:
        for issue in self.issues:
            if issue.status == "open" and issue.id not in self._deferred_ids:
                if claim:
                    issue.status = "in_progress"
                return issue
        return None

    @property
    def _deferred_ids(self) -> set[str]:
        """Deferred issues stay open and stay listed, but stop being offered."""
        return {issue_id for issue_id, _ in self.deferred}

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

    def defer(self, issue_id: str, *, reason: str) -> None:
        self.deferred.append((issue_id, reason))

    def note(self, issue_id: str, text: str) -> None:
        self.notes.append((issue_id, text))


@dataclass
class FakeClient:
    """A herdr client holding workspaces, worktrees, and tabs in memory.

    It models the shape that matters to lane assignment: a worktree is opened in
    a workspace of its own, the workspace label carries the issue id, and a
    stacked issue is a labelled tab inside somebody else's lane.
    """

    workspaces: dict[str, str] = field(default_factory=dict)
    """Open workspaces, as ``{workspace_id: label}``."""

    checkouts: list[Worktree] = field(default_factory=list)
    """The worktree registry, primary checkout included."""

    tab_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    """Tabs per workspace, as ``{workspace_id: {tab_id: label}}``."""

    focused: bool = False
    avoided: str | None = None
    _next: int = 0

    # -- workspaces -------------------------------------------------------

    def workspace_exists(self, workspace_id: str) -> bool:
        return workspace_id in self.workspaces

    def workspace_labels(self) -> dict[str, str]:
        return dict(self.workspaces)

    def find_workspace(self, label: str) -> str | None:
        for workspace_id, existing in self.workspaces.items():
            if existing == label:
                return workspace_id
        return None

    def create_workspace(self, cwd: Path, label: str, *, focus: bool = False) -> Workspace:
        self.focused = focus
        self.workspaces["wG"] = label
        self.checkouts.append(Worktree(path=cwd, branch="main", workspace_id="wG"))
        return Workspace(workspace_id="wG", pane_id="wG:p1", label=label)

    def first_pane(self, workspace_id: str) -> str:
        return f"{workspace_id}:p1"

    def pane_to_work_in(
        self,
        workspace_id: str,
        cwd: Path,
        *,
        avoid: str | None = None,
        tab_id: str | None = None,
    ) -> str:
        """Hand out ``:p1``, or ``:p2`` when ``:p1`` is the caller's own pane."""
        self.avoided = avoid
        if tab_id is not None:
            return f"{tab_id}:p1"
        if avoid == f"{workspace_id}:p1":
            return f"{workspace_id}:p2"
        return f"{workspace_id}:p1"

    # -- worktrees and tabs -----------------------------------------------

    def worktrees(self, repo_root: Path) -> list[Worktree]:
        return list(self.checkouts)

    def tabs(self, workspace_id: str) -> list[dict[str, str]]:
        return [
            {"tab_id": tab_id, "label": label}
            for tab_id, label in self.tab_labels.get(workspace_id, {}).items()
        ]

    def create_worktree(
        self,
        *,
        source_workspace: str,
        branch: str,
        base: str,
        label: str,
        focus: bool = False,
    ) -> Worktree:
        self._next += 1
        workspace_id = f"wL{self._next}"
        worktree = Worktree(
            path=Path("/worktrees") / branch.replace("/", "-"),
            branch=branch,
            workspace_id=workspace_id,
            pane_id=f"{workspace_id}:p1",
        )
        self.workspaces[workspace_id] = label
        self.checkouts.append(worktree)
        self.tab_labels.setdefault(workspace_id, {})[f"{workspace_id}:t1"] = "1"
        return worktree

    def open_worktree(
        self, *, source_workspace: str, path: Path, label: str, focus: bool = False
    ) -> Worktree:
        self._next += 1
        workspace_id = f"wL{self._next}"
        dormant = next(item for item in self.checkouts if item.path == path)
        reopened = Worktree(
            path=path,
            branch=dormant.branch,
            workspace_id=workspace_id,
            pane_id=f"{workspace_id}:p1",
        )
        self.workspaces[workspace_id] = label
        self.checkouts = [reopened if item.path == path else item for item in self.checkouts]
        return reopened

    def create_tab(self, workspace_id: str, cwd: Path, label: str, *, focus: bool = False) -> str:
        tabs = self.tab_labels.setdefault(workspace_id, {})
        tab_id = f"{workspace_id}:t{len(tabs) + 1}"
        tabs[tab_id] = label
        return f"{tab_id}:p1"

    # -- agents -----------------------------------------------------------

    def pane_agent(self, pane_id: str) -> str | None:
        """Always a shell prompt: no agent is ever started against this fake."""
        return None


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
    working: bool = False
    """Whether :meth:`settled` reports the turn as still running."""

    _pending: TurnResult | None = None

    def run_turn(self, prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        """The whole turn at once, which is what `milhouse step` asks for."""
        return self._act(prompt, issue_id=issue_id)

    def start_turn(self, prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        """Play the scripted turn now and hold the result until it is reaped.

        A dispatched turn's effects happen while nobody is watching, so playing
        the script here rather than in :meth:`finish_turn` is the honest fake.
        """
        self._pending = self._act(prompt, issue_id=issue_id)
        return TurnResult(agent_state="working", error=self._pending.error)

    def settled(self) -> AgentStatus | None:
        if self.working:
            return None
        return self._pending.agent_state if self._pending else "done"

    def finish_turn(self, iteration: int, *, issue_id: str | None = None) -> TurnResult:
        result = self._pending or TurnResult(agent_state="done")
        self._pending = None
        return result

    def exit_agent(self) -> None:
        return None

    def _act(self, prompt: str, *, issue_id: str | None) -> TurnResult:
        """Run the next scripted action against the fake tracker and repo."""
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
