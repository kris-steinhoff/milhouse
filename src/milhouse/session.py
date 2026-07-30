"""Everything a run needs that is not the loop.

:class:`Session` owns the resources: the lane locks, the branch, the herdr source
workspace, the agent runners, and whatever claims are in flight. It is a context
manager, so opening and tearing all of that down is one ``with``.

The lock is **per lane**, not per repository. Several turns running at once is
the point of lanes, so the thing being protected is one lane, not the run
(:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`). A run
therefore holds one lock per lane it has open: its integration lane's, and one
per worker lane if it has any
(:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

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

__all__ = ["Reporter", "Session", "usable_workspace"]

log = logging.getLogger(__name__)

Reporter = Callable[[str], None]
"""Where progress lines go. The CLI passes ``typer.echo``."""


def usable_workspace(
    client: HerdrClient, workspace_id: str | None, repo_root: Path
) -> tuple[str | None, str | None]:
    """Whether a named workspace can be this repository's source workspace.

    herdr resolves which repository a lane comes from by looking at its source
    workspace, so one belonging to somewhere else silently branches, works, and
    commits to the wrong repository. That is reachable by accident: herdr
    exports ``HERDR_WORKSPACE_ID`` into every pane it launches and milhouse
    reads it, which is right when stepping the repository you are sitting in and
    wrong the moment ``--repo`` points elsewhere.

    A workspace herdr reports no repository for is accepted. herdr allows a
    workspace with no worktree, and refusing one on a fact that was never
    knowable would break the ordinary case to guard the odd one.

    Module-level rather than a method so ``milhouse status`` reaches the same
    verdict a run would. A status that names a workspace the run would ignore is
    worse than no status line at all.

    Args:
        client: The herdr client.
        workspace_id: The configured or ambient workspace, or ``None``.
        repo_root: The repository milhouse was pointed at.

    Returns:
        The workspace to use, or ``None``; and a line explaining a refusal, or
        ``None`` when there was nothing to refuse.
    """
    if not workspace_id or not client.workspace_exists(workspace_id):
        return None, None
    root = client.workspace_repo(workspace_id)
    if root is None or root == repo_root:
        return workspace_id, None
    return None, (
        f"ignoring herdr workspace {workspace_id}: it is a checkout of {root}, not {repo_root}"
    )


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
        worker_lanes: bool = False,
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
            lane_key: The label of this run's **integration** lane, which is its
                target. What ``milhouse run`` passes, so a whole target lands on
                one reviewable branch
                (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).
                ``None`` gives each issue a lane and a lock of its own, which is
                what ``step``, ``dispatch``, and ``reap`` want.
            worker_lanes: Give each issue a lane of its own, branched from the
                integration branch, instead of working them all in the
                integration lane
                (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
                Ignored without a ``lane_key``, since a worker lane is defined by
                the integration branch it comes from. This is a **mode, not a
                count**: a session never learns how many turns are coming, so a
                run at ``--count 1`` simply leaves it off and gets ADR 0023
                exactly.
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
        self.worker_lanes = worker_lanes
        self.lanes = Lanes(client, config)
        self.branch: str | None = None
        self.workspace: Workspace | None = None
        self.in_flight: list[str] = []
        """Issues this process claimed and has neither settled nor handed off."""

        self._locks: dict[str, RunLock] = {}
        self._integration: Lane | None = None
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

        Every lane this session opened is deliberately left open, whether or not
        the work failed, so the panes can be inspected
        (:doc:`ADR 0005 <../../docs/decisions/0005-milhouse-owns-the-loop>`).
        Each one is named on the way out, with its checkout, because that is the
        directory somebody goes and looks at. The outcome does not change the
        line, since nothing is closed either way: after a failure it says where to
        debug, and after a success where the branch under review was written.

        The **source** workspace is not mentioned. It was open before the session
        started, no agent ever runs in it
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`),
        and it is the checkout milhouse was typed into, so naming it points a
        person at where they already are. A session that opened no lane therefore
        says nothing, rather than claiming something was left behind.
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
        for lane in self._lanes_opened():
            self.report(f"lane {lane.workspace_id} is left open ({lane.path})")
        return False

    def _lanes_opened(self) -> list[Lane]:
        """The distinct lanes this session opened, in the order it opened them.

        :attr:`_opened` is keyed by issue, and several issues can share one lane:
        a run works all of them in the lane named after its target
        (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`), and a
        stacked issue gets a tab in its blocker's lane. What teardown is telling
        somebody is where to look, so a checkout is named once however many turns
        happened in it, and one line per lane keeps the shape the same whether
        there is one or several.

        The integration lane comes first, and comes first even in a run that
        opened worker lanes and never worked an issue in it, because it is the
        branch a person reviews
        (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
        """
        every = list(self._opened.values())
        if self._integration is not None:
            every.insert(0, self._integration)
        distinct: dict[tuple[str, Path], Lane] = {}
        for lane in every:
            distinct.setdefault((lane.workspace_id, lane.path), lane)
        return list(distinct.values())

    # -- the per-lane lock ------------------------------------------------

    def lock_for(self, issue_id: str) -> RunLock:
        """Take the lock on one issue's lane, and hold it for this session.

        The lock is per lane rather than per repository, because concurrent lanes
        are the point
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).
        Two dispatchers cannot collide over *which* issue to take — ``bd ready
        --claim`` is atomic — so all this stops is two processes working the
        same lane, which is the case that would drive one pane from two places.

        A serial run works every issue in one lane, so it takes one lock, on the
        target. Taking a lock per issue would let a second run of the same
        target start the moment the first one moved on
        (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).

        A run with worker lanes is the other way round again: the lane an issue
        is worked in is its own, so the lock is the issue's, and the target's
        lock is held separately by :meth:`integration_lane`
        (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
        Keying it by the issue exactly as ``dispatch`` does is deliberate: the
        two are the same lane key in different lanes, so nothing else can be
        working that issue while this run is.

        Args:
            issue_id: The issue whose lane is being worked. Ignored when this
                session works every issue in one lane, which names it instead.

        Returns:
            The held lock.

        Raises:
            RunLockedError: A live process already holds this lane.
        """
        return self._lock(self._lane_key_for(issue_id))

    def _lane_key_for(self, issue_id: str) -> str:
        """Which lane ``issue_id``'s turn happens in, by label."""
        if self.lane_key is not None and not self.worker_lanes:
            return self.lane_key
        return issue_id

    def _lock(self, key: str) -> RunLock:
        """Take the lock on the lane labelled ``key``, and hold it for this session."""
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

        A run with worker lanes reconciles better than a serial one, because a
        worker lane is labelled with the issue rather than with the target, so
        the lane registry can answer the second question about it at all
        (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
        A serial run's claim is always re-opened, which is what ADR 0023 wanted
        and still gets.
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
        """The label milhouse gives this repository's workspace.

        Unconstrained: herdr stores a label verbatim
        (:meth:`~milhouse.herdr.HerdrClient.create_workspace`), so the colon costs
        nothing and a directory name goes in whatever shape it has. What it does
        have to be is derived the same way twice, because
        :meth:`~milhouse.herdr.HerdrClient.find_workspace` matches it exactly and
        that is how a later run rejoins this workspace instead of opening a second
        one.
        """
        return f"milhouse:{self.config.repo_root.name}"

    def open_workspace(self) -> None:
        """Reuse the configured workspace, find milhouse's own, or create one.

        This is the **source** workspace: the primary checkout, which is how
        herdr knows which repository a lane's worktree comes from. No agent runs
        in it — every turn happens in a lane
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

        milhouse writes no workspace id down. It asks the tool that owns them:
        the configured id if there is one **and it is a checkout of this
        repository**, otherwise the open workspace carrying this repository's
        label, otherwise a fresh one.

        The repository check is not defensive tidiness. herdr resolves which
        repository a lane comes from by looking at its source workspace, so a
        source workspace belonging to somewhere else silently branches, works,
        and commits to the wrong repository. That is reachable by accident:
        herdr exports ``HERDR_WORKSPACE_ID`` into every pane it launches and
        milhouse reads it, which is right when stepping the repository you are
        sitting in and wrong the moment ``--repo`` points elsewhere.

        A mismatch is reported and ignored rather than refused. There is a
        correct workspace to fall back to, and an unattended run that carries on
        in the right repository beats one that stops.
        """
        configured = self._usable(self.config.herdr.workspace)
        existing = configured or self.client.find_workspace(self.workspace_label)
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

    def _usable(self, workspace_id: str | None) -> str | None:
        """``workspace_id`` if :func:`usable_workspace` accepts it, reporting if not."""
        usable, refusal = usable_workspace(self.client, workspace_id, self.config.repo_root)
        if refusal:
            self.report(refusal)
        return usable

    def integration_lane(self) -> Lane | None:
        """This run's integration lane, opened and locked the first time it is asked for.

        The one branch a person reviews, labelled with the target
        (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`). Every
        worker lane branches from it, and a merge back into it is what lands one
        (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

        Opening it is deferred rather than done in :meth:`__enter__`, so a run
        that finds nothing ready creates no worktree and takes no lock, which is
        what ``step``, ``dispatch``, and ``reap`` already do.

        Returns:
            The lane, or ``None`` for a session with no :attr:`lane_key` — which
            is ``step``, ``dispatch``, and ``reap``, and is how a caller asks
            whether there is an integration branch at all.

        Raises:
            MilhouseError: The workspace has not been opened yet.
        """
        if self.lane_key is None:
            return None
        if self._integration is None:
            if self.workspace is None:
                raise MilhouseError("no herdr workspace has been opened yet")
            self._lock(self.lane_key)
            self._integration = self.lanes.open_for(
                self.lane_key,
                source_workspace=self.workspace.workspace_id,
                base=self.branch or "HEAD",
                focus=self.attach,
            )
            if self.worker_lanes:
                # Without worker lanes this is the lane every turn happens in,
                # and `runner_for` names it there instead of naming it twice.
                lane = self._integration
                self.report(
                    f"  integration lane {lane.workspace_id} on {lane.branch} ({lane.path})"
                )
        return self._integration

    def runner_for(self, issue: Issue) -> Runner:
        """Open ``issue``'s lane and return a runner bound to it.

        Each issue gets its own lane, its own branch, and its own agent name, so
        two turns can be in flight without either seeing the other's pane or
        commits. A session with a :attr:`lane_key` works them all in that one
        lane instead
        (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`), unless
        it also has :attr:`worker_lanes`, in which case each issue gets a lane
        branched from the integration branch as it stands right now
        (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

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
        integration = self.integration_lane()
        if integration is None:
            lane = self.lanes.open(
                issue, source_workspace=source, base=self.branch or "HEAD", focus=self.attach
            )
        elif not self.worker_lanes:
            lane = integration
        else:
            lane = self.lanes.open_worker(
                issue.id,
                target=integration.key,
                source_workspace=source,
                base=integration.branch,
                focus=self.attach,
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
            key=self._lane_key_for(issue.id),
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
