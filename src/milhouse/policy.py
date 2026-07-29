"""What happens after an iteration, as a pure function.

:func:`decide` takes the iteration that just ended and returns what becomes of
the issue it worked, plus a line for the person who will read it. It performs no
I/O, so every row of the table below is a unit test — the same reason
:func:`milhouse.outcome.classify` is pure.

Keeping the decision separate from the mutation that carries it out is the point.
The mutation lives on :class:`~milhouse.session.Session`, and swapping the policy
becomes swapping a function
(:doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>`).

**There are two policies.** :func:`decide` is what ``milhouse step`` uses. Every
iteration hands back to a person afterwards, so all it has to settle is what
state the issue is left in:

===========  ===============================================
Outcome      Issue becomes
===========  ===============================================
``success``  closed, by the agent
``blocked``  open, for a human to unblock
``rejected`` open, with the failing verification noted
everything   open, so the next claim can see it
else
===========  ===============================================

:func:`unattended` is what ``milhouse run`` uses, and it is the same rules with
one addition: an issue that has used up its attempts is **deferred** rather than
released, so the loop stops handing the rest of its budget to the issue it has
already failed three times
(:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`).

Neither policy decides whether there is another iteration. That question belongs
to :mod:`milhouse.run`, and keeping it there is what lets the issue's fate and
the run's fate be reasoned about separately.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .models import Iteration, Outcome

__all__ = ["Decision", "IssueAction", "Policy", "counts_as_attempt", "decide", "unattended"]

_NOT_AN_ATTEMPT: frozenset[Outcome] = frozenset({"success", "blocked"})
"""Outcomes that do not burn one of an issue's attempts.

``success`` finished the work. ``blocked`` is waiting on a person, and it is
about to stop the run anyway, so charging it an attempt would only make the
issue look worse than it is when somebody comes back to it.

Stated as the exceptions rather than as the failures, so an outcome added later
counts as an attempt until somebody decides otherwise.
"""


def counts_as_attempt(outcome: Outcome) -> bool:
    """Whether ``outcome`` burns one of the issue's attempts."""
    return outcome not in _NOT_AN_ATTEMPT


IssueAction = Literal["none", "release", "defer"]
"""What the issue's status becomes.

``release`` returns it to the open, unassigned pool. It is not optional
housekeeping: a claimed issue is ``in_progress``, and ``bd ready`` excludes
those, so an unfinished issue left alone would never be offered again and the
epic would look finished with the work undone.

``defer`` sets it aside instead: still open, still unfinished, no longer offered
as ready. Only :func:`unattended` returns it, and only once an issue has used up
its attempts. It is how a run stops spending its budget on one issue without
deciding for anybody that the issue is hopeless
(:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`).

There is no ``block``. Marking an issue blocked was how the old attempt cap gave
up on one and moved to the next, and blocked in ``bd`` means something specific
and untrue here: that the issue has an unmet dependency.
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


Policy = Callable[[Iteration], Decision]
"""What decides the aftermath of an iteration.

The seam :doc:`ADR 0014 <../../docs/decisions/0014-step-is-the-primitive>` said a
loop would swap. :func:`decide` and the function :func:`unattended` builds are
the two that exist.
"""


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


def unattended(*, max_attempts: int = 3) -> Policy:
    """The policy ``milhouse run`` uses: :func:`decide`, until an issue runs out.

    Identical to :func:`decide` for every outcome that is not a failed attempt,
    and for failed attempts until the last one. A failing outcome on attempt
    ``max_attempts`` defers the issue instead of releasing it, so the loop stops
    offering it and moves on
    (:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`).

    Deferring is not a verdict on the issue. It sets it aside where a person
    will see it, with the reason attached, and gives the rest of the run's
    budget to work that might still land. Giving up for good stays a person's
    decision, which is why there is no ``block``.

    Args:
        max_attempts: Attempts an issue gets before it is set aside. Attempts
            are counted over the whole audit history, not over one run, so
            re-running does not hand a hopeless issue three more turns.

    Returns:
        A policy, which is a pure function like the one it wraps.
    """

    def policy(iteration: Iteration) -> Decision:
        if not counts_as_attempt(iteration.outcome) or iteration.attempt < max_attempts:
            return decide(iteration)
        return Decision(
            issue="defer",
            reason=(
                f"{iteration.issue_id} did not finish in {iteration.attempt} attempt(s) "
                f"(last: {iteration.outcome}, {iteration.detail}); deferred so the run "
                "can move on"
            ),
            note=(
                f"milhouse deferred this issue after {iteration.attempt} attempt(s). "
                f"The last one ended {iteration.outcome}: {iteration.detail}.\n\n"
                f"It is still unfinished, but it will not be offered as ready again "
                f"until somebody runs `bd undefer {iteration.issue_id}`. The notes "
                "above are what the attempts found."
            ),
        )

    return policy


_DIRTY = ". The working tree has uncommitted changes; the next agent would inherit them"
"""Appended to a failure's reason when the turn left work behind uncommitted."""
