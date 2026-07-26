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
from .verify import Verification

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
    commits: list[str],
    attributed: bool = False,
    agent_state: AgentStatus,
    timed_out: bool = False,
    error: str | None = None,
    verification: Verification | None = None,
) -> Verdict:
    """Decide what an iteration achieved.

    The order of the checks is the decision, not an implementation detail. A
    closed issue wins over everything, including an agent that also ended
    ``blocked``: if the work is done, it is done. The one thing that outranks it
    is a verification that says it is not
    (:doc:`ADR 0016 <../../docs/decisions/0016-milhouse-verifies>`).

    Args:
        issue_after: The issue as beads has it now the turn is over.
        commits: Short shas of everything that landed during the turn.
        attributed: Whether any of them names this issue in its message.
        agent_state: The lifecycle state herdr left the agent in.
        timed_out: The turn did not settle within the turn timeout.
        error: A milhouse-side failure (herdr or ``bd``), if any.
        verification: What the repository's own gate said, when one is
            configured and the issue was closed. ``None`` means milhouse took
            the agent at its word.

    Returns:
        The outcome and the reason for it.
    """
    if issue_after.is_closed:
        if verification is not None and not verification.ok:
            return Verdict(
                "rejected",
                f"{issue_after.id} was closed but `{verification.command}` failed",
            )
        return Verdict("success", f"{issue_after.id} closed in beads")
    if error:
        return Verdict("error", error)
    if timed_out:
        return Verdict("timeout", "the turn did not finish within the turn timeout")
    if agent_state == "blocked":
        return Verdict("blocked", "the agent is waiting on a human")
    if commits:
        # A commit naming the issue is evidence the agent did this work. A
        # commit that does not could be anyone's — a hook, or a human in another
        # terminal — so it is reported as movement rather than as progress.
        if attributed:
            return Verdict(
                "partial",
                f"{issue_after.id} is still open, but {_count(commits)} for it",
            )
        return Verdict(
            "partial",
            f"{issue_after.id} is still open; {_count(commits)}, none naming it",
        )
    return Verdict("stalled", f"{issue_after.id} is still open and nothing was committed")


def _count(commits: list[str]) -> str:
    """``"1 commit landed"`` or ``"3 commits landed"``, for a detail line."""
    return f"{len(commits)} commit{'' if len(commits) == 1 else 's'} landed"
