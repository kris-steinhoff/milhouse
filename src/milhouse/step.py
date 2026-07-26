"""One iteration: claim an issue, work it once, classify, decide.

:func:`step` is the unit milhouse is built from. ``milhouse step`` calls it once
and hands back to a human. ``milhouse run`` calls it in a loop. Nothing else
differs between them, which is the point
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).

The steps, in order:

1. Claim one ready issue from the tracker.
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

from . import outcome as outcome_module
from . import prompts
from .errors import AgentError, HerdrError, MilhouseError
from .models import Issue, Iteration, now
from .policy import Decision, decide
from .runner import TurnResult
from .session import Session

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


def step(session: Session, epic: Issue, *, policy: Policy = decide) -> StepResult | None:
    """Work one ready issue under ``epic``, or report that none is ready.

    Args:
        session: An open session, holding the lock, branch, and workspace.
        epic: The epic whose children are worked.
        policy: What decides the aftermath. Injectable so a different policy is
            a different function rather than a different loop.

    Returns:
        The iteration and the decision, or ``None`` when nothing was ready.
    """
    issue = session.claim(epic)
    if issue is None:
        return None

    iteration = _work(session, issue)
    session.record(iteration)
    session.report(f"  → {iteration.outcome}: {iteration.detail}")

    decision = policy(iteration)
    session.settle(decision)
    return StepResult(iteration=iteration, decision=decision)


def nothing_ready(session: Session, epic: Issue) -> tuple[str, bool]:
    """Explain an empty ready queue, and say whether the epic is actually done.

    ``bd ready`` returns nothing both when every issue is closed and when
    everything left is stuck behind something. Those are opposite outcomes, and
    reporting the second as "the epic is finished" exits 0 on a run that did
    nothing — which is how a dogfood run whose issues all blocked on a permission
    prompt reported success.

    Args:
        session: The open session.
        epic: The epic being worked.

    Returns:
        A line for the human, and whether the epic is genuinely finished.
    """
    unfinished = session.unfinished(epic)
    if not unfinished:
        return "no issues are ready; the epic is finished", True
    detail = ", ".join(issue.id for issue in unfinished)
    return (
        f"nothing is ready but {len(unfinished)} issue(s) are unfinished ({detail})",
        False,
    )


def _work(session: Session, issue: Issue) -> Iteration:
    """Run one turn against ``issue`` and classify what it achieved."""
    number = session.next_number()
    previous = session.history_for(issue.id)
    attempt = len(previous) + 1
    suffix = f" (attempt {attempt})" if attempt > 1 else ""
    session.report(f"iteration {number}: {issue.id} {issue.title}{suffix}")

    prompt = prompts.render_iterate(
        session.task,
        issue,
        branch=session.state.branch,
        attempt=attempt,
        previous=[{"outcome": item.outcome, "detail": item.detail} for item in previous],
    )
    head_before = session.repo.head()
    started = now()

    runner = session.runner
    turn: TurnResult | None
    try:
        turn = runner.run_turn(prompt, iteration=number)
    except (AgentError, HerdrError) as exc:
        turn = None
        error: str | None = str(exc)
    else:
        error = turn.error
    session.state.pane_id = runner.pane_id

    head_after = session.repo.head()
    try:
        issue_after = session.tracker.get(issue.id)
    except MilhouseError as exc:
        # Do not let a tracker failure read as "the issue is still open", which
        # would be classified as a work outcome the agent never caused.
        issue_after = issue
        error = error or f"could not re-read {issue.id} after the turn: {exc}"

    verdict = outcome_module.classify(
        issue_after=issue_after,
        head_before=head_before,
        head_after=head_after,
        agent_state=turn.agent_state if turn else "unknown",
        timed_out=bool(turn and turn.timed_out),
        error=error,
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
        started_at=started,
        ended_at=now(),
        prompt_path=session.relative(turn.prompt_path if turn else None),
        transcript_path=session.relative(turn.transcript_path if turn else None),
    )
