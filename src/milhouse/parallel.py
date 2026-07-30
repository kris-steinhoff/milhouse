"""Several turns at once, as a loop body :func:`milhouse.run.run` can be handed.

The loop takes its body as an argument
(:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`), so working N
issues at a time is a different body rather than a different loop. This one
dispatches up to N, polls the lanes it started, and hands ``run()`` exactly one
finished turn per call. Each turn is still one issue, one fresh agent, one lane
(:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

It is a **stateful callable** rather than a function, and that is what keeps
``run()`` unchanged. The turns that have settled but not yet been reported live
here, so :func:`milhouse.run.should_halt` stays a pure function over one
finished iteration and the counters stay in ``run()``.

**It decides nothing.** :func:`milhouse.step.reap` has already classified each
turn and applied the policy by the time a result reaches this module, and
whether the run stops is ``run()``'s question. So nothing here reads an outcome
or a decision, and this module imports neither :mod:`milhouse.outcome` nor
:mod:`milhouse.policy` — which is also why the policy it carries through to
``reap`` is typed as :data:`Settle` rather than named.

**Claiming N issues in a row is still safe.**
``BeadsTracker._ready_among``, which a closure-scoped run uses, lists the ready
queue and then claims by id, giving up the atomicity of ``bd ready --claim``,
and its docstring names concurrency as the reason that matters. It still holds
here, because what this module makes concurrent is the *agents*, not the
claiming: :func:`milhouse.step.dispatch` claims one issue at a time in one
process, and each claim has completed before the next queue listing is asked
for. Two milhouse processes working one repository at once is the case that is
still unsafe, and nothing here makes it better or worse.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .models import Iteration, now
from .session import Session
from .step import Dispatched, StepResult, dispatch, reap

__all__ = ["DEFAULT_POLL_MS", "Parallel", "Settle"]

DEFAULT_POLL_MS = 5_000
"""How long to wait between polls of the in-flight lanes, in milliseconds.

Every poll asks herdr about each open lane and re-reads the audit trail, so this
is a subprocess or several per lane per interval, against turns that take
minutes. ``[run] poll_ms`` is the key that will override it
(:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
"""

Settle = Callable[[Iteration], Any]
"""What settles an issue once its turn is over: a :data:`milhouse.policy.Policy`.

Carried rather than named. This module passes the value straight to
:func:`milhouse.step.reap` and never looks inside it, and naming the type would
mean importing the layer this one is not allowed to reach into.
"""


class Parallel:
    """A loop body that keeps up to ``count`` turns in flight at once.

    One call is: top the lanes up, wait for something to settle, and hand back
    one result. Whatever else settled in the same round is kept for the calls
    after it, so ``run()`` still sees one finished iteration at a time.

    :meth:`drain` is the other half of that bargain. Handing back one turn at a
    time means the run halts on one turn while N-1 are still working, so the
    loop needs a way to say "start nothing more, and finish what you started"
    (:class:`milhouse.run.Draining`).
    """

    def __init__(
        self,
        *,
        count: int,
        max_iterations: int,
        poll_ms: int = DEFAULT_POLL_MS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Set the width of the run and how patiently it watches.

        Args:
            count: How many turns may be in flight at once. Below one is read
                as one, since a body that dispatches nothing is not a body.
            max_iterations: The same ceiling ``run()`` was given. Needed here
                too because a turn is spent when it is *started*, and only this
                object knows how many have been started
                (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
            poll_ms: How long to wait between polls of the in-flight lanes.
            sleep: How to wait. Injectable so the tests do not.
        """
        self.count = max(count, 1)
        self.max_iterations = max_iterations
        self.poll_ms = max(poll_ms, 0)
        self._sleep = sleep
        self._flying: dict[str, Dispatched] = {}
        self._lost: list[str] = []
        self._settled: deque[StepResult] = deque()
        self._handed_back = 0
        self._stopped = False

    @property
    def in_flight(self) -> list[str]:
        """Issues this body started and has not handed back, oldest dispatch first.

        A turn given up on by :meth:`_abandon` stays here rather than
        disappearing. milhouse cannot reap it, but its agent may well still be
        running, and the run's report is the only place anybody finds that out
        (:class:`milhouse.run.RunResult`).
        """
        return [*self._flying, *self._lost]

    def __call__(self, session: Session, policy: Settle) -> StepResult | None:
        """Dispatch, poll, reap, and hand back one turn.

        Args:
            session: An open session, whose tracker is already fenced to the
                target.
            policy: What settles each issue afterwards, passed through to
                :func:`milhouse.step.reap` unread.

        Returns:
            One finished turn, or ``None`` when nothing is in flight and the
            queue has nothing left — the same no-work signal
            :func:`milhouse.step.step` gives, which ``run()`` already turns into
            finished or deadlocked.
        """
        self._dispatch(session)
        while self._flying and not self._settled:
            self._collect(session, policy)
            if self._flying and not self._settled:
                self._sleep(self.poll_ms / 1000)
        if not self._settled:
            return None
        self._handed_back += 1
        return self._settled.popleft()

    def drain(self, session: Session, policy: Settle) -> list[StepResult]:
        """Start nothing more, and finish every turn already started.

        What :func:`milhouse.run.run` calls once the halt table has fired. A
        halt means stop starting work, not abandon the agents that are already
        working: their issues are claimed, their branches are unmerged, and
        ``milhouse reap`` would collect them later without landing any of them.

        So this keeps polling and reaping, and hands back everything that
        settles. Nothing new is dispatched, here or in any later call: a drained
        body is done.

        Whether a success is also *merged* is not this object's question and
        never was. :func:`milhouse.step.reap` lands what it reaps, and it stops
        doing so once a merge into the integration branch has not landed
        (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`),
        which is why a drain after a ``conflict`` halt finishes every turn and
        merges none of them without this method knowing anything about it.

        It terminates because :func:`milhouse.step.reap` collects a turn past
        ``[agent] turn_timeout_ms`` rather than waiting on it, and because
        :meth:`_abandon` gives up on one whose lane herdr has lost. That second
        case is what :attr:`in_flight` still names afterwards.

        Args:
            session: An open session, whose tracker is already fenced to the
                target.
            policy: What settles each issue afterwards, passed through unread.

        Returns:
            Every turn that settled, oldest dispatch first, including any that
            had settled before the halt and were not handed back yet.
        """
        self._stopped = True
        while self._flying:
            self._collect(session, policy)
            if self._flying:
                self._sleep(self.poll_ms / 1000)
        collected = list(self._settled)
        self._settled.clear()
        self._handed_back += len(collected)
        return collected

    # -- the three things one call does --------------------------------------

    def _dispatch(self, session: Session) -> None:
        """Top the lanes up to ``count``, without going past the run's ceiling.

        Dispatching happens even when there is already a result waiting to be
        handed back, so a lane that has just been emptied is refilled in the
        same call rather than after the backlog has drained. It stops entirely
        once :meth:`drain` has been called, which is what makes "a halt stops
        starting work" a property of this object rather than a convention its
        caller observes.
        """
        if self._stopped:
            return
        room = min(self.count - len(self._flying), self._allowance())
        if room <= 0:
            return
        for pending in dispatch(session, limit=room):
            self._flying[pending.issue.id] = pending

    def _allowance(self) -> int:
        """Turns the run may still start.

        ``max_iterations`` counts turns the run spent, and a turn is spent when
        it is dispatched rather than when it is reported. Counting only what
        ``run()`` has seen would let a ``count`` of four overshoot the ceiling by
        three.

        A turn given up on is spent too. Its agent was started and its issue was
        claimed, so freeing its slot in the *budget* as well as in the width
        would let a run of lost lanes dispatch past its ceiling one turn at a
        time.
        """
        spent = self._handed_back + len(self._settled) + len(self._flying) + len(self._lost)
        return self.max_iterations - spent

    def _collect(self, session: Session, policy: Settle) -> None:
        """Reap whatever has settled, and give up on whatever never will."""
        for result in reap(session, policy=policy):
            self._flying.pop(result.iteration.issue_id, None)
            self._settled.append(result)
        self._abandon(session)

    def _abandon(self, session: Session) -> None:
        """Stop waiting on a turn that is overdue and was not collected anyway.

        :func:`milhouse.step.reap` treats a turn past ``[agent]
        turn_timeout_ms`` as collectable, so one still in flight after a reap
        that saw it as overdue has no lane left to reap: herdr does not know
        where it went, and no later poll will change that. Waiting on it forever
        would hang an unattended run on a pane somebody closed. One poll
        interval of grace keeps a turn that crossed the deadline between the two
        checks from being given up on a round early.
        """
        deadline = session.config.agent.turn_timeout_ms / 1000 + self.poll_ms / 1000
        for issue_id, pending in list(self._flying.items()):
            if (now() - pending.started_at).total_seconds() <= deadline:
                continue
            session.report(f"{issue_id} is overdue and cannot be reaped; giving up on it")
            del self._flying[issue_id]
            self._lost.append(issue_id)
