"""How many turns happen, and what stops them.

This is the **Repetition** layer, and its constraint is that it may not do
anything else. Nothing below it knows how many turns are coming, which is what
kept removing the previous loop down to one file and is the test to apply to
anything added here: a piece that needs the turn count belongs in this module,
a piece that reads what a turn achieved belongs in :mod:`milhouse.outcome`, and
a piece that decides what to do about it belongs in :mod:`milhouse.policy`.

``milhouse run`` works one target until it is finished or until something stops
it (:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`). The whole
of the stopping is :func:`should_halt`, which is pure, plus the empty queue,
which :func:`milhouse.step.nothing_ready` already explains.

**The loop body is an argument.** Serially it is one :func:`milhouse.step.step`,
which claims an issue, waits for its agent, and settles it. A later ``--count N``
replaces it with dispatch-then-reap, and nothing else in this module changes,
because the loop's question is how many rather than how.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .models import Issue, Iteration, now
from .policy import Policy, decide
from .session import Session
from .step import StepResult, nothing_ready, step

__all__ = ["Body", "Halt", "RunResult", "run", "should_halt"]

StopReason = Literal["finished", "deadlocked", "blocked", "error", "dirty", "ceiling"]
"""Why a run stopped. Only ``finished`` means the target is done."""

Body = Callable[[Session, Policy], StepResult | None]
"""One unit of work, or ``None`` when the queue had nothing to offer."""


@dataclass(frozen=True)
class Halt:
    """Why the loop stopped, and whether that is a good thing.

    Attributes:
        reason: Which row of the table fired.
        detail: The same thing in a sentence, for whoever reads the report.
        finished: Whether the target is actually done. True for exactly one
            reason, because every other way of stopping leaves work behind and
            reporting otherwise is how a run that did nothing exits zero.
    """

    reason: StopReason
    detail: str
    finished: bool = False


@dataclass(frozen=True)
class RunResult:
    """What one run did, as a value the CLI formats and nothing else reads.

    Attributes:
        target: The issue or epic the run was working towards.
        halt: Why it stopped.
        iterations: Every turn it took, in order.
        deferred: Issues it gave up on, as ``(issue_id, reason)``. These are
            still unfinished, so a run with any of them did not finish.
        started_at: When the run began.
        ended_at: When it stopped.
    """

    target: Issue
    halt: Halt
    iterations: list[Iteration] = field(default_factory=list)
    deferred: list[tuple[str, str]] = field(default_factory=list)
    started_at: datetime = field(default_factory=now)
    ended_at: datetime = field(default_factory=now)

    @property
    def finished(self) -> bool:
        """Whether the target is done. What the CLI's exit code is."""
        return self.halt.finished

    @property
    def elapsed(self) -> float:
        """Seconds the run took."""
        return (self.ended_at - self.started_at).total_seconds()

    def closed(self) -> list[Iteration]:
        """The turns that finished an issue."""
        return [item for item in self.iterations if item.outcome == "success"]


def should_halt(iteration: Iteration, *, used: int, max_iterations: int) -> Halt | None:
    """Whether the run stops now that ``iteration`` is over.

    Pure, so the table below is a unit test rather than a scenario. The order is
    the decision: a blocked agent is reported as blocked even on the last
    permitted turn, because the ceiling is not why the run is over.

    ==================================  ===========================================
    Condition                           Halt
    ==================================  ===========================================
    outcome ``blocked``                 ``blocked``, nobody is there to approve
    outcome ``error``                   ``error``, milhouse failed rather than the
                                        agent
    ``success`` that left a dirty tree  ``dirty``, the next turn would inherit it
    ``used`` reached ``max_iterations`` ``ceiling``
    anything else                       none, keep going
    ==================================  ===========================================

    A **failed** turn that left the tree dirty does not stop the run. The policy
    already says so on the issue, the turn is going to be retried anyway, and
    stopping there would let one untidy agent end a fifty-issue run. A
    *successful* one is different: the issue is closed, so nothing will revisit
    those changes, and they may be the work the close was claiming to have done
    (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).

    Args:
        iteration: The turn that just ended, already classified.
        used: How many turns the run has taken, this one included.
        max_iterations: The ceiling it is allowed.

    Returns:
        Why to stop, or ``None`` to keep going.
    """
    if iteration.outcome == "blocked":
        return Halt(
            "blocked",
            f"the agent stopped waiting on a human during {iteration.issue_id}, and "
            "an unattended run has nobody to approve it",
        )
    if iteration.outcome == "error":
        return Halt("error", f"milhouse itself failed: {iteration.detail}")
    if iteration.outcome == "success" and iteration.dirty_after:
        return Halt(
            "dirty",
            f"{iteration.issue_id} was closed but left uncommitted changes, which the "
            "next iteration in this lane would inherit",
        )
    if used >= max_iterations:
        return Halt("ceiling", f"the run hit its ceiling of {max_iterations} iteration(s)")
    return None


def run(
    session: Session,
    target: Issue,
    *,
    policy: Policy = decide,
    max_iterations: int = 50,
    body: Body = lambda session, policy: step(session, policy=policy),
) -> RunResult:
    """Work ``target`` until it is finished or something stops the run.

    Args:
        session: An open session, whose tracker is already fenced to the target
            (:mod:`milhouse.scope`). This function never asks what is in scope,
            which is why it needs no idea how the target was resolved.
        target: What the run is working towards, for the report.
        policy: What settles each issue afterwards. ``milhouse run`` passes
            :func:`milhouse.policy.unattended`, which caps attempts.
        max_iterations: Turns the run may take. At least one turn always
            happens: the ceiling is checked once a turn is over, so setting it
            to zero does not make a run that does nothing.
        body: One unit of work. The default is one :func:`milhouse.step.step`.

    Returns:
        What the run did and why it stopped.
    """
    started_at = now()
    iterations: list[Iteration] = []
    deferred: list[tuple[str, str]] = []
    used = 0

    def finish(halt: Halt) -> RunResult:
        session.report(f"stopping: {halt.detail}")
        return RunResult(
            target=target,
            halt=halt,
            iterations=iterations,
            deferred=deferred,
            started_at=started_at,
            ended_at=now(),
        )

    while True:
        result = body(session, policy)
        if result is None:
            return finish(_empty_queue(session, deferred))
        used += 1
        iterations.append(result.iteration)
        if result.decision.issue == "defer":
            deferred.append((result.iteration.issue_id, result.decision.reason))
        halt = should_halt(result.iteration, used=used, max_iterations=max_iterations)
        if halt is not None:
            return finish(halt)


def _empty_queue(session: Session, deferred: list[tuple[str, str]]) -> Halt:
    """Why an empty ready queue stopped the run, and whether it is a good ending.

    ``bd ready`` returns nothing both when everything is closed and when
    everything left is stuck, which are opposite outcomes
    (:func:`milhouse.step.nothing_ready`). A run that gave up on issues itself
    is the third case, and it looks exactly like the second from the queue's
    side, so it is named rather than left to be inferred from `bd blocked`.
    """
    detail, finished = nothing_ready(session)
    if finished:
        return Halt("finished", detail, finished=True)
    if deferred:
        detail += f", and this run deferred {len(deferred)} of them"
    return Halt("deadlocked", detail)
