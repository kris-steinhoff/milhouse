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
which claims an issue, waits for its agent, and settles it. The concurrent one
is :class:`milhouse.parallel.Parallel`, which dispatches several and reaps them,
and nothing else in this module changed to accommodate it, because the loop's
question is how many rather than how.

**A halt stops starting work, not work already started.** A concurrent body has
turns of its own in flight when the table fires, and abandoning them would leave
claimed issues with live agents and branches nobody merges — ``milhouse reap``
collects a turn but does not land it. So a body that has turns to finish is
asked to :meth:`~Draining.drain` them before the run reports, and whatever they
produce joins the report. The first halt is still why the run stopped: nothing
that settles during the drain is put back through :func:`should_halt`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from .models import Issue, Iteration, now
from .policy import Policy, decide
from .session import Session
from .step import StepResult, merge_line, nothing_ready, step

__all__ = ["Body", "Draining", "Halt", "RunResult", "run", "should_halt"]

StopReason = Literal[
    "finished", "deadlocked", "blocked", "error", "dirty", "conflict", "integration", "ceiling"
]
"""Why a run stopped. Only ``finished`` means the target is done."""

Body = Callable[[Session, Policy], StepResult | None]
"""One unit of work, or ``None`` when the queue had nothing to offer."""


@runtime_checkable
class Draining(Protocol):
    """A loop body with turns of its own that a halt must not abandon.

    :func:`milhouse.step.step` is not one: it waits for its agent, so when it
    returns there is nothing left running and a halt can report immediately.
    :class:`milhouse.parallel.Parallel` is, because it keeps up to N turns in
    flight and only one of them is the turn that halted the run.

    Structural rather than declared, so the loop keeps taking any callable as a
    body and gains a second question it may ask one.
    """

    @property
    def in_flight(self) -> list[str]:
        """Issues this body started and has not handed back."""

    def drain(self, session: Session, policy: Policy) -> list[StepResult]:
        """Start nothing more, and finish everything already started."""


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
        still_running: Issues whose turns the run started and never collected,
            even after draining. Empty for a serial run, which cannot start a
            turn it does not wait for. A concurrent one that stopped with two
            agents still working has to say so, rather than printing numbers
            that look complete.
        started_at: When the run began.
        ended_at: When it stopped.
    """

    target: Issue
    halt: Halt
    iterations: list[Iteration] = field(default_factory=list)
    deferred: list[tuple[str, str]] = field(default_factory=list)
    still_running: list[str] = field(default_factory=list)
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

    def merged(self) -> list[Iteration]:
        """The turns whose branch is now on the integration branch, in merge order.

        Empty for a serial run, which works in the integration lane itself and
        so has nothing to land in it
        (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
        """
        return [item for item in self.iterations if item.merge is not None and item.merge.landed]

    def unmerged(self) -> list[Iteration]:
        """The turns that closed an issue and could not be landed.

        Usually one, since the first of them halts the run, but a drain can
        produce more: the turns already in flight are finished and merged, and
        their merges can fail too. Each is a closed issue whose work is on a
        branch only a person can land, which is the one mess a serial run could
        not leave.
        """
        return [
            item for item in self.iterations if item.merge is not None and not item.merge.landed
        ]


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
    a merge that did not land           ``conflict``, only a person can land it
    a red integration gate              ``integration``, two green branches are
                                        red together
    ``used`` reached ``max_iterations`` ``ceiling``
    anything else                       none, keep going
    ==================================  ===========================================

    A **failed** turn that left the tree dirty does not stop the run. The policy
    already says so on the issue, the turn is going to be retried anyway, and
    stopping there would let one untidy agent end a fifty-issue run. A
    *successful* one is different: the issue is closed, so nothing will revisit
    those changes, and they may be the work the close was claiming to have done
    (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).

    A merge that did not land is a **halt rather than a deferral**, because the
    work is done, the issue is closed, and nothing an agent could be asked next
    would change that. Only a person can land the branch
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

    That row is one row and not two. ``MergeRecord.landed`` is false both for a
    conflict and for a merge git refused outright, and those are different
    causes with the same consequence: a closed issue, a live worker branch, an
    integration branch without the work, and a recovery that is entirely by
    hand. Splitting them would double the table without doubling the response,
    so the reason is one word and the detail is the one that says which happened
    — which is also why the detail is :func:`milhouse.step.merge_line`, the same
    sentence the run printed when the merge failed.

    A **red integration gate** is the row after it, and it is a halt for a
    different reason: nothing is broken about the merge, the branch is simply
    red now that two histories are on it. The run stops and undoes nothing. The
    merge stays, the issue stays closed, and the failing output is already on the
    issue as a note by the time this is asked
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
    ``integration_verified`` is a tri-state, and only ``False`` fires: ``None``
    is the gate not having run, which is every turn in a run with no gate
    configured and every merge that fast-forwarded.

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
    if iteration.merge is not None and not iteration.merge.landed:
        return Halt(
            "conflict",
            f"{iteration.issue_id} is closed but its work is not on "
            f"{iteration.merge.target}: {merge_line(iteration.merge)}",
        )
    if iteration.integration_verified is False:
        target = iteration.merge.target if iteration.merge else "the integration branch"
        return Halt(
            "integration",
            f"the gate failed on {target} once {iteration.issue_id} was merged into it; "
            "the merge stands, the issue stays closed, and the output is on the issue",
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

    def take(result: StepResult) -> None:
        iterations.append(result.iteration)
        if result.decision.issue == "defer":
            deferred.append((result.iteration.issue_id, result.decision.reason))

    def finish(halt: Halt) -> RunResult:
        session.report(f"stopping: {halt.detail}")
        for result in _drain(body, session, policy):
            take(result)
        return RunResult(
            target=target,
            halt=halt,
            iterations=iterations,
            deferred=deferred,
            still_running=_still_running(body),
            started_at=started_at,
            ended_at=now(),
        )

    while True:
        result = body(session, policy)
        if result is None:
            return finish(_empty_queue(session, deferred))
        used += 1
        take(result)
        halt = should_halt(result.iteration, used=used, max_iterations=max_iterations)
        if halt is not None:
            return finish(halt)


def _drain(body: Body, session: Session, policy: Policy) -> list[StepResult]:
    """Finish the turns the body already started, and start no more.

    A serial body has none, so this is nothing at all for ``milhouse run``
    without ``--count``. A concurrent one has up to N-1 turns whose agents are
    still working when the halt fires, and dropping them would leave claimed
    issues with live agents and successful branches nobody merges — a later
    ``milhouse reap`` collects a turn but does not land it.

    The drain is bounded by the turn timeout, which
    :func:`milhouse.step.reap` already enforces: a turn past it is collected and
    classified ``timeout`` rather than waited on forever.

    Whatever settles here is reported, and none of it is put back through
    :func:`should_halt`. A second reason arriving during the drain does not
    change the outcome, because the run is already stopping and the first reason
    is why.

    A body with nothing in flight is still asked, because turns that settled
    together are handed back one per call: when the first of them halts the run,
    the rest have already been reaped and merged, and losing them would report a
    merge the branch really has as a merge nobody made.
    """
    if not isinstance(body, Draining):
        return []
    if body.in_flight:
        session.report(
            f"draining {len(body.in_flight)} turn(s) already in flight; "
            "they are not abandoned, and a successful one is still merged"
        )
    return body.drain(session, policy)


def _still_running(body: Body) -> list[str]:
    """Issues whose turns the run started and could not collect, after draining.

    Normally empty: the drain waits for everything it started. What survives it
    is a turn whose lane herdr no longer has, which no amount of polling will
    settle and whose agent may well still be running somewhere.
    """
    return list(body.in_flight) if isinstance(body, Draining) else []


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
