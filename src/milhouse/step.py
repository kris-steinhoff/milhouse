"""One iteration: claim an issue, work it once, classify, decide.

:func:`step` is the unit milhouse is built from, and ``milhouse step`` calls it
once and hands back to a human. Nothing calls it in a loop yet, on purpose
(:doc:`ADR 0017 <../../docs/decisions/0017-no-loop-until-it-is-earned>`), and the
seam for one is already here: the ``policy`` argument is what a loop would swap
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).

The steps, in order:

1. Claim the next ready issue from the tracker.
2. Render the iteration prompt, including whatever earlier attempts left behind.
3. Start a fresh agent, prompt it once, capture the transcript, exit it.
4. Read back what changed in beads and git.
5. Classify the turn (:mod:`milhouse.outcome`) and record it.
6. Decide what happens to the issue (:mod:`milhouse.policy`) and apply it.

Step 3 is the only one that costs money, and steps 5 and 6 are pure functions
over what it produced.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import outcome as outcome_module
from . import prompts
from .errors import AgentError, HerdrError, MilhouseError
from .models import Issue, Iteration, now
from .policy import Decision, decide
from .runner import TurnResult
from .session import Session
from .verify import Verification, verify

__all__ = ["StepResult", "nothing_ready", "step"]

Policy = Callable[[Iteration], Decision]
"""What decides the aftermath of an iteration. Defaults to the supervised rule."""


@dataclass(frozen=True)
class StepResult:
    """One iteration and what was decided about it.

    Attributes:
        iteration: What happened, already appended to the event log.
        decision: What was done about the issue, already applied.
    """

    iteration: Iteration
    decision: Decision


def step(session: Session, *, policy: Policy = decide) -> StepResult | None:
    """Work the next ready issue, or report that none is ready.

    Args:
        session: An open session, holding the lock, branch, and workspace.
        policy: What decides the aftermath. Injectable so a different policy is
            a different function rather than different plumbing.

    Returns:
        The iteration and the decision, or ``None`` when nothing was ready.
    """
    issue = session.claim()
    if issue is None:
        return None

    iteration = _work(session, issue)
    session.record(iteration)
    session.report(f"  → {iteration.outcome}: {iteration.detail}")

    decision = policy(iteration)
    session.settle(decision)
    return StepResult(iteration=iteration, decision=decision)


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


def _work(session: Session, issue: Issue) -> Iteration:
    """Run one turn against ``issue`` and classify what it achieved."""
    number = session.next_number()
    previous = session.history_for(issue.id)
    attempt = len(previous) + 1
    suffix = f" (attempt {attempt})" if attempt > 1 else ""
    session.report(f"iteration {number}: {issue.id} {issue.title}{suffix}")

    runner = session.runner_for(issue)
    prompt = prompts.render_iterate(
        issue,
        background=session.background(issue),
        branch=session.repo.at(runner.workdir).current_branch(),
        attempt=attempt,
        previous=[{"outcome": item.outcome, "detail": item.detail} for item in previous],
    )
    # Read git where the turn will actually happen — the lane's worktree, not
    # the repository root. Anywhere else would credit this issue with somebody
    # else's commits (ADR 0020).
    repo = session.repo.at(runner.workdir)
    head_before = repo.head()
    started = now()

    turn: TurnResult | None
    try:
        turn = runner.run_turn(prompt, iteration=number, issue_id=issue.id)
    except (AgentError, HerdrError) as exc:
        turn = None
        error: str | None = str(exc)
    else:
        error = turn.error

    head_after = repo.head()
    commits = repo.commits_between(head_before, head_after)
    attributed = bool(repo.commits_between(head_before, head_after, grep=issue.id))
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

    return Iteration(
        number=number,
        issue_id=issue.id,
        issue_title=issue.title,
        outcome=verdict.outcome,
        detail=verdict.detail,
        agent_state=turn.agent_state if turn else None,
        head_before=head_before,
        head_after=head_after,
        commits=commits,
        attributed=attributed,
        dirty_after=dirty_after,
        verified=checked.ok if checked else None,
        verification_output=checked.output if checked and not checked.ok else "",
        started_at=started,
        ended_at=now(),
        prompt_path=session.relative(turn.prompt_path if turn else None),
        transcript_path=session.relative(turn.transcript_path if turn else None),
    )


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
