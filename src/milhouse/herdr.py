"""A narrow client over the ``herdr`` CLI.

Everything milhouse knows about herdr's argv lives here, so swapping the CLI for
the socket API later is one file rather than a refactor
(:doc:`ADR 0001 <../../docs/decisions/0001-shell-out-to-bd-and-herdr>`).

Three things about the CLI shape the code:

- Responses are wrapped: ``{"id": "cli:agent:start", "result": {...}}``.
- **An error is an envelope on stderr, with a non-zero exit status**:
  ``{"id": ..., "error": {"code", "message"}}``. So a failure is only readable
  if the envelope is looked for on both streams whatever the status, which is
  what :func:`HerdrClient._call` does — trusting the exit status leaves every
  herdr failure surfacing as a subprocess error quoting raw JSON.
- **One identifier herdr takes is validated, and the labels are not.** An agent
  name has a grammar (:data:`AGENT_NAME`), checked here before the call, so a
  name milhouse built badly is refused before anything is created. A workspace,
  worktree or tab label is free text that herdr stores verbatim, so an issue id
  goes into one whole, which is what milhouse then finds the lane again by.
"""

from __future__ import annotations

import json
import re
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

AGENT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
"""herdr's grammar for an agent name, which is herdr's rule and so lives here.

The grammar is written ``^[a-z][a-z0-9_-]{0,31}$`` where herdr states it, and
kept unanchored here because every use is a :meth:`~re.Pattern.fullmatch`.
:attr:`~milhouse.lanes.Lane.agent_name` is built to satisfy it, and
:meth:`HerdrClient.start_agent` is where that is checked rather than assumed: a
name outside it used to come back as raw CLI JSON, after the lane had been
opened and the issue claimed.

Confirmed by probing herdr 0.7.5 rather than read out of its source. Refused: 33
characters, a leading digit, uppercase, an empty name. Taken: 32 characters,
underscores, a trailing hyphen. Nothing pins the herdr version milhouse talks to,
so whether an older server enforced this is unknown.
"""

AGENT_NAME_RULE = (
    "must start with a lowercase letter and contain only lowercase letters, "
    "digits, '-' or '_' (1-32 characters)"
)
"""herdr's own words for :data:`AGENT_NAME`, quoted so both refusals read alike.

A name refused here never reaches herdr, so this is the only place the sentence
can come from, and keeping herdr's wording means one search finds either failure.
"""


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
            ``~/.herdr/worktrees/<repo>/<branch>``, outside the repository, with
            ``/`` and ``.`` in the branch flattened to ``-``:
            ``milhouse/bd-e.1`` becomes ``milhouse-bd-e-1``. So two branches
            differing only in ``.`` against ``-`` want the one directory, and the
            second :meth:`HerdrClient.create_worktree` fails with
            ``worktree_create_failed``. Distinct bead ids do not collide there in
            practice, and the failure is loud when they do.
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
            label: Workspace label, e.g. ``milhouse:hello``. Unconstrained: herdr
                stores a label verbatim, which is why the colon is free. Probed
                against 0.7.5 with dots, spaces, uppercase, two hundred
                characters and the empty string, all of which came back unchanged.
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

    def workspace_repo(self, workspace_id: str) -> Path | None:
        """The repository ``workspace_id`` is a checkout of, if herdr knows one.

        herdr resolves the repository for a new worktree from its **source
        workspace**, so a workspace belonging to another repository silently
        branches the wrong one. This is what makes that checkable.

        Returns:
            The repository root, or ``None`` for a workspace with no worktree
            (herdr allows one) or a herdr that will not answer.
        """
        try:
            result = self._call(["workspace", "get", workspace_id])
        except HerdrError:
            return None
        # Not _dig: every step here is legitimately absent for a workspace that
        # is not a checkout, and that is an answer rather than a broken schema.
        node: Any = result
        for key in ("workspace", "worktree", "repo_root"):
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return Path(str(node)) if node else None

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
                it again by. Unconstrained, as in :meth:`create_workspace`, so the
                ``.N`` on a child issue survives and the id can go in whole. That
                is a requirement rather than a convenience: the lookup matches the
                label exactly, so an id sanitized on the way in is a lane nothing
                finds on the way out.
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
            label: Label for the workspace, namely the issue id. Unconstrained and
                matched exactly, as in :meth:`create_worktree`.
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
            label: Label for the tab, namely the issue id. Unconstrained like a
                workspace label (probed against 0.7.5 with the same dots, spaces
                and lengths), and matched exactly by the lane lookup, so the id
                goes in unchanged. herdr labels a tab nobody named with its
                number, so ``1`` is a label a lane can hold without meaning one.
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

        This is the only call that *introduces* a name, so it is the only one that
        checks it against :data:`AGENT_NAME`. Everything after it addresses an
        agent herdr already accepted, and an unknown name there is
        ``agent_not_found``, which says what it is. Refusing early is what keeps
        a name milhouse built badly from being discovered as CLI JSON in the
        middle of an iteration, with the lane opened and the issue claimed.

        Args:
            name: Agent name, used to address it afterwards. Must match
                :data:`AGENT_NAME`.
            kind: A ``herdr agent start --kind`` value, e.g. ``claude``.
            pane_id: The pane to start it in.
            args: Extra arguments for the agent binary, passed after ``--``.
            timeout_ms: How long herdr may take to report readiness.

        Returns:
            The started agent.

        Raises:
            HerdrError: The name is outside herdr's grammar, in which case nothing
                was started at all. Or the pane was not at a shell prompt, or the
                agent was not detected in time.
        """
        if not AGENT_NAME.fullmatch(name):
            raise HerdrError(f"invalid herdr agent name {name!r}: an agent name {AGENT_NAME_RULE}")
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
        wait: bool = True,
    ) -> AgentStatus:
        """Submit a prompt, optionally blocking until the turn settles.

        Waiting is the turn-completion mechanism for ``milhouse step``: one
        blocking subprocess per iteration, which is exactly what a sequential
        step wants
        (:doc:`ADR 0003 <../../docs/decisions/0003-agents-run-in-herdr-panes>`).

        Not waiting is what lets several turns be in flight at once: the caller
        submits, returns, and asks :meth:`agent_status` later
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

        Args:
            name: The agent to prompt.
            text: The rendered prompt.
            timeout_ms: How long the turn may take before milhouse gives up.
                Ignored when ``wait`` is false, since nothing is being waited on.
            until: States that count as settled. Defaults to :data:`SETTLED`,
                which includes ``done`` — the state claude actually reaches when
                a turn ends, as opposed to ``idle``.
            wait: Block until the agent reaches one of ``until``.

        Returns:
            The state the agent settled in, or the state it was in at
            submission when ``wait`` is false.

        Raises:
            TurnTimeoutError: The turn did not settle in time.
            HerdrError: The prompt could not be submitted at all.
        """
        argv = ["agent", "prompt", name, text]
        if wait:
            argv += ["--wait", "--timeout", str(timeout_ms)]
            for status in until:
                argv += ["--until", status]
        try:
            result = self._call(argv, timeout=(timeout_ms / 1000 + 30) if wait else TIMEOUT)
        except HerdrError as exc:
            if wait and _is_timeout(exc):
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

    def agent_pane(self, name: str) -> str | None:
        """The pane an agent occupies, or ``None`` when herdr has lost track of it.

        Reaping a dispatched turn has to send the exit keys to the pane the
        agent is *in*, which is not necessarily the pane a fresh lookup would
        pick — that one skips panes with an agent in them, which is exactly this
        one (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).
        """
        try:
            result = self._call(["agent", "get", name])
        except HerdrError:
            return None
        pane = result.get("agent", {}).get("pane_id")
        return str(pane) if pane else None

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

        The envelope is read before the exit status is judged, and from either
        stream, because that is the only order in which a herdr error is legible.
        Against 0.7.5 a result comes back on stdout with status 0 and an error
        comes back on **stderr** with status 1, so a call that let
        :func:`~milhouse.proc.run_json` raise on the status never saw the error
        object at all: the code and message arrived quoted inside a subprocess
        failure instead of as a sentence. Reading the envelope first is also what
        keeps this working if a herdr writes one somewhere else, which matters
        because milhouse pins no herdr version.

        Args:
            args: herdr arguments, without the executable.
            timeout: Seconds before the subprocess is killed.
            expect_json: Some pane commands print nothing on success. Pass
                ``False`` for those so an empty stdout is not an error.

        Returns:
            The ``result`` object, or ``{}`` for a command that printed nothing.

        Raises:
            HerdrError: herdr reported an error object, could not be run, or
                answered with something that is not a herdr response.
        """
        argv = self._argv(args)
        label = f"herdr {' '.join(args[:2])}"
        try:
            completed = proc.run(argv, cwd=self.cwd, timeout=timeout, check=False)
        except MilhouseError as exc:
            # Not on PATH, or killed at the timeout: there is no envelope to read.
            raise HerdrError(f"{label} failed: {exc}") from exc
        payload = _envelope(completed)
        if isinstance(payload, dict) and "error" in payload:
            error = payload["error"] or {}
            code = str(error.get("code") or "error")
            raise HerdrError(f"{label}: {code}: {error.get('message', '')}", code=code)
        if not completed.ok:
            # No envelope to quote: an argv herdr rejects itself (status 2), or
            # a herdr that fell over before it could answer.
            raise HerdrError(f"{label} exited {completed.returncode}: {_said(completed)}")
        if payload is None:
            said = _said(completed)
            if said:
                raise HerdrError(f"{label} produced unreadable output: {said}")
            if expect_json:
                raise HerdrError(f"{label} produced no output")
            return {}
        if not isinstance(payload, dict):
            raise HerdrError(f"unexpected herdr output: {payload!r}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}


def _envelope(completed: proc.ProcResult) -> Any:
    """Parse herdr's response envelope from whichever stream carries it.

    Args:
        completed: The finished ``herdr`` invocation, whatever its exit status.

    Returns:
        The parsed document, or ``None`` when neither stream holds JSON — a
        command that printed nothing, or one that failed before herdr's own
        argument parsing produced an envelope.
    """
    for stream in (completed.stdout, completed.stderr):
        text = stream.strip()
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def _said(completed: proc.ProcResult) -> str:
    """The first non-blank line herdr printed, for output milhouse cannot parse.

    stderr first, because the cases that reach this are failures and that is
    where herdr puts them. A command that exited zero has nothing there.
    """
    for line in (completed.stderr + "\n" + completed.stdout).splitlines():
        if line.strip():
            return line.strip()
    return ""


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
    """Whether a herdr error was the server's ``timeout`` code.

    The code, exactly, rather than the message. ``herdr agent prompt --help``
    states that a ``--timeout`` shorter than the state-change wait "returns
    timeout instead", so this is the code that means the turn ran out — while
    ``invalid_agent_timeout``, an argument herdr refused, contains the same word
    and means nothing was waited on at all. Matching the message could not tell
    those apart, and it only worked before because the raw JSON it searched
    happened to have the code in it.
    """
    return exc.code == "timeout"
