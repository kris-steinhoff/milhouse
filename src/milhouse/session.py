"""Everything a run needs that is not the loop.

:class:`Session` owns the resources and the state: the run lock, the branch, the
herdr workspace and pane, the agent runner, and the claim currently in flight. It
is a context manager, so opening and tearing all of that down is one ``with``.

It holds **no policy**. It does not decide what to work on next or when to
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

from .config import Config
from .errors import MilhouseError, UserAbortError
from .gitrepo import GitRepo
from .herdr import HerdrClient, Workspace
from .models import Issue, Iteration, RunState
from .policy import Decision
from .runner import AgentRunner, Runner
from .state import RunStore
from .tracker.base import Tracker

__all__ = ["Reporter", "Session"]

log = logging.getLogger(__name__)

Reporter = Callable[[str], None]
"""Where progress lines go. The CLI passes ``typer.echo``."""


class Session:
    """One repository's resources and state, for as long as milhouse is working it."""

    def __init__(
        self,
        config: Config,
        *,
        tracker: Tracker,
        client: HerdrClient,
        repo: GitRepo,
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
        self.report = report
        self.attach = attach
        self.store = RunStore(config.run_dir())
        self.state = self._load()
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
        stale = self.store.lock.acquire()
        if stale is not None:
            self.report(f"took over the run lock from a dead run ({stale.describe()})")
        try:
            self.reconcile()
            self.state.branch = self.repo.current_branch()
            self.open_workspace()
            self.save()
        except BaseException:
            self.store.lock.release()
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
            if self.state.claimed_issue:
                self.release_claim("Re-opened by milhouse: the run did not finish this issue.")
            self.exit_agent()
        finally:
            self.store.lock.release()
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

    def _load(self) -> RunState:
        """Read this repository's state, creating a fresh one on the first run."""
        return self.store.load() or RunState()

    def save(self) -> None:
        """Persist the session state."""
        self.store.save(self.state)

    def reconcile(self) -> None:
        """Undo whatever a previous run left half-done.

        A run killed with ``SIGKILL`` leaves an issue claimed forever, because
        ``bd`` has no lease expiry. Re-running is the recovery mechanism
        (:doc:`ADR 0008 <../../docs/decisions/0008-crash-recovery-by-reconciliation>`),
        and holding the run lock first is what makes it safe: the claim being
        re-opened cannot belong to a run that is still working it
        (:doc:`ADR 0015 <../../docs/decisions/0015-one-run-at-a-time>`).
        """
        if not self.state.claimed_issue:
            return
        self.report(
            f"reconciling: re-opening {self.state.claimed_issue}, "
            "claimed by a run that did not finish"
        )
        self.release_claim("Re-opened by milhouse: the previous run did not finish.")

    # -- resources --------------------------------------------------------

    def open_workspace(self) -> None:
        """Reuse the configured or recorded workspace, or create one.

        A workspace recorded in ``state.json`` may have been closed by a human
        since, so its existence is checked rather than assumed.

        A reused workspace is not an empty one. Its panes may be running agents,
        including the one milhouse was typed into, so the pane to work in is
        chosen rather than taken.
        """
        configured = self.config.herdr.workspace
        existing = configured or self.state.workspace_id
        if existing and self.client.workspace_exists(existing):
            pane = self.state.pane_id if self.state.workspace_id == existing else None
            workspace = Workspace(
                workspace_id=existing,
                pane_id=pane
                or self.client.pane_to_work_in(
                    existing,
                    self.config.repo_root,
                    avoid=self.config.herdr.self_pane,
                ),
            )
            self.state.owns_workspace = self.state.owns_workspace and configured is None
        else:
            workspace = self.client.create_workspace(
                self.config.repo_root,
                f"milhouse:{self.config.repo_root.name}",
                focus=self.attach,
            )
            self.state.owns_workspace = True
            self.report(f"created herdr workspace {workspace.workspace_id} ({workspace.label})")

        self.state.workspace_id = workspace.workspace_id
        self.state.pane_id = workspace.pane_id
        self.workspace = workspace
        if self._runner is None:
            self._runner = AgentRunner(
                self.client,
                self.config,
                run_dir=self.store.run_dir,
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
        """Claim the next ready issue, recording it as in flight.

        Returns:
            The claimed issue, or ``None`` when nothing is ready.
        """
        issue = self.tracker.ready(claim=True)
        if issue is None:
            return None
        self.state.claimed_issue = issue.id
        self.save()
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
            self.state.claimed_issue = None
            self.save()

    def release_claim(self, note: str | None = None) -> None:
        """Return the in-flight issue to the open pool, tolerating a bd hiccup."""
        issue_id = self.state.claimed_issue
        if issue_id is None:
            return
        try:
            self.tracker.release(issue_id, note=note)
        except MilhouseError as exc:
            log.warning("could not re-open %s: %s", issue_id, exc)
        self.state.claimed_issue = None
        self.save()

    def record(self, iteration: Iteration) -> None:
        """Append an iteration to the run's event log."""
        self.store.append(iteration)

    def history_for(self, issue_id: str) -> list[Iteration]:
        """Earlier iterations that worked ``issue_id``, oldest first."""
        return self.store.history_for(issue_id)

    def next_number(self) -> int:
        """The number the next iteration gets."""
        return self.store.next_number()

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
        """Render an artifact path relative to the repo root, for the event log."""
        if path is None:
            return None
        try:
            return str(path.relative_to(self.config.repo_root))
        except ValueError:
            return str(path)
