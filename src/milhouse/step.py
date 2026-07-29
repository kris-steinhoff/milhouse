"""One iteration, in one piece or in two.

An iteration is: claim an issue, hand it to a fresh agent in its own lane, read
back what changed, classify it, and decide what becomes of the issue. The middle
part is the only one that costs money, and the last two are pure functions over
what it produced.

**The turn has a seam in it**, because waiting is what stops several running at
once (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`):

- :func:`dispatch` claims up to N ready issues, sets up their lanes, starts their
  agents, and returns. It waits for nothing.
- :func:`reap` finds the turns that have settled since and finishes them.
- :func:`step` is dispatch-one-and-wait: the whole turn in one call, which is
  what ``milhouse step`` has always done and still does.

Splitting the primitive is not a repetition policy. Nothing here decides whether
there is another turn, so :doc:`ADR 0017 <../../docs/decisions/0017-no-loop-until-it-is-earned>`
still holds, and the seam a loop would swap is still the ``policy`` argument
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import outcome as outcome_module
from . import prompts
from .errors import AgentError, HerdrError, MilhouseError
from .lanes import Lane
from .models import Issue, Iteration, now
from .policy import Decision, Policy, decide
from .runner import Runner, TurnResult
from .session import Session
from .verify import Verification, verify

__all__ = ["Dispatched", "StepResult", "dispatch", "nothing_ready", "reap", "step"]


@dataclass(frozen=True)
class StepResult:
    """One iteration and what was decided about it.

    Attributes:
        iteration: What happened, already recorded in the audit log.
        decision: What was done about the issue, already applied.
    """

    iteration: Iteration
    decision: Decision


@dataclass(frozen=True)
class Dispatched:
    """A turn that has been started and not yet reaped.

    Everything here is written to the audit log when the agent starts, because
    the process that reaps the turn may not be the one that dispatched it.

    Attributes:
        issue: The issue being worked.
        lane: Where its agent is running.
        number: The iteration number, which names the turn's artifacts.
        attempt: Which attempt at this issue this turn is. Carried rather than
            recomputed at reap time, because by then this turn's own iteration
            entry may already be in the history being counted.
        head_before: The lane's ``HEAD`` before the agent started.
        prompt_path: Where the rendered prompt was saved, repo-relative.
        started_at: When the agent was prompted, which is what the turn timeout
            is measured from.
    """

    issue: Issue
    lane: Lane
    number: int
    attempt: int
    head_before: str | None
    prompt_path: str | None
    started_at: datetime

    def as_entry(self) -> dict[str, Any]:
        """The audit entry a later :func:`reap` rebuilds this from."""
        return {
            "number": self.number,
            "attempt": self.attempt,
            "head_before": self.head_before,
            "prompt_path": self.prompt_path,
            "started_at": self.started_at.isoformat(),
            "lane_branch": self.lane.branch,
            "lane_path": str(self.lane.path),
        }


def step(session: Session, *, policy: Policy = decide) -> StepResult | None:
    """Work the next ready issue and wait for it, or report that none is ready.

    Args:
        session: An open session.
        policy: What decides the aftermath. Injectable so a different policy is
            a different function rather than different plumbing.

    Returns:
        The iteration and the decision, or ``None`` when nothing was ready.
    """
    issue = session.claim()
    if issue is None:
        return None

    runner, prompt, pending = _prepare(session, issue)
    turn: TurnResult | None
    try:
        turn = runner.run_turn(prompt, iteration=pending.number, issue_id=issue.id)
    except (AgentError, HerdrError) as exc:
        turn = None
        error: str | None = str(exc)
    else:
        error = turn.error

    result = _finish(session, pending, runner, turn, error=error, policy=policy)
    session.report(f"  → {result.iteration.outcome}: {result.iteration.detail}")
    return result


def dispatch(session: Session, *, limit: int = 1) -> list[Dispatched]:
    """Claim up to ``limit`` ready issues, start their agents, and return.

    Nothing is waited on, so the turns run concurrently in their own lanes.
    ``bd ready --claim`` is atomic, so two dispatchers cannot take the same
    issue, and each holds only its own lane's lock.

    A turn that could not be started is settled immediately rather than left
    claimed: an agent that never ran will never be reaped.

    Args:
        session: An open session.
        limit: How many issues to take at most. Fewer if the queue runs dry.

    Returns:
        The turns now in flight, in the order they were started.
    """
    started: list[Dispatched] = []
    for _ in range(max(limit, 0)):
        issue = session.claim()
        if issue is None:
            break
        runner, prompt, pending = _prepare(session, issue)
        try:
            turn: TurnResult | None = runner.start_turn(
                prompt, iteration=pending.number, issue_id=issue.id
            )
        except (AgentError, HerdrError) as exc:
            turn = None
            error: str | None = str(exc)
        else:
            error = turn.error
        if error:
            session.report(f"  → could not start: {error}")
            _finish(session, pending, runner, turn, error=error, policy=decide)
            continue
        pending = replace(pending, prompt_path=session.relative(turn.prompt_path if turn else None))
        session.audit.dispatched(issue.id, pending.as_entry())
        session.hand_off(issue.id)
        session.report(f"  → dispatched to {pending.lane.workspace_id}")
        started.append(pending)
    return started


def reap(session: Session, *, policy: Policy = decide) -> list[StepResult]:
    """Finish every dispatched turn whose agent has settled.

    A turn still working is left alone, and reaping again later picks it up. A
    turn past the configured turn timeout is collected anyway and classified
    ``timeout``, which is what ``milhouse step`` does with the same case.

    Args:
        session: An open session.
        policy: What decides the aftermath of each turn.

    Returns:
        One result per turn that was finished, oldest dispatch first.
    """
    results = []
    for issue_id, entry in session.audit.dispatches().items():
        pending = _rebuild(session, issue_id, entry)
        if pending is None:
            continue
        session.lock_for(issue_id)
        runner = session.reaper_for(pending.lane)
        timed_out = _overdue(session, pending)
        if runner.settled() is None and not timed_out:
            session.report(f"{pending.issue.id} is still working")
            continue
        session.report(f"reaping iteration {pending.number}: {pending.issue.id}")
        turn = runner.finish_turn(pending.number, issue_id=issue_id)
        turn.timed_out = timed_out
        result = _finish(session, pending, runner, turn, error=None, policy=policy)
        session.report(f"  → {result.iteration.outcome}: {result.iteration.detail}")
        results.append(result)
    return results


def nothing_ready(session: Session) -> tuple[str, bool]:
    """Explain an empty ready queue, and say whether the work is actually done.

    ``bd ready`` returns nothing both when every issue is closed and when
    everything left is stuck behind something. Those are opposite outcomes, and
    reporting the second as "finished" exits 0 having done nothing — which is
    how a dogfood run whose issues all blocked on a permission prompt reported
    success.

    Args:
        session: The open session.

    Returns:
        A line for the human, and whether the work is genuinely finished.
    """
    unfinished = session.unfinished()
    if not unfinished:
        return "no issues are ready; everything in scope is closed", True
    detail = ", ".join(issue.id for issue in unfinished)
    return (
        f"nothing is ready but {len(unfinished)} issue(s) are unfinished ({detail}); "
        "`bd blocked` says what is stuck",
        False,
    )


# -- the two halves of a turn --------------------------------------------------


def _prepare(session: Session, issue: Issue) -> tuple[Runner, str, Dispatched]:
    """Open the issue's lane, render its prompt, and read where ``HEAD`` is.

    Everything a turn needs before an agent exists, and nothing that depends on
    how it will be waited for.
    """
    number = session.next_number()
    previous = session.history_for(issue.id)
    attempt = len(previous) + 1
    suffix = f" (attempt {attempt})" if attempt > 1 else ""
    session.report(f"iteration {number}: {issue.id} {issue.title}{suffix}")

    runner = session.runner_for(issue)
    lane = session.lane_of(runner, issue)
    prompt = prompts.render_iterate(
        issue,
        background=session.background(issue),
        branch=lane.branch,
        attempt=attempt,
        previous=[{"outcome": item.outcome, "detail": item.detail} for item in previous],
    )
    # Read git where the turn will actually happen — the lane's worktree, not
    # the repository root. Anywhere else would credit this issue with somebody
    # else's commits (ADR 0020).
    head_before = session.repo.at(lane.path).head()
    return (
        runner,
        prompt,
        Dispatched(
            issue=issue,
            lane=lane,
            number=number,
            attempt=attempt,
            head_before=head_before,
            prompt_path=None,
            started_at=now(),
        ),
    )


def _finish(
    session: Session,
    pending: Dispatched,
    runner: Runner,
    turn: TurnResult | None,
    *,
    error: str | None,
    policy: Policy,
) -> StepResult:
    """Classify a turn that is over, record it, and settle its issue."""
    issue = pending.issue
    repo = session.repo.at(runner.workdir)
    head_after = repo.head()
    commits = repo.commits_between(pending.head_before, head_after)
    attributed = bool(repo.commits_between(pending.head_before, head_after, grep=issue.id))
    dirty_after = repo.is_dirty()
    try:
        issue_after = session.tracker.get(issue.id)
    except MilhouseError as exc:
        # Do not let a tracker failure read as "the issue is still open", which
        # would be classified as a work outcome the agent never caused.
        issue_after = issue
        error = error or f"could not re-read {issue.id} after the turn: {exc}"

    checked = _verify(session, issue_after, error=error, cwd=runner.workdir)
    verdict = outcome_module.classify(
        issue_after=issue_after,
        commits=commits,
        attributed=attributed,
        agent_state=turn.agent_state if turn else "unknown",
        timed_out=bool(turn and turn.timed_out),
        error=error,
        verification=checked,
    )

    iteration = Iteration(
        number=pending.number,
        attempt=pending.attempt,
        issue_id=issue.id,
        issue_title=issue.title,
        outcome=verdict.outcome,
        detail=verdict.detail,
        agent_state=turn.agent_state if turn else None,
        head_before=pending.head_before,
        head_after=head_after,
        commits=commits,
        attributed=attributed,
        dirty_after=dirty_after,
        verified=checked.ok if checked else None,
        verification_output=checked.output if checked and not checked.ok else "",
        started_at=pending.started_at,
        ended_at=now(),
        prompt_path=session.relative(turn.prompt_path if turn else None) or pending.prompt_path,
        transcript_path=session.relative(turn.transcript_path if turn else None),
    )
    session.record(iteration)
    decision = policy(iteration)
    session.settle(issue.id, decision)
    return StepResult(iteration=iteration, decision=decision)


def _rebuild(session: Session, issue_id: str, entry: dict[str, Any]) -> Dispatched | None:
    """Rebuild a dispatched turn from its audit entry, or skip it.

    The lane comes from herdr rather than from the entry: the entry says which
    lane the agent was started in, and herdr says whether it is still there.
    """
    located = session.lanes.locate(issue_id)
    if located is None:
        session.report(f"{issue_id} was dispatched but its lane is gone; leaving it to reconcile")
        return None
    lane, _ = located
    try:
        issue = session.tracker.get(issue_id)
    except MilhouseError as exc:
        session.report(f"{issue_id} was dispatched but cannot be read back: {exc}")
        return None
    return Dispatched(
        issue=issue,
        lane=lane,
        number=int(entry.get("number") or session.next_number()),
        # An entry written before attempts were recorded has no key, and the
        # history is the next best answer: this turn has not been recorded yet,
        # so what is there is what came before it.
        attempt=int(entry.get("attempt") or len(session.history_for(issue_id)) + 1),
        head_before=entry.get("head_before"),
        prompt_path=entry.get("prompt_path"),
        started_at=_when(entry.get("started_at")),
    )


def _when(value: Any) -> datetime:
    """Parse a recorded timestamp, falling back to now for an unreadable one."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return now()


def _overdue(session: Session, pending: Dispatched) -> bool:
    """Whether a dispatched turn has outlived the turn timeout.

    ``milhouse step`` gets this from herdr, which is doing the waiting. A
    dispatched turn has nobody waiting on it, so the deadline is measured from
    the time recorded when it started.
    """
    elapsed = (now() - pending.started_at).total_seconds()
    return elapsed > session.config.agent.turn_timeout_ms / 1000


def _verify(
    session: Session, issue_after: Issue, *, error: str | None, cwd: Path
) -> Verification | None:
    """Run the repository's gate, but only on a turn that claims to be finished.

    Running it every iteration would buy the whole test suite to confirm that an
    unfinished issue is unfinished. An iteration that already failed for another
    reason is skipped for the same argument.

    It runs in the lane, not in the primary checkout, because the lane is where
    the work is. That is also where the per-lane bootstrap problem shows up: a
    fresh worktree has no `.venv` and no `node_modules`, so a gate that assumes
    one fails for environmental reasons rather than real ones
    (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).
    """
    command = session.config.verify.command
    if error or not issue_after.is_closed or not command:
        return None
    session.report(f"  verifying in {cwd}: {' '.join(command)}")
    return verify(session.config, cwd=cwd)
