"""Checking that a closed issue is actually done.

The success signal is ``bd`` saying the issue is closed, and the agent is the one
that closes it. That makes a run a self-graded exam: an agent that closes an
unfinished issue is the one failure milhouse could not detect
(:doc:`ADR 0013 <../../docs/decisions/0013-iteration-prompt-contract>`).

:func:`verify` runs the repository's own gate — its tests, its linter, whatever
``[verify] command`` names — after an iteration that claims success. A failure
becomes the ``rejected`` outcome: the issue is re-opened and the output is
appended as a note, so the next agent starts knowing why its predecessor's work
was turned down
(:doc:`ADR 0016 <../../docs/decisions/0016-milhouse-verifies>`).

It runs only when the agent claims to be finished. Running it every turn would
pay for the repository's whole test suite to tell us something we already know,
which is that an unfinished issue is unfinished.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import proc
from .config import Config
from .errors import ProcessError

__all__ = ["OUTPUT_TAIL", "Verification", "verify"]

log = logging.getLogger(__name__)

OUTPUT_TAIL = 2_000
"""Characters of output kept from a failure.

The tail rather than the head: test runners put the summary at the end. It is
capped because this text is appended to a bead and written to the event log, and
neither wants a megabyte of pytest.
"""


@dataclass(frozen=True)
class Verification:
    """What the verification command reported.

    Attributes:
        ok: Whether it exited zero.
        command: The command, for the message a human or an agent reads.
        output: The tail of its combined output. Empty when it passed.
    """

    ok: bool
    command: str
    output: str = ""


def verify(config: Config) -> Verification | None:
    """Run the configured verification command in the repository.

    Args:
        config: Resolved configuration, holding the command and its timeout.

    Returns:
        What the command reported, or ``None`` when none is configured — which
        is the default, and means milhouse takes the agent at its word.
    """
    argv = config.verify.command
    if not argv:
        return None

    printable = " ".join(argv)
    log.debug("verifying with: %s", printable)
    try:
        result = proc.run(
            argv,
            cwd=config.repo_root,
            timeout=config.verify.timeout_ms / 1000,
            check=False,
        )
    except ProcessError as exc:
        # A command that will not start, or that hangs past its timeout, is a
        # failed verification rather than a failed run. The issue goes back in
        # the queue either way, and the reason is in the note.
        return Verification(ok=False, command=printable, output=_tail(str(exc)))

    if result.ok:
        return Verification(ok=True, command=printable)
    return Verification(
        ok=False,
        command=printable,
        output=_tail(f"{result.stdout}\n{result.stderr}".strip()),
    )


def _tail(text: str) -> str:
    """The last :data:`OUTPUT_TAIL` characters of ``text``, marked if truncated."""
    text = text.strip()
    if len(text) <= OUTPUT_TAIL:
        return text
    return "…\n" + text[-OUTPUT_TAIL:]
