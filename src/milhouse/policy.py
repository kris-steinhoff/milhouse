"""What happens after an iteration, as a pure function.

:func:`decide` takes the iteration that just ended and returns two things: what
becomes of the issue, and whether the run carries on. It performs no I/O, so
every row of the table below is a unit test — the same reason
:func:`milhouse.outcome.classify` is pure.

Keeping the decision separate from the mutation that carries it out is the point.
The mutation lives on :class:`~milhouse.session.Session`, and swapping the policy
becomes swapping a function
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).

**There is one policy today: supervised.** It stops at the first iteration that
does not succeed, and says why in a line the human can act on:

===========  ===============================  ==============
Outcome      Issue becomes                    Run
===========  ===============================  ==============
``success``  closed, by the agent             carries on
``blocked``  open, for a human to unblock     stops
``rejected`` open, with the failure noted     stops
everything   open, so the next claim sees it  stops
else
===========  ===============================  ==============

A run that succeeds all the way to an empty ready queue is the only one that
finishes. Anything else hands back to a person.

Retrying, attempt caps, and waiting out a blocked agent are what the ralph policy
adds when it lands. They are deliberately absent here: an unattended retry ladder
is a set of answers to questions that only arise once a run is unattended, and
this one is not yet.
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
        stop: Whether the run ends here.
        reason: One line for the human, empty when the run carries on.
        note: Text to append to the issue before acting, or ``None``.
    """

    issue: IssueAction
    stop: bool
    reason: str = ""
    note: str | None = None


def decide(iteration: Iteration) -> Decision:
    """Decide what happens after ``iteration``.

    Args:
        iteration: The iteration that just ended, already classified.

    Returns:
        What becomes of the issue, and whether the run stops.
    """
    if iteration.outcome == "success":
        if iteration.dirty_after:
            return Decision(
                issue="none",
                stop=True,
                reason=(
                    f"{iteration.issue_id} is closed but the working tree is dirty; "
                    "commit or discard the leftovers before running again"
                ),
            )
        return Decision(issue="none", stop=False)

    if iteration.outcome == "blocked":
        return Decision(
            issue="release",
            stop=True,
            reason=(
                f"the agent stopped waiting on a human during {iteration.issue_id}; "
                "attach to the workspace, then run again"
            ),
            note=(
                f"milhouse iteration {iteration.number} left this open: the agent "
                "stopped waiting on a human."
            ),
        )

    if iteration.outcome == "rejected":
        return Decision(
            issue="release",
            stop=True,
            reason=(
                f"{iteration.issue_id} was closed but verification failed; "
                "it has been re-opened with the output"
            ),
        )

    return Decision(
        issue="release",
        stop=True,
        reason=f"{iteration.issue_id} did not finish ({iteration.outcome}: {iteration.detail})",
        note=(
            f"milhouse iteration {iteration.number} ended {iteration.outcome}: {iteration.detail}"
        ),
    )
