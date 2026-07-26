"""Everything a run needs that is not the loop.

:class:`Session` owns the resources and the state: the run lock, the branch, the
herdr workspace and pane, the agent runner, the epic, and the claim currently in
flight. It is a context manager, so opening and tearing all of that down is one
``with``.

It holds **no policy**. It does not decide what to work on next, when to retry,
or when a run is over. That separation is what lets one supervised iteration
(``milhouse step``) and a loop over many (``milhouse run``) be the same code with
a different caller
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

from .config import Config
from .errors import MilhouseError
from .gitrepo import GitRepo
from .herdr import HerdrClient, Workspace
from .models import Issue, Iteration, RunState, TaskDefinition
from .planner import Planner
from .policy import Decision
from .runner import AgentRunner, Runner
from .state import RunStore
from .tracker.base import Tracker

__all__ = ["Reporter", "Session"]

log = logging.getLogger(__name__)

Reporter = Callable[[str], None]
"""Where progress lines go. The CLI passes ``typer.echo``."""


class Session:
    """One task's resources and state, for as long as milhouse is working it."""

    def __init__(
        self,
        config: Config,
        task: TaskDefinition,
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
            task: The task being worked.
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
        self.task = task
        self.tracker = tracker
        self.client = client
        self.repo = repo
        self.report = report
        self.attach = attach
        self.store = RunStore(config.run_dir(task.slug))
        self.state = self._load()
        self.workspace: Workspace | None = None
        self._runner: Runner | None = runner

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> Session:
        """Take the lock, reconcile, prepare the branch, and open the workspace.

        Raises:
            RunLockedError: Another milhouse process is working this task.
            MilhouseError: The branch could not be prepared.
        """
        stale = self.store.lock.acquire()
        if stale is not None:
            self.report(f"took over the run lock from a dead run ({stale.describe()})")
        try:
            self.reconcile()
            self._prepare_branch()
            self.open_workspace()
            self.save()
        except BaseException:
            self.store.lock.release()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Release any in-flight claim, exit the agent, and drop the lock.

        The workspace is deliberately left open, whether or not the run failed,
        so the panes can be inspected
        (:doc:`ADR 0005 <../../docs/decisions/0005-milhouse-owns-the-loop>`).
        """
        try:
            if self.state.claimed_issue:
                self.release_claim("Re-opened by milhouse: the run did not finish this issue.")
            self.exit_agent()
        finally:
            self.store.lock.release()
        if self.workspace is not None:
            self.report(f"the herdr workspace {self.workspace.workspace_id} is left open")
        return False

    def _load(self) -> RunState:
        """Read this task's state, creating a fresh one on the first run.

        Raises:
            MilhouseError: The state file belongs to a different task, which
                happens when two task definitions share a filename and therefore
                a slug.
        """
        loaded = self.store.load()
        if loaded is None:
            return RunState(task_id=self.task.task_id, task_slug=self.task.slug)
        if loaded.task_id != self.task.task_id:
            raise MilhouseError(
                f"{self.store.state_path} belongs to {loaded.task_id}, not {self.task.task_id}. "
                "Two task definitions share a slug; rename one."
            )
        return loaded

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

    def _prepare_branch(self) -> None:
        """Put the run on its branch, or leave the repo where it is.

        Raises:
            MilhouseError: The working tree is dirty, so a checkout would risk
                someone's uncommitted work.
        """
        if self.config.git.branch_strategy == "current":
            self.state.branch = self.repo.current_branch()
            return
        branch = self.state.branch or f"{self.config.git.branch_prefix}{self.task.slug}"
        if self.repo.current_branch() != branch and self.repo.is_dirty():
            raise MilhouseError(
                "the working tree has uncommitted changes; commit or stash them before "
                f"milhouse checks out {branch}"
            )
        self.state.branch = self.repo.ensure_branch(branch)
        self.report(f"working on branch {self.state.branch}")

    def open_workspace(self) -> None:
        """Reuse the configured or recorded workspace, or create one.

        A workspace recorded in ``state.json`` may have been closed by a human
        since, so its existence is checked rather than assumed.
        """
        configured = self.config.herdr.workspace
        existing = configured or self.state.workspace_id
        if existing and self.client.workspace_exists(existing):
            pane = self.state.pane_id if self.state.workspace_id == existing else None
            workspace = Workspace(
                workspace_id=existing,
                pane_id=pane or self.client.first_pane(existing),
            )
            self.state.owns_workspace = self.state.owns_workspace and configured is None
        else:
            workspace = self.client.create_workspace(
                self.config.repo_root,
                f"milhouse:{self.task.slug}",
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
                agent_name=f"milhouse-{self.task.slug}",
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

    def ensure_epic(self, *, confirm: Callable[[Any], bool] | None = None) -> Issue:
        """Find the epic for this task, planning one if it does not exist yet.

        Args:
            confirm: Passed to the planner for decomposition approval. ``None``
                creates without asking, which is what ``--yes`` does.

        Returns:
            The epic this task's issues hang under.
        """
        existing = self.tracker.find_epic(self.task)
        if existing is not None:
            self.state.epic_id = existing.id
            self.save()
            self.report(f"using existing epic {existing.id}: {existing.title}")
            return existing

        self.report("no decomposition found; running the planning agent")
        planner = Planner(self.config, self.tracker, self.runner, run_dir=self.store.run_dir)
        epic, children = planner.plan(self.task, confirm=confirm)
        self.state.epic_id = epic.id
        self.save()
        self.report(f"created epic {epic.id} with {len(children)} issues")
        return epic

    def claim(self, epic: Issue) -> Issue | None:
        """Claim the next ready issue under ``epic``, recording it as in flight.

        Returns:
            The claimed issue, or ``None`` when nothing is ready.
        """
        issue = self.tracker.ready(epic.id, claim=True)
        if issue is None:
            return None
        self.state.claimed_issue = issue.id
        self.save()
        return issue

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

    def unfinished(self, epic: Issue) -> list[Issue]:
        """Children of ``epic`` that are not closed."""
        return [child for child in self.tracker.children(epic.id) if not child.is_closed]

    def relative(self, path: Path | None) -> str | None:
        """Render an artifact path relative to the repo root, for the event log."""
        if path is None:
            return None
        try:
            return str(path.relative_to(self.config.repo_root))
        except ValueError:
            return str(path)
