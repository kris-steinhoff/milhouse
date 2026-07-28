"""Everything a run needs that is not the loop.

:class:`Session` owns the resources: the run lock, the branch, the herdr
workspace and pane, the agent runner, and the claim currently in flight. It is a
context manager, so opening and tearing all of that down is one ``with``.

It **stores nothing**. Every fact it needs comes back from whatever owns it —
``bd`` for the issues, herdr for the workspace, git for the branch, and the
beads audit log for the history — so there is no session file to go stale, and
nothing to reconcile against
(:doc:`ADR 0021 <../../docs/decisions/0021-iteration-history-goes-in-the-beads-audit-log>`).

It holds **no policy** either. It does not decide what to work on next or when to
retry. That separation is what will let a loop over many iterations reuse
everything ``milhouse step`` already does, whenever there is one worth writing
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).
"""

from __future__ import annotations

import contextlib
import logging
import signal
from collections.abc import Callable
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any

from .audit import AuditLog
from .config import Config
from .errors import MilhouseError, UserAbortError
from .gitrepo import GitRepo
from .herdr import HerdrClient, Workspace
from .models import Issue, Iteration
from .policy import Decision
from .rundir import LOCK_FILENAME, RunLock
from .runner import AgentRunner, Runner
from .tracker.base import Tracker

__all__ = ["Reporter", "Session"]

log = logging.getLogger(__name__)

Reporter = Callable[[str], None]
"""Where progress lines go. The CLI passes ``typer.echo``."""


class Session:
    """One repository's resources, for as long as milhouse is working it."""

    def __init__(
        self,
        config: Config,
        *,
        tracker: Tracker,
        client: HerdrClient,
        repo: GitRepo,
        audit: AuditLog | None = None,
        report: Reporter = lambda line: None,
        attach: bool = False,
        runner: Runner | None = None,
    ) -> None:
        """Assemble a session from its collaborators.

        Args:
            config: Resolved configuration.
            tracker: Where issues live.
            client: The herdr client.
            repo: The git repository being worked in.
            audit: Where iterations are recorded. Defaults to this repository's
                beads audit log.
            report: Called with each human-readable progress line.
            attach: Focus the workspace when creating it, so a human watches it
                happen. Off by default, so a background run does not steal the
                screen.
            runner: Run turns with this instead of starting agents in the pane.
                Nothing in the package passes one; the tests do.
        """
        self.config = config
        self.tracker = tracker
        self.client = client
        self.repo = repo
        self.audit = audit or AuditLog(config.repo_root)
        self.report = report
        self.attach = attach
        self.lock = RunLock(config.run_dir() / LOCK_FILENAME)
        self.branch: str | None = None
        self.claimed_issue: str | None = None
        self.workspace: Workspace | None = None
        self._runner: Runner | None = runner
        self._signals: dict[int, Any] = {}

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> Session:
        """Take the lock, reconcile, note the branch, and open the workspace.

        Raises:
            RunLockedError: Another milhouse process is working this repository.
        """
        self._catch_signals()
        stale = self.lock.acquire()
        if stale is not None:
            self.report(f"took over the run lock from a dead run ({stale.describe()})")
        try:
            self.reconcile()
            self.branch = self.repo.current_branch()
            self.open_workspace()
        except BaseException:
            self.lock.release()
            self._restore_signals()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Release any in-flight claim, exit the agent, and drop the lock.

        The workspace is deliberately left open, whether or not the work failed,
        so the panes can be inspected
        (:doc:`ADR 0005 <../../docs/decisions/0005-milhouse-owns-the-loop>`).
        """
        try:
            if self.claimed_issue:
                self.release_claim("Re-opened by milhouse: the run did not finish this issue.")
            self.exit_agent()
        finally:
            self.lock.release()
            self._restore_signals()
        if self.workspace is not None:
            self.report(f"the herdr workspace {self.workspace.workspace_id} is left open")
        return False

    def _catch_signals(self) -> None:
        """Turn SIGINT and SIGTERM into an exception, so teardown gets to run.

        The default disposition for ``SIGTERM`` kills the process outright, which
        would skip :meth:`__exit__` and leave both the claim and the run lock
        behind. Raising instead unwinds through the ``with``, which is what
        reverts the claim and drops the lock.
        """

        def handle(signum: int, frame: FrameType | None) -> None:
            raise UserAbortError("interrupted")

        for number in (signal.SIGINT, signal.SIGTERM):
            # ValueError means we are not on the main thread, where handlers
            # cannot be installed; the caller keeps whatever it had.
            with contextlib.suppress(ValueError):
                self._signals[number] = signal.getsignal(number)
                signal.signal(number, handle)

    def _restore_signals(self) -> None:
        """Put back whatever handlers were installed before this session."""
        for number, handler in self._signals.items():
            with contextlib.suppress(ValueError):
                signal.signal(number, handler)
        self._signals.clear()

    def reconcile(self) -> None:
        """Re-open whatever a previous run claimed and never finished.

        A run killed with ``SIGKILL`` leaves an issue ``in_progress`` forever,
        because ``bd`` has no lease expiry, and ``bd ready`` excludes those, so
        it would never be offered again. Re-running is the recovery mechanism
        (:doc:`ADR 0008 <../../docs/decisions/0008-crash-recovery-by-reconciliation>`).

        The audit log is what says which claims were milhouse's: a turn writes a
        ``claim`` entry before it starts and an ``iteration`` entry when it ends,
        so a claim with nothing after it is a run that died mid-turn. Reading it
        rather than "every in-progress issue" is what keeps this from re-opening
        an issue a person claimed by hand.

        Holding the run lock first is what makes it safe: the claim being
        re-opened cannot belong to a run that is still working it
        (:doc:`ADR 0015 <../../docs/decisions/0015-one-run-at-a-time>`).
        """
        for issue_id in self.audit.unsettled_claims():
            self.report(f"reconciling: re-opening {issue_id}, claimed by a run that did not finish")
            self.claimed_issue = issue_id
            self.release_claim("Re-opened by milhouse: the previous run did not finish.")

    # -- resources --------------------------------------------------------

    @property
    def workspace_label(self) -> str:
        """The label milhouse gives this repository's workspace."""
        return f"milhouse:{self.config.repo_root.name}"

    def open_workspace(self) -> None:
        """Reuse the configured workspace, find milhouse's own, or create one.

        milhouse writes no workspace id down. It asks the tool that owns them:
        the configured id if there is one, otherwise the open workspace carrying
        this repository's label, otherwise a fresh one.

        A reused workspace is not an empty one. Its panes may be running agents,
        including the one milhouse was typed into, so the pane to work in is
        chosen rather than taken.
        """
        configured = self.config.herdr.workspace
        existing = configured if configured and self.client.workspace_exists(configured) else None
        existing = existing or self.client.find_workspace(self.workspace_label)
        if existing:
            workspace = Workspace(
                workspace_id=existing,
                pane_id=self.client.pane_to_work_in(
                    existing,
                    self.config.repo_root,
                    avoid=self.config.herdr.self_pane,
                ),
                label=self.workspace_label,
            )
        else:
            workspace = self.client.create_workspace(
                self.config.repo_root,
                self.workspace_label,
                focus=self.attach,
            )
            self.report(f"created herdr workspace {workspace.workspace_id} ({workspace.label})")

        self.workspace = workspace
        if self._runner is None:
            self._runner = AgentRunner(
                self.client,
                self.config,
                run_dir=self.config.run_dir(),
                pane_id=workspace.pane_id,
                agent_name=f"milhouse-{self.config.repo_root.name}",
            )

    @property
    def runner(self) -> Runner:
        """Whatever runs this session's turns.

        Raises:
            MilhouseError: Called before the workspace was opened, which is a bug.
        """
        if self._runner is None:
            raise MilhouseError("no herdr workspace has been opened yet")
        return self._runner

    def exit_agent(self) -> None:
        """Best-effort return of the pane to a shell prompt."""
        if self._runner is None:
            return
        try:
            self._runner.exit_agent()
        except MilhouseError as exc:
            log.warning("could not exit the agent: %s", exc)

    # -- the work ---------------------------------------------------------

    def claim(self) -> Issue | None:
        """Claim the next ready issue, and say so in the audit log.

        The entry is what a later run reads to tell a claim milhouse abandoned
        from one a person made by hand, so it is written before the turn starts
        rather than after it.

        Returns:
            The claimed issue, or ``None`` when nothing is ready.
        """
        issue = self.tracker.ready(claim=True)
        if issue is None:
            return None
        self.claimed_issue = issue.id
        self.audit.claimed(issue.id)
        return issue

    def background(self, issue: Issue) -> str:
        """The parent epic's description, which is this issue's wider context.

        With no task definition there is no separate document saying what the
        work is for, so the prompt takes the parent's description instead
        (:doc:`ADR 0018 <../../docs/decisions/0018-no-task-milhouse-works-the-ready-queue>`).
        An issue with no parent, or a parent bd will not hand back, simply gets
        no background: it is context, and a turn without it is still a turn.

        Args:
            issue: The issue about to be worked.

        Returns:
            The parent's description, or the empty string.
        """
        if not issue.parent:
            return ""
        try:
            return self.tracker.get(issue.parent).description
        except MilhouseError as exc:
            log.warning("could not read the parent of %s: %s", issue.id, exc)
            return ""

    def settle(self, decision: Decision) -> None:
        """Apply a policy decision to the issue currently in flight.

        Args:
            decision: What :func:`milhouse.policy.decide` returned.
        """
        if decision.issue == "release":
            self.release_claim(decision.note)
        else:
            self.claimed_issue = None

    def release_claim(self, note: str | None = None) -> None:
        """Return the in-flight issue to the open pool, tolerating a bd hiccup."""
        issue_id = self.claimed_issue
        if issue_id is None:
            return
        try:
            self.tracker.release(issue_id, note=note)
        except MilhouseError as exc:
            log.warning("could not re-open %s: %s", issue_id, exc)
        self.claimed_issue = None

    def record(self, iteration: Iteration) -> None:
        """Record an iteration in the beads audit log."""
        self.audit.record(iteration)

    def history_for(self, issue_id: str) -> list[Iteration]:
        """Earlier iterations that worked ``issue_id``, oldest first."""
        return self.audit.iterations_for(issue_id)

    def next_number(self) -> int:
        """The number the next iteration gets."""
        return self.audit.next_number()

    def unfinished(self) -> list[Issue]:
        """Issues in scope that are not closed.

        Epics are left out, the same way the ready queue leaves them out: an
        epic is a container for the work rather than a unit of it, so an epic
        nobody has got round to closing is not unfinished work.
        """
        return [
            issue
            for issue in self.tracker.children()
            if not issue.is_closed and issue.issue_type != "epic"
        ]

    def relative(self, path: Path | None) -> str | None:
        """Render an artifact path relative to the repo root, for the audit entry."""
        if path is None:
            return None
        try:
            return str(path.relative_to(self.config.repo_root))
        except ValueError:
            return str(path)
