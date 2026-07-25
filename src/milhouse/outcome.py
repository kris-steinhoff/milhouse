"""Deciding what one iteration achieved.

There is no exit status to read from an interactive agent, so the outcome is
inferred from observable state: what beads says about the issue, whether git
``HEAD`` moved, and what lifecycle state herdr left the agent in
(:doc:`ADR 0004 <../../docs/decisions/0004-outcome-from-beads-and-git>`).

:func:`classify` is pure — values in, an outcome out — so every row of the
decision table is a unit test with no subprocess involved.
"""

from __future__ import annotations

from dataclasses import dataclass

from .herdr import AgentStatus
from .models import Issue, Outcome

__all__ = ["Verdict", "classify"]


@dataclass(frozen=True)
class Verdict:
    """An outcome and the one-line reason for it.

    Attributes:
        outcome: What happened.
        detail: Why, in a sentence, for the run history and ``milhouse status``.
    """

    outcome: Outcome
    detail: str

    @property
    def counts_as_attempt(self) -> bool:
        """Whether this outcome burns one of the issue's attempts.

        ``success`` finished the work and ``blocked`` is waiting on a human;
        neither is a failed attempt.
        """
        return self.outcome not in ("success", "blocked")


def classify(
    *,
    issue_after: Issue,
    head_before: str | None,
    head_after: str | None,
    agent_state: AgentStatus,
    timed_out: bool = False,
    error: str | None = None,
) -> Verdict:
    """Decide what an iteration achieved.

    The order of the checks is the decision, not an implementation detail. A
    closed issue wins over everything, including an agent that also ended
    ``blocked``: if the work is done, it is done.

    Args:
        issue_after: The issue as beads has it now the turn is over.
        head_before: Git ``HEAD`` before the turn, or ``None`` in an empty repo.
        head_after: Git ``HEAD`` after the turn.
        agent_state: The lifecycle state herdr left the agent in.
        timed_out: The turn did not settle within the turn timeout.
        error: A milhouse-side failure (herdr or ``bd``), if any.

    Returns:
        The outcome and the reason for it.
    """
    if issue_after.is_closed:
        return Verdict("success", f"{issue_after.id} closed in beads")
    if error:
        return Verdict("error", error)
    if timed_out:
        return Verdict("timeout", "the turn did not finish within the turn timeout")
    if agent_state == "blocked":
        return Verdict("blocked", "the agent is waiting on a human")
    # `head_before` is None in a repository with no commits yet, so the first
    # commit of all still counts as movement.
    if head_after and head_before != head_after:
        return Verdict("partial", f"{issue_after.id} is still open, but HEAD moved")
    return Verdict("stalled", f"{issue_after.id} is still open and nothing was committed")
