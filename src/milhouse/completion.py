"""Shell completion callbacks for the command line.

typer installs the completion script itself (``milhouse --install-completion``);
this module supplies the values it offers for each parameter. Three rules every
callback here follows:

- **Never raise.** A callback runs on a keypress, in a shell with nowhere to
  show a traceback. One that fails returns nothing and lets the shell fall back
  to its own completion.
- **Never call a server.** Completions come from the filesystem and from
  constants, so tab is instant and works with the herdr server down. That is
  why ``--workspace`` has no callback: milhouse no longer writes a workspace id
  down anywhere, and the only thing left that knows one is herdr itself
  (:doc:`ADR 0021 <../../docs/decisions/0021-iteration-history-goes-in-the-beads-audit-log>`).
- **Suggest, do not constrain.** ``--agent`` takes any kind herdr supports; the
  list here is the common ones, not a validation rule.

typer only offers values that start with what was typed, so every path returned
is spelled the way the user is spelling it rather than normalised.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

__all__ = [
    "AGENT_KINDS",
    "complete_agent",
    "complete_repo",
]

AGENT_KINDS = (
    "claude",
    "codex",
    "gemini",
    "amp",
    "opencode",
    "copilot",
    "cursor",
    "droid",
)
"""Common ``herdr agent start --kind`` values, offered as suggestions.

herdr's enum is longer and grows; ``herdr agent start --help`` has the current
list. Any kind herdr accepts works, whether or not it appears here.
"""


def complete_repo(incomplete: str) -> list[str]:
    """Complete a ``--repo`` value with directories only."""
    return sorted(_paths(incomplete, _nothing))


def complete_agent(incomplete: str) -> list[str]:
    """Complete an ``--agent`` value with the common herdr agent kinds."""
    return [kind for kind in AGENT_KINDS if kind.startswith(incomplete)]


def _paths(incomplete: str, keep: Callable[[Path], bool]) -> Iterator[str]:
    """Yield directories, plus files ``keep`` accepts, matching ``incomplete``.

    The text up to the last ``/`` names the directory to list, and what follows
    is the prefix entries must match. That text is re-used verbatim in the
    results, so ``./doc`` completes to ``./docs/`` rather than to ``docs/``.

    Args:
        incomplete: A partial path, as typed.
        keep: Predicate deciding which non-directory entries to offer.

    Yields:
        Matching paths, directories with a trailing ``/``.
    """
    head, sep, prefix = incomplete.rpartition("/")
    directory = Path(head + sep) if sep else Path()
    if prefix.startswith("~") or head.startswith("~"):
        return  # The shell expands ~, so anything still holding one is unusable.
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(prefix) or (entry.name.startswith(".") and not prefix):
            continue
        if entry.is_dir():
            yield f"{head}{sep}{entry.name}/"
        elif keep(entry):
            yield f"{head}{sep}{entry.name}"


def _nothing(path: Path) -> bool:
    """Accept no files, for completions that offer directories only."""
    return False
