"""Repeating a step until something says stop.

:class:`RalphLoop` is deliberately thin. It opens a session, makes sure the task
is decomposed, and then calls :func:`milhouse.step.step` until the policy stops
it, the epic runs out of ready issues, or the iteration budget is spent. Every
interesting decision belongs to somebody else: :mod:`milhouse.outcome` says what
an iteration achieved, :mod:`milhouse.policy` says what to do about it, and
:mod:`milhouse.session` owns the state.

The loop keeps two things of its own, and they are the two an agent cannot bound
from inside its own context: **what is worked next** and **when to stop**
(:doc:`ADR 0005 <../../docs/decisions/0005-milhouse-owns-the-loop>`).

The name is aspirational for now. Today's policy stops at the first iteration
that does not succeed, so a run is a supervised batch rather than an unattended
ralph loop. Making it ralph is a policy that lands later, over this same step
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).
"""

from __future__ import annotations

import contextlib
import logging
import signal
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from .errors import UserAbortError
from .models import Iteration
from .policy import decide
from .session import Session
from .step import Policy, nothing_ready, step

__all__ = ["LoopResult", "RalphLoop"]

log = logging.getLogger(__name__)


@dataclass
class LoopResult:
    """How a run ended.

    Attributes:
        reason: Why the loop stopped, in one line.
        completed: Whether the epic was worked to exhaustion.
        iterations: The iterations this invocation ran, in order.
    """

    reason: str
    completed: bool
    iterations: list[Iteration] = field(default_factory=list)

    @property
    def count(self) -> int:
        """How many iterations this invocation ran."""
        return len(self.iterations)


class RalphLoop:
    """Calls :func:`~milhouse.step.step` until something says stop."""

    def __init__(self, session: Session, *, policy: Policy = decide) -> None:
        """Bind the loop to a session and a policy.

        Args:
            session: The session to drive. Not yet opened.
            policy: What decides the aftermath of each iteration.
        """
        self.session = session
        self.policy = policy

    def run(self, *, confirm: Callable[[Any], bool] | None = None) -> LoopResult:
        """Decompose if needed, then step until something stops the run.

        Args:
            confirm: Passed to the planner for decomposition approval. ``None``
                creates without asking, which is what ``--yes`` does.

        Returns:
            How the run ended.

        Raises:
            UserAbortError: The run was interrupted, or approval was declined.
        """
        with _interrupts(), self.session as session:
            epic = session.ensure_epic(confirm=confirm)
            budget = session.config.loop.max_iterations
            done: list[Iteration] = []

            while len(done) < budget:
                result = step(session, epic, policy=self.policy)
                if result is None:
                    reason, completed = nothing_ready(session, epic)
                    return self._stop(session, reason, completed=completed, iterations=done)
                done.append(result.iteration)
                if result.decision.stop:
                    return self._stop(
                        session, result.decision.reason, completed=False, iterations=done
                    )

            return self._stop(
                session,
                f"reached the {budget}-iteration budget for this run",
                completed=False,
                iterations=done,
            )

    def _stop(
        self,
        session: Session,
        reason: str,
        *,
        completed: bool,
        iterations: list[Iteration],
    ) -> LoopResult:
        """Report why the run ended. Teardown is the session's job."""
        session.report(reason)
        return LoopResult(reason=reason, completed=completed, iterations=iterations)


@contextlib.contextmanager
def _interrupts() -> Iterator[None]:
    """Turn SIGINT/SIGTERM into a :class:`UserAbortError` for the duration.

    Raising from the handler unwinds through the session's ``__exit__``, which is
    what reverts the in-flight claim and exits the agent. Panes are never closed
    out from under a human.
    """
    previous: dict[int, Any] = {}

    def handle(signum: int, frame: FrameType | None) -> None:
        raise UserAbortError("interrupted")

    for number in (signal.SIGINT, signal.SIGTERM):
        # ValueError means we are not on the main thread, where signal handlers
        # cannot be installed; the caller keeps its own.
        with contextlib.suppress(ValueError):
            previous[number] = signal.getsignal(number)
            signal.signal(number, handle)
    try:
        yield
    finally:
        for number, handler in previous.items():
            with contextlib.suppress(ValueError):
                signal.signal(number, handler)
