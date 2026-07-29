"""Everything a run needs that is not the loop.

:class:`Session` owns the resources: the lane locks, the branch, the herdr source
workspace, the agent runners, and whatever claims are in flight. It is a context
manager, so opening and tearing all of that down is one ``with``.

The lock is **per lane**, not per repository. Several turns running at once is
the point of lanes, so the thing being protected is one lane, not the run
(:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

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
from .lanes import Lane, Lanes
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
        lane_key: str | None = None,
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
            lane_key: Work every issue in the one lane labelled this, and hold
                one lock on it. What ``milhouse run`` passes, so a whole target
                lands on one reviewable branch
                (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).
                ``None`` gives each issue a lane and a lock of its own, which is
                what ``step``, ``dispatch``, and ``reap`` want.
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
        self.lane_key = lane_key
        self.lanes = Lanes(client, config)
        self.branch: str | None = None
        self.workspace: Workspace | None = None
        self.in_flight: list[str] = []
        """Issues this process claimed and has neither settled nor handed off."""

        self._locks: dict[str, RunLock] = {}
        self._opened: dict[str, Lane] = {}
        self._runner: Runner | None = runner
        self._active: Runner | None = None
        self._signals: dict[int, Any] = {}

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> Session:
        """Reconcile, note the branch, and open the source workspace."""
        self._catch_signals()
        try:
            self.reconcile()
            self.branch = self.repo.current_branch()
            self.open_workspace()
        except BaseException:
            self._release_locks()
            self._restore_signals()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Release any claim still in flight, exit the agent, and drop the locks.

        A claim that was **handed off** — dispatched to an agent that is still
        running — is deliberately left alone. It belongs to the lane now, and
        ``milhouse reap`` is what settles it.

        The workspace is deliberately left open, whether or not the work failed,
        so the panes can be inspected
        (:doc:`ADR 0005 <../../docs/decisions/0005-milhouse-owns-the-loop>`).
        """
        try:
            for issue_id in list(self.in_flight):
                self.release_claim(
                    issue_id, "Re-opened by milhouse: the run did not finish this issue."
                )
            self.exit_agent()
        finally:
            self._release_locks()
            self._restore_signals()
        if self.workspace is not None:
            self.report(f"the herdr workspace {self.workspace.workspace_id} is left open")
        return False

    # -- the per-lane lock ------------------------------------------------

    def lock_for(self, issue_id: str) -> RunLock:
        """Take the lock on one issue's lane, and hold it for this session.

        The lock is per lane rather than per repository, because concurrent lanes
        are the point
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).
        Two dispatchers cannot collide over *which* issue to take — ``bd ready
        --claim`` is atomic — so all this stops is two processes working the
        same lane, which is the case that would drive one pane from two places.

        A run works every issue in one lane, so it takes one lock, on the
        target. Taking a lock per issue would let a second run of the same
        target start the moment the first one moved on
        (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).

        Args:
            issue_id: The issue whose lane is being worked. Ignored when this
                session has a :attr:`lane_key`, which names the only lane there
                is.

        Returns:
            The held lock.

        Raises:
            RunLockedError: A live process already holds this lane.
        """
        key = self.lane_key or issue_id
        held = self._locks.get(key)
        if held is not None:
            return held
        lock = RunLock(self.config.run_dir() / key / LOCK_FILENAME)
        stale = lock.acquire()
        if stale is not None:
            self.report(f"took over the lock on {key} from a dead run ({stale.describe()})")
        self._locks[key] = lock
        return lock

    def _release_locks(self) -> None:
        """Drop every lane lock this session took."""
        for lock in self._locks.values():
            lock.release()
        self._locks.clear()

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
        """Re-open whatever a previous run claimed and abandoned.

        A run killed with ``SIGKILL`` leaves an issue ``in_progress`` forever,
        because ``bd`` has no lease expiry, and ``bd ready`` excludes those, so
        it would never be offered again. Re-running is the recovery mechanism
        (:doc:`ADR 0008 <../../docs/decisions/0008-crash-recovery-by-reconciliation>`).

        Two questions decide it, and both are answered by tools that own their
        answer. The audit log says which claims were milhouse's — a turn writes
        a ``claim`` entry before it starts and an ``iteration`` entry when it
        ends — which keeps this from re-opening an issue a person claimed by
        hand. herdr's lane registry then says which of those are still live: an
        issue whose lane is gone has nobody working it, and one whose lane is
        open is either running or waiting to be reaped
        (:doc:`ADR 0021 <../../docs/decisions/0021-iteration-history-goes-in-the-beads-audit-log>`).

        Taking each lane's lock first is what makes it safe: the claim being
        re-opened cannot belong to a dispatcher that is still setting it up.
        """
        for issue_id in self.audit.unsettled_claims():
            if self.lanes.locate(issue_id) is not None:
                continue
            self.lock_for(issue_id)
            self.report(f"reconciling: re-opening {issue_id}, claimed by a run that did not finish")
            self.release_claim(issue_id, "Re-opened by milhouse: the previous run did not finish.")

    # -- resources --------------------------------------------------------

    @property
    def workspace_label(self) -> str:
        """The label milhouse gives this repository's workspace."""
        return f"milhouse:{self.config.repo_root.name}"

    def open_workspace(self) -> None:
        """Reuse the configured workspace, find milhouse's own, or create one.

        This is the **source** workspace: the primary checkout, which is how
        herdr knows which repository a lane's worktree comes from. No agent runs
        in it — every turn happens in a lane
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

        milhouse writes no workspace id down. It asks the tool that owns them:
        the configured id if there is one, otherwise the open workspace carrying
        this repository's label, otherwise a fresh one.
        """
        configured = self.config.herdr.workspace
        existing = configured if configured and self.client.workspace_exists(configured) else None
        existing = existing or self.client.find_workspace(self.workspace_label)
        if existing:
            self.workspace = Workspace(
                workspace_id=existing, pane_id="", label=self.workspace_label
            )
            return
        self.workspace = self.client.create_workspace(
            self.config.repo_root,
            self.workspace_label,
            focus=self.attach,
        )
        self.report(
            f"created herdr workspace {self.workspace.workspace_id} ({self.workspace.label})"
        )

    def runner_for(self, issue: Issue) -> Runner:
        """Open ``issue``'s lane and return a runner bound to it.

        Each issue gets its own lane, its own branch, and its own agent name, so
        two turns can be in flight without either seeing the other's pane or
        commits. A session with a :attr:`lane_key` works them all in that one
        lane instead
        (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).

        Args:
            issue: The claimed issue.

        Returns:
            A runner working in that lane.

        Raises:
            MilhouseError: The workspace has not been opened, or the issue's
                blockers ran in more than one lane.
        """
        if self._runner is not None:
            # Injected by the tests, which run no agent and need no lane.
            return self._runner
        if self.workspace is None:
            raise MilhouseError("no herdr workspace has been opened yet")
        source = self.workspace.workspace_id
        base = self.branch or "HEAD"
        lane = (
            self.lanes.open_for(
                self.lane_key, source_workspace=source, base=base, focus=self.attach
            )
            if self.lane_key
            else self.lanes.open(issue, source_workspace=source, base=base, focus=self.attach)
        )
        self._opened[issue.id] = lane
        self.report(f"  lane {lane.workspace_id} on {lane.branch} ({lane.path})")
        self._active = AgentRunner(
            self.client,
            self.config,
            run_dir=self.config.run_dir(),
            pane_id=lane.pane_id,
            agent_name=lane.agent_name,
            workdir=lane.path,
        )
        return self._active

    def reaper_for(self, lane: Lane) -> Runner:
        """A runner bound to the pane a dispatched agent is already in.

        Not :meth:`runner_for`, which picks a *free* pane and would therefore
        skip the one holding the running agent — and send the exit keys
        somewhere else.

        Args:
            lane: The lane the turn was dispatched to.

        Returns:
            A runner over that lane's live agent.
        """
        if self._runner is not None:
            return self._runner
        agent_name = lane.agent_name
        self._active = AgentRunner(
            self.client,
            self.config,
            run_dir=self.config.run_dir(),
            pane_id=self.client.agent_pane(agent_name) or lane.pane_id,
            agent_name=agent_name,
            workdir=lane.path,
        )
        return self._active

    def lane_of(self, runner: Runner, issue: Issue) -> Lane:
        """The lane ``runner`` is working ``issue`` in.

        Normally the one :meth:`runner_for` just opened. A runner the tests
        inject has no lane, so one is described from where it says it works.
        """
        opened = self._opened.get(issue.id)
        if opened is not None:
            return opened
        return Lane(
            key=self.lane_key or issue.id,
            path=runner.workdir,
            branch=self.repo.at(runner.workdir).current_branch() or "",
            workspace_id="",
            pane_id=runner.pane_id,
        )

    def exit_agent(self) -> None:
        """Best-effort return of the last lane's pane to a shell prompt."""
        runner = self._runner or self._active
        if runner is None:
            return
        try:
            runner.exit_agent()
        except MilhouseError as exc:
            log.warning("could not exit the agent: %s", exc)

    # -- the work ---------------------------------------------------------

    def claim(self) -> Issue | None:
        """Claim the next ready issue, take its lane's lock, and record the claim.

        The audit entry is what a later run reads to tell a claim milhouse
        abandoned from one a person made by hand, so it is written before the
        turn starts rather than after it.

        Returns:
            The claimed issue, or ``None`` when nothing is ready.
        """
        issue = self.tracker.ready(claim=True)
        if issue is None:
            return None
        self.lock_for(issue.id)
        self.in_flight.append(issue.id)
        self.audit.claimed(issue.id)
        return self._full(issue)

    def hand_off(self, issue_id: str) -> None:
        """Stop treating ``issue_id`` as this process's to release.

        A dispatched turn outlives the process that started it. Teardown must
        not re-open its claim, because an agent is working it and
        ``milhouse reap`` is what settles it.
        """
        if issue_id in self.in_flight:
            self.in_flight.remove(issue_id)

    def _full(self, issue: Issue) -> Issue:
        """Re-read a claimed issue, since ``bd ready`` does not carry relations.

        Lane assignment needs :attr:`~milhouse.models.Issue.blocked_by`, which
        only ``bd show`` returns. A tracker that will not answer is not worth
        failing the turn over: the issue is claimed either way, and the worst
        case is a lane of its own rather than a tab in its predecessor's.
        """
        try:
            return self.tracker.get(issue.id)
        except MilhouseError as exc:
            log.warning("could not re-read %s after claiming it: %s", issue.id, exc)
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

    def settle(self, issue_id: str, decision: Decision) -> None:
        """Apply a policy decision to one issue's turn.

        Args:
            issue_id: The issue the turn worked.
            decision: What :func:`milhouse.policy.decide` returned.
        """
        if decision.issue == "release":
            self.release_claim(issue_id, decision.note)
        elif decision.issue == "defer":
            self.defer_claim(issue_id, decision.reason, note=decision.note)
        else:
            self.hand_off(issue_id)

    def release_claim(self, issue_id: str, note: str | None = None) -> None:
        """Return an issue to the open pool, tolerating a bd hiccup."""
        try:
            self.tracker.release(issue_id, note=note)
        except MilhouseError as exc:
            log.warning("could not re-open %s: %s", issue_id, exc)
        self.hand_off(issue_id)

    def defer_claim(self, issue_id: str, reason: str, *, note: str | None = None) -> None:
        """Set an issue aside, having first returned it to the open pool.

        The release is not redundant. A deferred issue that is still
        ``in_progress`` and still assigned reads as work somebody is doing, and
        whoever picks it back up would have to undo two things rather than one.

        A ``bd`` that will not take either is logged rather than raised, for the
        same reason :meth:`release_claim` tolerates one: the turn already
        happened and cannot be re-run.
        """
        self.release_claim(issue_id, note)
        try:
            self.tracker.defer(issue_id, reason=reason)
        except MilhouseError as exc:
            log.warning("could not defer %s: %s", issue_id, exc)

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
