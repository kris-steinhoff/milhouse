"""The ralph loop, and the guardrails that keep it from running away.

:class:`RalphLoop` is the thing PLAN.md describes: claim one ready issue, give it
to a fresh agent, classify what happened, repeat until nothing is ready. The
loop, not the agent, decides what is worked on and when to stop
(:doc:`ADR 0005 <../../docs/decisions/0005-milhouse-owns-the-loop>`).

Everything unattended loops get wrong lives here: the iteration ceiling, the
per-issue attempt cap, stall detection, the blocked-agent policy, crash
reconciliation, and clean teardown on a signal.
"""

from __future__ import annotations

import contextlib
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from . import outcome as outcome_module
from . import prompts
from .config import Config
from .errors import AgentError, HerdrError, LoopAbortedError, MilhouseError, UserAbortError
from .gitrepo import GitRepo
from .herdr import AgentStatus, HerdrClient, Workspace
from .models import Issue, Iteration, RunState, TaskDefinition, now
from .planner import Planner
from .runner import AgentRunner
from .tracker.base import Tracker

__all__ = ["LoopResult", "RalphLoop"]

log = logging.getLogger(__name__)

Reporter = Callable[[str], None]
"""Where the loop's progress lines go. The CLI passes ``typer.echo``."""


@dataclass
class LoopResult:
    """How a run ended.

    Attributes:
        reason: Why the loop stopped, in one line.
        completed: Whether the epic was worked to exhaustion.
        iterations: How many iterations ran.
        state: The final run state, already persisted.
    """

    reason: str
    completed: bool
    iterations: int
    state: RunState


@dataclass
class _Session:
    """The mutable pieces of one run, so teardown can reach them.

    Attributes:
        workspace: The workspace being driven, once one exists.
        runner: The agent runner, once one exists.
        interrupted: Set by the signal handler; checked between iterations.
    """

    workspace: Workspace | None = None
    runner: AgentRunner | None = None
    interrupted: bool = False
    previous_handlers: dict[int, Any] = field(default_factory=dict)


class RalphLoop:
    """Drives one task's issues to completion, one fresh agent at a time."""

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
    ) -> None:
        """Assemble a loop from its collaborators.

        Args:
            config: Resolved configuration.
            task: The task to work.
            tracker: Where issues live.
            client: The herdr client.
            repo: The git repository being worked in.
            report: Called with each human-readable progress line.
            attach: Focus the workspace when creating it, so a human watches it
                happen. Off by default, so an unattended run does not steal the
                screen.
        """
        self.config = config
        self.task = task
        self.tracker = tracker
        self.client = client
        self.repo = repo
        self.report = report
        self.attach = attach
        self.run_dir = config.run_dir(task.slug)
        self.state_path = self.run_dir / "state.json"
        self._session = _Session()

    # -- entry points ----------------------------------------------------

    def state(self) -> RunState:
        """Load this task's run state, creating a fresh one on the first run.

        Raises:
            MilhouseError: The state file belongs to a different task, which
                happens when two task definitions share a filename and therefore
                a slug.
        """
        loaded = RunState.load(self.state_path)
        if loaded is None:
            return RunState(task_id=self.task.task_id, task_slug=self.task.slug)
        if loaded.task_id != self.task.task_id:
            raise MilhouseError(
                f"{self.state_path} belongs to {loaded.task_id}, not {self.task.task_id}. "
                "Two task definitions share a slug; rename one."
            )
        return loaded

    def run(self, *, confirm: Callable[[Any], bool] | None = None) -> LoopResult:
        """Reconcile, decompose if needed, then loop until the epic is finished.

        Args:
            confirm: Passed to the planner for decomposition approval. ``None``
                creates without asking, which is what ``--yes`` does.

        Returns:
            How the run ended.

        Raises:
            LoopAbortedError: A guardrail stopped the run early.
            UserAbortError: The run was interrupted, or approval was declined.
        """
        state = self.state()
        self.reconcile(state)
        self._prepare_branch(state)
        state.save(self.state_path)

        with self._signals():
            self._open_workspace(state)
            epic = self._ensure_epic(state, confirm=confirm)
            return self._iterate(state, epic)

    def reconcile(self, state: RunState) -> None:
        """Undo whatever a previous run left half-done.

        A run killed with ``SIGKILL`` leaves an issue claimed forever, because
        ``bd`` has no lease expiry. Re-running is the recovery mechanism
        (:doc:`ADR 0008 <../../docs/decisions/0008-crash-recovery-by-reconciliation>`),
        and it only ever touches a claim milhouse itself recorded.

        Args:
            state: The run state, mutated in place.
        """
        if not state.claimed_issue:
            return
        issue_id = state.claimed_issue
        self.report(f"reconciling: re-opening {issue_id}, claimed by an interrupted run")
        try:
            self.tracker.release(
                issue_id, note="Re-opened by milhouse: the previous run did not finish."
            )
        except MilhouseError as exc:
            log.warning("could not re-open %s: %s", issue_id, exc)
        state.claimed_issue = None

    # -- setup -----------------------------------------------------------

    def _prepare_branch(self, state: RunState) -> None:
        """Put the run on its branch, or leave the repo where it is.

        Raises:
            MilhouseError: The working tree is dirty, so a checkout would risk
                someone's uncommitted work.
        """
        if self.config.git.branch_strategy == "current":
            state.branch = self.repo.current_branch()
            return
        branch = state.branch or f"{self.config.git.branch_prefix}{self.task.slug}"
        if self.repo.current_branch() != branch and self.repo.is_dirty():
            raise MilhouseError(
                "the working tree has uncommitted changes; commit or stash them before "
                f"milhouse checks out {branch}"
            )
        state.branch = self.repo.ensure_branch(branch)
        self.report(f"working on branch {state.branch}")

    def _open_workspace(self, state: RunState) -> None:
        """Reuse the configured or recorded workspace, or create one.

        A workspace recorded in ``state.json`` may have been closed by a human
        since, so its existence is checked rather than assumed.
        """
        configured = self.config.herdr.workspace
        existing = configured or state.workspace_id
        if existing and self.client.workspace_exists(existing):
            pane = state.pane_id if state.workspace_id == existing else None
            workspace = Workspace(
                workspace_id=existing,
                pane_id=pane or self.client.first_pane(existing),
            )
            state.owns_workspace = state.owns_workspace and configured is None
        else:
            workspace = self.client.create_workspace(
                self.config.repo_root,
                f"milhouse:{self.task.slug}",
                focus=self.attach,
            )
            state.owns_workspace = True
            self.report(f"created herdr workspace {workspace.workspace_id} ({workspace.label})")

        state.workspace_id = workspace.workspace_id
        state.pane_id = workspace.pane_id
        self._session.workspace = workspace
        self._session.runner = AgentRunner(
            self.client,
            self.config,
            run_dir=self.run_dir,
            pane_id=workspace.pane_id,
            agent_name=f"milhouse-{self.task.slug}",
        )

    def _ensure_epic(self, state: RunState, *, confirm: Callable[[Any], bool] | None) -> Issue:
        """Find the epic for this task, planning one if it does not exist yet."""
        existing = self.tracker.find_epic(self.task)
        if existing is not None:
            state.epic_id = existing.id
            state.save(self.state_path)
            self.report(f"using existing epic {existing.id}: {existing.title}")
            return existing

        self.report("no decomposition found; running the planning agent")
        planner = Planner(self.config, self.tracker, self._runner(), run_dir=self.run_dir)
        epic, children = planner.plan(self.task, confirm=confirm)
        state.epic_id = epic.id
        state.save(self.state_path)
        self.report(f"created epic {epic.id} with {len(children)} issues")
        return epic

    # -- the loop --------------------------------------------------------

    def _iterate(self, state: RunState, epic: Issue) -> LoopResult:
        """Claim, work, and classify issues until something stops the loop."""
        limit = self.config.loop.max_iterations
        count = len(state.iterations)
        while True:
            if self._session.interrupted:
                raise UserAbortError("interrupted; the in-flight claim was reverted")
            if count >= limit:
                return self._stop(state, f"reached the {limit}-iteration ceiling", completed=False)

            issue = self.tracker.ready(epic.id, claim=True)
            if issue is None:
                return self._stop(state, self._nothing_ready(epic), completed=self._is_done(epic))

            state.claimed_issue = issue.id
            state.save(self.state_path)
            count += 1

            iteration = self._work(state, issue, number=count)
            state.record(iteration)
            state.claimed_issue = None
            state.save(self.state_path)

            stop = self._after(state, issue, iteration)
            if stop is not None:
                return stop

    def _work(self, state: RunState, issue: Issue, *, number: int) -> Iteration:
        """Run one iteration against one issue and classify the result."""
        attempt = state.attempts_for(issue.id) + 1
        attempts_left = max(self.config.loop.max_attempts - state.attempts_for(issue.id), 0)
        self.report(
            f"iteration {number}: {issue.id} {issue.title} "
            f"(attempt {attempt} of {self.config.loop.max_attempts})"
        )

        prompt = prompts.render_iterate(
            self.task,
            issue,
            branch=state.branch,
            attempt=attempt,
            attempts_left=attempts_left,
            previous=self._history(state, issue.id),
        )
        head_before = self.repo.head()
        started = now()

        runner = self._runner()
        try:
            turn = runner.run_turn(prompt, iteration=number)
        except (AgentError, HerdrError) as exc:
            turn = None
            error: str | None = str(exc)
        else:
            error = turn.error
        state.pane_id = runner.pane_id

        if turn is not None and turn.agent_state == "blocked":
            turn.agent_state = self._handle_blocked(runner)

        head_after = self.repo.head()
        issue_after = self._reread(issue)
        verdict = outcome_module.classify(
            issue_after=issue_after,
            head_before=head_before,
            head_after=head_after,
            agent_state=turn.agent_state if turn else "unknown",
            timed_out=bool(turn and turn.timed_out),
            error=error,
        )
        self.report(f"  → {verdict.outcome}: {verdict.detail}")

        return Iteration(
            number=number,
            issue_id=issue.id,
            issue_title=issue.title,
            outcome=verdict.outcome,
            detail=verdict.detail,
            agent_state=turn.agent_state if turn else None,
            head_before=head_before,
            head_after=head_after,
            started_at=started,
            ended_at=now(),
            prompt_path=self._relative(turn.prompt_path if turn else None),
            transcript_path=self._relative(turn.transcript_path if turn else None),
        )

    def _handle_blocked(self, runner: AgentRunner) -> AgentStatus:
        """Apply the ``--on-blocked`` policy to an agent waiting on a human."""
        policy = self.config.loop.on_blocked
        workspace = self._session.workspace
        where = workspace.workspace_id if workspace else "the workspace"
        if policy == "abort":
            self.report(f"  agent is blocked; attach to {where} and re-run")
            raise LoopAbortedError(f"the agent is waiting on a human in workspace {where}")
        if policy == "skip":
            self.report("  agent is blocked; skipping this issue")
            return "blocked"
        minutes = self.config.loop.blocked_timeout_ms // 60_000
        self.report(
            f"  agent is blocked and waiting for you. Attach with: "
            f"herdr agent attach {runner.agent_name}  (waiting up to {minutes}m)"
        )
        return runner.wait_for_unblock()

    def _after(self, state: RunState, issue: Issue, iteration: Iteration) -> LoopResult | None:
        """Decide what happens to the issue now the iteration is over.

        A claimed issue is ``in_progress`` in beads, and ``bd ready`` deliberately
        excludes those. So an unfinished issue that is simply left alone would
        never be offered again, and the loop would report the epic finished with
        the work undone. Every non-success path therefore has to say explicitly
        what the issue's status becomes:

        - retry left → back to ``open``, so the next claim can pick it up
        - attempts exhausted → ``blocked``, with a note, and the loop moves on
        - the agent was waiting on a human → ``blocked``, without burning an
          attempt, because nothing failed

        Returns:
            A result if the run should stop, or ``None`` to keep going.
        """
        if iteration.outcome == "success":
            return None

        if iteration.outcome == "blocked":
            self._set_blocked(
                issue,
                f"The agent stopped waiting on a human during iteration {iteration.number}. "
                "Attach to the milhouse workspace, unblock it, then re-run milhouse.",
                why="needs a human",
            )
            return None

        attempts = state.attempts_for(issue.id)
        if attempts < self.config.loop.max_attempts:
            try:
                self.tracker.release(issue.id)
            except MilhouseError as exc:
                log.warning("could not re-open %s for another attempt: %s", issue.id, exc)
            return None

        self._set_blocked(
            issue,
            f"milhouse gave up after {attempts} attempts. "
            f"Last outcome: {iteration.outcome} — {iteration.detail}",
            why=f"failed {attempts} times",
        )
        return None

    def _set_blocked(self, issue: Issue, note: str, *, why: str) -> None:
        """Mark an issue blocked so ``bd ready`` stops offering it, and say why."""
        self.report(f"  {issue.id} {why}; marking it blocked and moving on")
        try:
            self.tracker.block(issue.id, note)
        except MilhouseError as exc:
            log.warning("could not mark %s blocked: %s", issue.id, exc)

    def _unfinished(self, epic: Issue) -> list[Issue]:
        """Children of ``epic`` that are not closed."""
        return [child for child in self.tracker.children(epic.id) if not child.is_closed]

    def _is_done(self, epic: Issue) -> bool:
        """Whether the epic is genuinely finished, rather than merely stuck."""
        return not self._unfinished(epic)

    def _nothing_ready(self, epic: Issue) -> str:
        """Explain an empty ready queue.

        ``bd ready`` returns nothing both when every issue is closed and when
        everything left is blocked, or depends on something blocked. Those are
        opposite outcomes, and reporting the second as "the epic is finished"
        exits 0 on a run that did nothing — which is how a dogfood run whose
        issues all blocked on a permission prompt reported success.
        """
        unfinished = self._unfinished(epic)
        if not unfinished:
            return "no issues are ready; the epic is finished"
        blocked = [issue for issue in unfinished if issue.status == "blocked"]
        detail = ", ".join(issue.id for issue in unfinished)
        if blocked:
            return (
                f"nothing is ready but {len(unfinished)} issue(s) are unfinished "
                f"({detail}); {len(blocked)} blocked and needing a human"
            )
        return f"nothing is ready but {len(unfinished)} issue(s) are unfinished ({detail})"

    def _stop(self, state: RunState, reason: str, *, completed: bool) -> LoopResult:
        """Finish the run cleanly, leaving the workspace open for inspection."""
        state.save(self.state_path)
        self._exit_agent()
        self.report(reason)
        if self._session.workspace:
            self.report(f"the herdr workspace {self._session.workspace.workspace_id} is still open")
        return LoopResult(
            reason=reason,
            completed=completed,
            iterations=len(state.iterations),
            state=state,
        )

    # -- plumbing --------------------------------------------------------

    def _runner(self) -> AgentRunner:
        """The session's agent runner.

        Raises:
            MilhouseError: Called before a workspace was opened, which is a bug.
        """
        if self._session.runner is None:
            raise MilhouseError("no herdr workspace has been opened yet")
        return self._session.runner

    def _reread(self, issue: Issue) -> Issue:
        """Read an issue back after a turn, tolerating a tracker hiccup."""
        try:
            return self.tracker.get(issue.id)
        except MilhouseError as exc:
            log.warning("could not re-read %s: %s", issue.id, exc)
            return issue

    def _history(self, state: RunState, issue_id: str) -> list[dict[str, str]]:
        """Earlier attempts at ``issue_id``, for the retry section of the prompt."""
        return [
            {"outcome": item.outcome, "detail": item.detail}
            for item in state.iterations
            if item.issue_id == issue_id
        ]

    def _relative(self, path: Path | None) -> str | None:
        """Render an artifact path relative to the repo root, for the state file."""
        if path is None:
            return None
        try:
            return str(path.relative_to(self.config.repo_root))
        except ValueError:
            return str(path)

    def _exit_agent(self) -> None:
        """Best-effort return of the pane to a shell prompt."""
        if self._session.runner is None:
            return
        try:
            self._session.runner.exit_agent()
        except MilhouseError as exc:
            log.warning("could not exit the agent: %s", exc)

    # -- teardown --------------------------------------------------------

    class _Signals:
        """Context manager installing the loop's SIGINT/SIGTERM handling."""

        def __init__(self, loop: RalphLoop) -> None:
            """Bind to the loop whose teardown this manages."""
            self.loop = loop

        def __enter__(self) -> RalphLoop._Signals:
            """Install handlers, remembering whatever was there before."""
            for number in (signal.SIGINT, signal.SIGTERM):
                # ValueError means we are not on the main thread, where signal
                # handlers cannot be installed; the caller keeps its own.
                with contextlib.suppress(ValueError):
                    self.loop._session.previous_handlers[number] = signal.getsignal(number)
                    signal.signal(number, self._handle)
            return self

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            """Restore the previous handlers and tear down on an abort."""
            for number, handler in self.loop._session.previous_handlers.items():
                with contextlib.suppress(ValueError):
                    signal.signal(number, handler)
            if exc_type is not None:
                self.loop._teardown()
            return False

        def _handle(self, signum: int, frame: FrameType | None) -> None:
            """Record the interrupt and let the loop unwind at a safe point."""
            self.loop._session.interrupted = True
            raise UserAbortError("interrupted")

    def _signals(self) -> RalphLoop._Signals:
        """Install SIGINT/SIGTERM handling for the duration of a run."""
        return RalphLoop._Signals(self)

    def _teardown(self) -> None:
        """Revert the in-flight claim and exit the agent, leaving panes open.

        Panes are never closed out from under a human: the workspace stays open
        so whatever went wrong can be looked at
        (:doc:`ADR 0005 <../../docs/decisions/0005-milhouse-owns-the-loop>`).
        """
        state = RunState.load(self.state_path)
        if state and state.claimed_issue:
            self.report(f"reverting the claim on {state.claimed_issue}")
            try:
                self.tracker.release(
                    state.claimed_issue, note="Re-opened by milhouse: the run was interrupted."
                )
            except MilhouseError as exc:
                log.warning("could not revert the claim on %s: %s", state.claimed_issue, exc)
            state.claimed_issue = None
            state.save(self.state_path)
        self._exit_agent()
        if self._session.workspace:
            self.report(f"the herdr workspace {self._session.workspace.workspace_id} is left open")
