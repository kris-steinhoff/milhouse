"""Shell completion callbacks for the command line.

typer installs the completion script itself (``milhouse --install-completion``);
this module supplies the values it offers for each parameter. Three rules every
callback here follows:

- **Never raise.** A callback runs on a keypress, in a shell with nowhere to
  show a traceback. One that fails returns nothing and lets the shell fall back
  to its own completion.
- **Never call a server.** Completions come from the filesystem and from
  constants, so tab is instant and works with the herdr server down. The one
  subprocess any of them runs is the ``git rev-parse`` that finds the repo root.
- **Suggest, do not constrain.** ``--agent`` takes any kind herdr supports; the
  list here is the common ones, not a validation rule.

typer only offers values that start with what was typed, so every path returned
is spelled the way the user is spelling it rather than normalised.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import get_args

from .config import RUNS_RELPATH, BranchStrategy, OnBlocked
from .gitrepo import find_repo_root
from .models import RunState

__all__ = [
    "AGENT_KINDS",
    "complete_agent",
    "complete_branch_strategy",
    "complete_on_blocked",
    "complete_repo",
    "complete_task",
    "complete_workspace",
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

FILE_PREFIX = "file:"
"""Explicit prefix on a file task spec, which completion preserves if typed."""

TASK_SUFFIXES = (".md", ".markdown")
"""Extensions a task definition can have. Anything else is not offered."""

_ON_BLOCKED_HELP = {
    "wait": "wait for a human, up to blocked_timeout_ms",
    "skip": "mark the issue blocked and move on",
    "abort": "stop the run",
}

_BRANCH_STRATEGY_HELP = {
    "task": "one branch per task definition",
    "current": "commit to the branch you are on",
}


def complete_task(incomplete: str) -> list[str]:
    """Complete a task spec with markdown files and the directories holding them.

    Directories come back with a trailing ``/`` so a second tab descends into
    them. ``gh:`` specs are left alone: those issue numbers live on GitHub, and
    fetching them would mean a network call on a keypress.

    Args:
        incomplete: What the user has typed so far.

    Returns:
        Matching paths, sorted, or nothing when there is nothing to offer.
    """
    if incomplete.startswith("gh:"):
        return []
    prefix = FILE_PREFIX if incomplete.startswith(FILE_PREFIX) else ""
    return sorted(prefix + match for match in _paths(incomplete[len(prefix) :], _is_task_file))


def complete_repo(incomplete: str) -> list[str]:
    """Complete a ``--repo`` value with directories only."""
    return sorted(_paths(incomplete, _nothing))


def complete_agent(incomplete: str) -> list[str]:
    """Complete an ``--agent`` value with the common herdr agent kinds."""
    return [kind for kind in AGENT_KINDS if kind.startswith(incomplete)]


def complete_on_blocked(incomplete: str) -> list[tuple[str, str]]:
    """Complete an ``--on-blocked`` value with the three policies and what they do."""
    return _choices(get_args(OnBlocked), _ON_BLOCKED_HELP, incomplete)


def complete_branch_strategy(incomplete: str) -> list[tuple[str, str]]:
    """Complete a ``--branch-strategy`` value with the two strategies."""
    return _choices(get_args(BranchStrategy), _BRANCH_STRATEGY_HELP, incomplete)


def complete_workspace(incomplete: str) -> list[tuple[str, str]]:
    """Complete a ``--workspace`` value from the workspaces this repo's runs used.

    Reusing a workspace is nearly always about rejoining one of these runs, and
    their ids are recorded in ``.milhouse/runs/*/state.json``. Reading those
    keeps completion local, and works whether or not the herdr server is up.

    Args:
        incomplete: What the user has typed so far.

    Returns:
        ``(workspace_id, task_id)`` pairs, most recently updated run first.
    """
    try:
        repo_root = find_repo_root()
    except Exception:
        return []
    found: dict[str, str] = {}
    for state in _run_states(repo_root):
        if state.workspace_id and state.workspace_id.startswith(incomplete):
            found.setdefault(state.workspace_id, state.task_id)
    return list(found.items())


def _run_states(repo_root: Path) -> list[RunState]:
    """Every readable run state under ``repo_root``, most recently updated first.

    An unreadable or outdated ``state.json`` is skipped rather than reported:
    completion is not the place to learn a state file went bad.
    """
    states = []
    for path in sorted((repo_root / RUNS_RELPATH).glob("*/state.json")):
        try:
            state = RunState.load(path)
        except Exception:
            continue
        if state is not None:
            states.append(state)
    return sorted(states, key=lambda state: state.updated_at, reverse=True)


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


def _is_task_file(path: Path) -> bool:
    """Whether ``path`` looks like a task definition."""
    return path.suffix.lower() in TASK_SUFFIXES


def _nothing(path: Path) -> bool:
    """Accept no files, for completions that offer directories only."""
    return False


def _choices(
    values: tuple[str, ...], help_text: dict[str, str], incomplete: str
) -> list[tuple[str, str]]:
    """Filter ``values`` by ``incomplete``, pairing each with its help text."""
    return [(value, help_text.get(value, "")) for value in values if value.startswith(incomplete)]
