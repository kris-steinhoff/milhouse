"""A narrow client over the ``herdr`` CLI.

Everything milhouse knows about herdr's argv lives here, so swapping the CLI for
the socket API later is one file rather than a refactor
(:doc:`ADR 0001 <../../docs/decisions/0001-shell-out-to-bd-and-herdr>`).

Two things about the CLI shape the code:

- Responses are wrapped: ``{"id": "cli:agent:start", "result": {...}}``.
- **Errors come back with exit status 0** as ``{"error": {"code", "message"}}``,
  so every call has to inspect the payload rather than trust the exit status.
  :func:`HerdrClient._call` is the one place that does.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import proc
from .errors import HerdrError, MilhouseError, TurnTimeoutError

__all__ = ["AgentInfo", "AgentStatus", "HerdrClient", "Workspace", "Worktree"]

AgentStatus = Literal["idle", "working", "blocked", "done", "unknown"]
"""The lifecycle states herdr reports for an agent pane."""

SETTLED: tuple[AgentStatus, ...] = ("idle", "done", "blocked")
"""States that mean a turn is over. ``done`` is the one claude actually reaches."""

TIMEOUT = 120.0
"""Seconds a non-blocking herdr call may take, as a backstop against a wedged server."""


@dataclass(frozen=True)
class Workspace:
    """A herdr workspace and the pane milhouse drives inside it.

    Attributes:
        workspace_id: herdr's id, e.g. ``wG``.
        pane_id: The pane an agent is started in, e.g. ``wG:p1``.
        label: The workspace label, e.g. ``milhouse:hello``.
    """

    workspace_id: str
    pane_id: str
    label: str = ""


@dataclass(frozen=True)
class Worktree:
    """A git worktree herdr knows about, and the workspace holding it open.

    herdr's ``worktree create`` opens the checkout in a **workspace of its own**,
    labelled with whatever ``--label`` was given. That containment is what makes
    a worktree usable as a lane
    (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

    Attributes:
        path: The checkout on disk. herdr puts linked worktrees under
            ``~/.herdr/worktrees/<repo>/<branch>``, outside the repository.
        branch: The branch checked out there.
        workspace_id: The workspace holding it, or ``""`` when nothing has it
            open — a worktree that outlived the workspace it was created in.
        pane_id: A pane to work in, when the call that produced this made one.
    """

    path: Path
    branch: str
    workspace_id: str = ""
    pane_id: str = ""


@dataclass(frozen=True)
class AgentInfo:
    """What herdr reports about a running agent.

    Attributes:
        name: The agent name milhouse gave it, e.g. ``milhouse-hello``.
        status: Its lifecycle state.
        pane_id: The pane it occupies.
    """

    name: str
    status: AgentStatus
    pane_id: str


class HerdrClient:
    """Drives one herdr server through its CLI."""

    def __init__(self, *, cwd: Path | None = None) -> None:
        """Create a client.

        Args:
            cwd: Working directory for ``herdr`` invocations. Only matters for
                relative paths, which milhouse does not use, but it keeps the
                subprocesses anchored somewhere predictable.
        """
        self.cwd = cwd

    # -- workspaces and panes --------------------------------------------

    def create_workspace(self, cwd: Path, label: str, *, focus: bool = False) -> Workspace:
        """Create a workspace whose root pane sits at a shell prompt.

        Args:
            cwd: Working directory the pane opens in — the repository root.
            label: Workspace label, e.g. ``milhouse:hello``.
            focus: Bring the workspace to the front. ``False`` keeps an
                unattended run from stealing the user's screen.

        Returns:
            The workspace and its root pane.
        """
        argv = [
            "workspace",
            "create",
            "--cwd",
            str(cwd),
            "--label",
            label,
            "--focus" if focus else "--no-focus",
        ]
        result = self._call(argv)
        return Workspace(
            workspace_id=_dig(result, "workspace", "workspace_id"),
            pane_id=_dig(result, "root_pane", "pane_id"),
            label=label,
        )

    def workspace_exists(self, workspace_id: str) -> bool:
        """Whether ``workspace_id`` is still open.

        Used when a workspace is named by configuration or by the environment: it
        may have been closed by a human since
        (:doc:`ADR 0008 <../../docs/decisions/0008-crash-recovery-by-reconciliation>`).
        """
        try:
            self._call(["workspace", "get", workspace_id])
        except HerdrError:
            return False
        return True

    def find_workspace(self, label: str) -> str | None:
        """The id of the open workspace labelled ``label``, or ``None``.

        This is how a second run rejoins the first one's workspace without
        milhouse writing the id down anywhere. herdr owns the workspace, so
        herdr is asked
        (:doc:`ADR 0021 <../../docs/decisions/0021-iteration-history-goes-in-the-beads-audit-log>`).

        Args:
            label: The label to match exactly, e.g. ``milhouse:greet``.

        Returns:
            The first matching workspace id, or ``None`` when there is none.
        """
        try:
            workspaces = self._call(["workspace", "list"]).get("workspaces", [])
        except HerdrError:
            return None
        for workspace in workspaces:
            if isinstance(workspace, dict) and workspace.get("label") == label:
                return str(workspace["workspace_id"])
        return None

    def close_workspace(self, workspace_id: str) -> None:
        """Close a workspace. milhouse only ever does this to one it created."""
        self._call(["workspace", "close", workspace_id])

    def workspace_labels(self) -> dict[str, str]:
        """Every open workspace's label, keyed by id.

        The lane registry is built from this and :meth:`worktrees`: a lane is a
        worktree of this repository whose workspace is labelled with an issue id.
        """
        workspaces = self._call(["workspace", "list"]).get("workspaces", [])
        return {
            str(item["workspace_id"]): str(item.get("label") or "")
            for item in workspaces
            if isinstance(item, dict) and item.get("workspace_id")
        }

    def panes_in(self, workspace_id: str, *, tab_id: str | None = None) -> list[dict[str, Any]]:
        """Every pane herdr reports for ``workspace_id``, in its own order.

        Args:
            workspace_id: The workspace to look in.
            tab_id: Narrow to one tab. A stacked issue gets a tab of its own
                inside its predecessor's lane, and its agent has to start there
                rather than anywhere in the workspace.
        """
        panes = self._call(["pane", "list"]).get("panes", [])
        return [
            pane
            for pane in panes
            if pane.get("workspace_id") == workspace_id
            and (tab_id is None or pane.get("tab_id") == tab_id)
        ]

    def tabs(self, workspace_id: str) -> list[dict[str, Any]]:
        """Every tab herdr reports for ``workspace_id``, in its own order."""
        return list(self._call(["tab", "list", "--workspace", workspace_id]).get("tabs", []))

    def first_pane(self, workspace_id: str) -> str:
        """Return a pane id belonging to ``workspace_id``.

        Args:
            workspace_id: The workspace to look in.

        Returns:
            The first pane herdr reports for it.

        Raises:
            HerdrError: The workspace has no panes, which should not happen.
        """
        panes = self.panes_in(workspace_id)
        if not panes:
            raise HerdrError(f"herdr workspace {workspace_id} has no panes")
        return str(panes[0]["pane_id"])

    def pane_to_work_in(
        self,
        workspace_id: str,
        cwd: Path,
        *,
        avoid: str | None = None,
        tab_id: str | None = None,
    ) -> str:
        """Find a pane in ``workspace_id`` that milhouse may drive, or make one.

        A pane is only usable if it is empty. A pane already running an agent is
        somebody's session — very often the caller's own, because herdr exports
        ``HERDR_WORKSPACE_ID`` into every pane it launches, so ``milhouse step``
        typed into a pane reuses the workspace that pane belongs to. Taking that
        pane would send the exit keys to the terminal the user is sitting in.

        Args:
            workspace_id: The workspace to find a pane in.
            cwd: Working directory for a pane that has to be created.
            avoid: A pane to skip whatever its state, namely the caller's own.
            tab_id: Narrow the search to one tab of the workspace.

        Returns:
            An empty pane's id, splitting a new one when every pane is in use.

        Raises:
            HerdrError: The workspace, or the named tab, has no panes at all.
        """
        panes = self.panes_in(workspace_id, tab_id=tab_id)
        if not panes:
            where = f"{workspace_id} tab {tab_id}" if tab_id else workspace_id
            raise HerdrError(f"herdr workspace {where} has no panes")
        for pane in panes:
            pane_id = str(pane["pane_id"])
            if pane_id != avoid and not pane.get("agent"):
                return pane_id
        return self.split_pane(str(panes[0]["pane_id"]), cwd)

    # -- worktrees and tabs ----------------------------------------------

    def worktrees(self, repo_root: Path) -> list[Worktree]:
        """Every worktree of the repository at ``repo_root``, primary one included.

        This is the lane registry, which is why milhouse keeps no lane state of
        its own. herdr owns the worktree, so herdr is asked
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

        Args:
            repo_root: The primary checkout, which herdr resolves the repo from.

        Returns:
            One :class:`Worktree` per checkout, without panes.
        """
        result = self._call(["worktree", "list", "--cwd", str(repo_root)])
        found = []
        for item in result.get("worktrees", []):
            if not isinstance(item, dict) or not item.get("path"):
                continue
            found.append(
                Worktree(
                    path=Path(str(item["path"])),
                    branch=str(item.get("branch") or ""),
                    workspace_id=str(item.get("open_workspace_id") or ""),
                )
            )
        return found

    def create_worktree(
        self,
        *,
        source_workspace: str,
        branch: str,
        base: str,
        label: str,
        focus: bool = False,
    ) -> Worktree:
        """Create a worktree and open it in a workspace of its own.

        Args:
            source_workspace: The workspace of the primary checkout, which is how
                herdr knows which repository to branch from.
            branch: Branch to create, e.g. ``milhouse/bd-e.1``.
            base: Ref to branch from.
            label: Label for the new workspace. milhouse uses the issue id, which
                is what :meth:`worktrees` plus :meth:`workspace_labels` then finds
                it again by.
            focus: Bring the new workspace to the front.

        Returns:
            The worktree, its workspace, and the pane it opened with.

        Raises:
            HerdrError: The worktree could not be created — most often because
                the branch or the checkout path already exists.
        """
        result = self._call(
            [
                "worktree",
                "create",
                "--workspace",
                source_workspace,
                "--branch",
                branch,
                "--base",
                base,
                "--label",
                label,
                "--focus" if focus else "--no-focus",
            ]
        )
        return Worktree(
            path=Path(_dig(result, "worktree", "path")),
            branch=_dig(result, "worktree", "branch"),
            workspace_id=_dig(result, "workspace", "workspace_id"),
            pane_id=_dig(result, "root_pane", "pane_id"),
        )

    def open_worktree(
        self, *, source_workspace: str, path: Path, label: str, focus: bool = False
    ) -> Worktree:
        """Re-open a worktree that exists on disk but has no workspace holding it.

        A lane outlives the workspace it was created in: closing the workspace
        leaves the checkout and its branch alone. Resuming that issue means
        opening it again rather than creating anything.

        Args:
            source_workspace: The workspace of the primary checkout.
            path: The existing checkout.
            label: Label for the workspace, namely the issue id.
            focus: Bring it to the front.

        Returns:
            The worktree, its workspace, and the pane it opened with.
        """
        result = self._call(
            [
                "worktree",
                "open",
                "--workspace",
                source_workspace,
                "--path",
                str(path),
                "--label",
                label,
                "--focus" if focus else "--no-focus",
            ]
        )
        return Worktree(
            path=Path(_dig(result, "worktree", "path")),
            branch=_dig(result, "worktree", "branch"),
            workspace_id=_dig(result, "workspace", "workspace_id"),
            pane_id=_dig(result, "root_pane", "pane_id"),
        )

    def create_tab(self, workspace_id: str, cwd: Path, label: str, *, focus: bool = False) -> str:
        """Add a tab to a workspace and return the pane it opened with.

        This is how an issue whose blocker ran in a live lane continues on the
        same branch: a new tab in that lane, rather than a worktree branched
        from it (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

        Args:
            workspace_id: The lane's workspace.
            cwd: Working directory for the tab, namely the lane's checkout.
            label: Label for the tab, namely the issue id.
            focus: Bring it to the front.

        Returns:
            The new tab's root pane id.
        """
        result = self._call(
            [
                "tab",
                "create",
                "--workspace",
                workspace_id,
                "--cwd",
                str(cwd),
                "--label",
                label,
                "--focus" if focus else "--no-focus",
            ]
        )
        return _dig(result, "root_pane", "pane_id")

    def pane_agent(self, pane_id: str) -> str | None:
        """The kind of agent occupying ``pane_id``, or ``None`` for a shell prompt.

        This is how milhouse confirms an agent actually exited: herdr drops the
        ``agent`` field once the pane is back at its shell
        (:doc:`ADR 0011 <../../docs/decisions/0011-exiting-the-agent>`).
        """
        pane = self._call(["pane", "get", pane_id]).get("pane", {})
        agent = pane.get("agent")
        return str(agent) if agent else None

    def close_pane(self, pane_id: str) -> None:
        """Close a pane. The fallback half of the agent-exit strategy."""
        self._call(["pane", "close", pane_id], expect_json=False)

    def split_pane(self, pane_id: str, cwd: Path, *, direction: str = "right") -> str:
        """Split ``pane_id`` and return the new pane's id.

        Args:
            pane_id: The pane to split.
            cwd: Working directory for the new pane.
            direction: ``right`` or ``down``. herdr requires one.

        Returns:
            The new pane id.
        """
        result = self._call(
            ["pane", "split", pane_id, "--direction", direction, "--cwd", str(cwd), "--no-focus"]
        )
        return _dig(result, "pane", "pane_id")

    def send_keys(self, pane_id: str, keys: list[str]) -> None:
        """Send key presses to a pane.

        Addressed by pane rather than by agent name on purpose: the sequence that
        exits an agent makes the agent disappear partway through, and an
        agent-addressed call would then fail with ``agent_not_found``.

        Args:
            pane_id: The pane to type into.
            keys: herdr key names, e.g. ``["ctrl+c", "ctrl+c", "ctrl+d"]``. The
                ``ctrl+`` spelling is the one that works for every control key.
                herdr accepts ``c-c`` but rejects ``c-d``, and rejects the
                hyphenated ``ctrl-c``, both with ``invalid_key``.
        """
        self._call(["pane", "send-keys", pane_id, *keys], expect_json=False)

    def read_pane(self, pane_id: str, *, source: str = "visible", lines: int = 400) -> str:
        """Capture a pane's terminal contents as plain text.

        Args:
            pane_id: The pane to read.
            source: ``visible``, ``recent``, ``recent-unwrapped``, or ``detection``.
            lines: How many lines to ask for.

        Returns:
            The transcript, which may be empty.
        """
        result = proc.run(
            self._argv(
                [
                    "pane",
                    "read",
                    pane_id,
                    "--source",
                    source,
                    "--lines",
                    str(lines),
                    "--format",
                    "text",
                ]
            ),
            cwd=self.cwd,
            timeout=TIMEOUT,
            check=False,
        )
        return result.stdout

    # -- agents ----------------------------------------------------------

    def start_agent(
        self,
        name: str,
        *,
        kind: str,
        pane_id: str,
        args: list[str] | None = None,
        timeout_ms: int = 60_000,
    ) -> AgentInfo:
        """Start an agent in a pane and return once herdr reports it ready.

        The pane must be at an interactive shell prompt. herdr only returns once
        it has detected the expected agent, so this is a checkable step rather
        than a sleep.

        Args:
            name: Agent name, used to address it afterwards.
            kind: A ``herdr agent start --kind`` value, e.g. ``claude``.
            pane_id: The pane to start it in.
            args: Extra arguments for the agent binary, passed after ``--``.
            timeout_ms: How long herdr may take to report readiness.

        Returns:
            The started agent.

        Raises:
            HerdrError: The pane was not at a shell prompt, or the agent was not
                detected in time.
        """
        argv = [
            "agent",
            "start",
            name,
            "--kind",
            kind,
            "--pane",
            pane_id,
            "--timeout",
            str(timeout_ms),
        ]
        if args:
            argv += ["--", *args]
        result = self._call(argv, timeout=timeout_ms / 1000 + 30)
        return _agent_info(result.get("agent", {}), fallback_name=name, fallback_pane=pane_id)

    def prompt(
        self,
        name: str,
        text: str,
        *,
        timeout_ms: int,
        until: tuple[AgentStatus, ...] = SETTLED,
    ) -> AgentStatus:
        """Submit a prompt and block until the turn settles.

        This is the whole turn-completion mechanism: one blocking subprocess per
        iteration, which is exactly what a sequential loop wants
        (:doc:`ADR 0003 <../../docs/decisions/0003-agents-run-in-herdr-panes>`).

        Args:
            name: The agent to prompt.
            text: The rendered prompt.
            timeout_ms: How long the turn may take before milhouse gives up.
            until: States that count as settled. Defaults to :data:`SETTLED`,
                which includes ``done`` — the state claude actually reaches when
                a turn ends, as opposed to ``idle``.

        Returns:
            The state the agent settled in.

        Raises:
            TurnTimeoutError: The turn did not settle in time.
            HerdrError: The prompt could not be submitted at all.
        """
        argv = ["agent", "prompt", name, text, "--wait", "--timeout", str(timeout_ms)]
        for status in until:
            argv += ["--until", status]
        try:
            result = self._call(argv, timeout=timeout_ms / 1000 + 30)
        except HerdrError as exc:
            if _is_timeout(exc):
                raise TurnTimeoutError(
                    f"agent {name} did not finish its turn within {timeout_ms}ms"
                ) from exc
            raise
        status = result.get("agent", {}).get("agent_status") or self.agent_status(name)
        return _as_status(status)

    def agent_status(self, name: str) -> AgentStatus:
        """Current lifecycle state of an agent, or ``unknown`` if it is gone."""
        try:
            result = self._call(["agent", "get", name])
        except HerdrError:
            return "unknown"
        return _as_status(result.get("agent", {}).get("agent_status"))

    def read_agent(self, name: str, *, source: str = "visible", lines: int = 400) -> str:
        """Capture an agent pane's terminal contents as plain text."""
        result = proc.run(
            self._argv(
                [
                    "agent",
                    "read",
                    name,
                    "--source",
                    source,
                    "--lines",
                    str(lines),
                    "--format",
                    "text",
                ]
            ),
            cwd=self.cwd,
            timeout=TIMEOUT,
            check=False,
        )
        return result.stdout

    def wait_for_shell(self, pane_id: str, *, timeout_s: float = 8.0) -> bool:
        """Poll until ``pane_id`` is back at a shell prompt.

        Args:
            pane_id: The pane to watch.
            timeout_s: How long to keep checking.

        Returns:
            ``True`` if the pane has no agent, ``False`` if one is still there.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            if self.pane_agent(pane_id) is None:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    # -- plumbing --------------------------------------------------------

    def _argv(self, args: list[str]) -> list[str]:
        """Prefix ``args`` with the herdr executable."""
        return ["herdr", *args]

    def _call(
        self,
        args: list[str],
        *,
        timeout: float = TIMEOUT,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        """Run a herdr command and unwrap its response.

        Args:
            args: herdr arguments, without the executable.
            timeout: Seconds before the subprocess is killed.
            expect_json: Some pane commands print nothing on success. Pass
                ``False`` for those so an empty stdout is not an error.

        Returns:
            The ``result`` object, or ``{}`` for a command that printed nothing.

        Raises:
            HerdrError: The command failed, or herdr reported an error object.
        """
        argv = self._argv(args)
        try:
            payload = proc.run_json(argv, cwd=self.cwd, timeout=timeout, allow_empty=True)
        except MilhouseError as exc:
            raise HerdrError(f"herdr {' '.join(args[:2])} failed: {exc}") from exc
        if payload is None:
            if expect_json:
                raise HerdrError(f"herdr {' '.join(args[:2])} produced no output")
            return {}
        if not isinstance(payload, dict):
            raise HerdrError(f"unexpected herdr output: {payload!r}")
        if "error" in payload:
            error = payload["error"] or {}
            code = error.get("code", "error")
            message = error.get("message", "")
            raise HerdrError(f"herdr {' '.join(args[:2])}: {code}: {message}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}


def _dig(result: dict[str, Any], *path: str) -> str:
    """Pull a nested string out of a herdr result.

    Raises:
        HerdrError: The path is missing, meaning herdr's shape changed.
    """
    node: Any = result
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise HerdrError(f"herdr response is missing {'.'.join(path)}")
        node = node[key]
    return str(node)


def _agent_info(raw: dict[str, Any], *, fallback_name: str, fallback_pane: str) -> AgentInfo:
    """Build an :class:`AgentInfo` from a herdr agent object."""
    return AgentInfo(
        name=str(raw.get("name") or fallback_name),
        status=_as_status(raw.get("agent_status")),
        pane_id=str(raw.get("pane_id") or fallback_pane),
    )


def _as_status(value: Any) -> AgentStatus:
    """Coerce a herdr status string to :data:`AgentStatus`, defaulting to unknown."""
    if value in ("idle", "working", "blocked", "done", "unknown"):
        return value
    return "unknown"


def _is_timeout(exc: HerdrError) -> bool:
    """Whether a herdr error was the server's ``timeout`` code."""
    return "timeout" in str(exc)
