"""What happens after an iteration, as a pure function.

:func:`decide` takes the iteration that just ended and returns what becomes of
the issue it worked, plus a line for the person who will read it. It performs no
I/O, so every row of the table below is a unit test — the same reason
:func:`milhouse.outcome.classify` is pure.

Keeping the decision separate from the mutation that carries it out is the point.
The mutation lives on :class:`~milhouse.session.Session`, and swapping the policy
becomes swapping a function
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).

**There is one policy today: supervised.** Every iteration hands back to a person
afterwards, so all it has to settle is what state the issue is left in:

===========  ===============================================
Outcome      Issue becomes
===========  ===============================================
``success``  closed, by the agent
``blocked``  open, for a human to unblock
``rejected`` open, with the failing verification noted
everything   open, so the next claim can see it
else
===========  ===============================================

Retrying, attempt caps, and waiting out a blocked agent are what a policy for an
unattended loop adds when there is one
(:doc:`ADR 0017 <../../docs/decisions/0017-no-loop-until-it-is-earned>`). They are
deliberately absent: they answer questions that only arise once nobody is
watching, and right now somebody always is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import Iteration

__all__ = ["Decision", "IssueAction", "decide"]

IssueAction = Literal["none", "release"]
"""What the issue's status becomes.

``release`` returns it to the open, unassigned pool. It is not optional
housekeeping: a claimed issue is ``in_progress``, and ``bd ready`` excludes
those, so an unfinished issue left alone would never be offered again and the
epic would look finished with the work undone.

There is no ``block`` here. Marking an issue blocked was how the old attempt cap
gave up on one and moved to the next, and giving up is a decision this policy
hands to a person.
"""


@dataclass(frozen=True)
class Decision:
    """What to do now an iteration is over.

    Attributes:
        issue: What becomes of the issue that was worked.
        reason: One line for the person reading it, empty when there is nothing
            to say beyond the outcome itself.
        note: Text to append to the issue before acting, or ``None``.
    """

    issue: IssueAction
    reason: str = ""
    note: str | None = None


def decide(iteration: Iteration) -> Decision:
    """Decide what happens after ``iteration``.

    Args:
        iteration: The iteration that just ended, already classified.

    Returns:
        What becomes of the issue, and what to say about it.
    """
    if iteration.outcome == "success":
        if iteration.dirty_after:
            return Decision(
                issue="none",
                reason=(
                    f"{iteration.issue_id} is closed but the working tree is dirty; "
                    "commit or discard the leftovers before stepping again"
                ),
            )
        return Decision(issue="none")

    if iteration.outcome == "blocked":
        return Decision(
            issue="release",
            reason=(
                f"the agent stopped waiting on a human during {iteration.issue_id}; "
                "attach to the workspace, then step again"
            ),
            note=(
                f"milhouse iteration {iteration.number} left this open: the agent "
                "stopped waiting on a human."
            ),
        )

    if iteration.outcome == "rejected":
        return Decision(
            issue="release",
            reason=(
                f"{iteration.issue_id} was closed but verification failed; "
                "it has been re-opened with the output"
            ),
            note=(
                f"milhouse re-opened this issue: it was closed in iteration "
                f"{iteration.number}, but verification failed.\n\n"
                f"{iteration.verification_output}"
            ),
        )

    dirty = _DIRTY if iteration.dirty_after else ""
    return Decision(
        issue="release",
        reason=(
            f"{iteration.issue_id} did not finish ({iteration.outcome}: {iteration.detail}){dirty}"
        ),
        note=(
            f"milhouse iteration {iteration.number} ended {iteration.outcome}: {iteration.detail}"
        ),
    )


_DIRTY = ". The working tree has uncommitted changes; the next agent would inherit them"
"""Appended to a failure's reason when the turn left work behind uncommitted."""
