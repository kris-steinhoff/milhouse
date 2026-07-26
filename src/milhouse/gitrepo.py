"""The small amount of git milhouse needs.

Decision 4 classifies an iteration partly on whether ``HEAD`` moved, and the
branching strategy (ADR 0007) needs one branch created per task. That is the
whole surface: locate the repo, read ``HEAD``, and put the loop on a branch.

Everything here goes through :mod:`milhouse.proc`, so tests fake it the same way
they fake ``bd`` and ``herdr``.
"""

from __future__ import annotations

from pathlib import Path

from . import proc
from .errors import MilhouseError

__all__ = ["GitRepo", "find_repo_root"]


def find_repo_root(start: Path | None = None) -> Path:
    """Find the root of the git repository containing ``start``.

    Args:
        start: Directory to search from. Defaults to the current directory.

    Returns:
        Absolute path of the repository root.

    Raises:
        MilhouseError: ``start`` is not inside a git repository.
    """
    start = (start or Path.cwd()).resolve()
    try:
        result = proc.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"])
    except MilhouseError as exc:
        raise MilhouseError(f"{start} is not inside a git repository") from exc
    return Path(result.stdout.strip())


class GitRepo:
    """A git repository milhouse reads ``HEAD`` from and commits into."""

    def __init__(self, root: Path) -> None:
        """Bind to the repository rooted at ``root``.

        Args:
            root: Absolute path of the repository root.
        """
        self.root = root

    def _git(self, *args: str, check: bool = True) -> proc.ProcResult:
        """Run a git command in this repository."""
        return proc.run(["git", "-C", str(self.root), *args], check=check)

    def head(self) -> str | None:
        """Current commit sha, or ``None`` in a repository with no commits yet.

        Returns:
            The full sha of ``HEAD``, or ``None`` before the first commit.
        """
        result = self._git("rev-parse", "HEAD", check=False)
        if not result.ok:
            return None
        return result.stdout.strip() or None

    def current_branch(self) -> str | None:
        """Name of the checked-out branch, or ``None`` when detached."""
        result = self._git("rev-parse", "--abbrev-ref", "HEAD", check=False)
        name = result.stdout.strip()
        if not result.ok or not name or name == "HEAD":
            return None
        return name

    def branch_exists(self, name: str) -> bool:
        """Whether a local branch named ``name`` exists."""
        return self._git("show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False).ok

    def ensure_branch(self, name: str) -> str:
        """Check out ``name``, creating it from the current ``HEAD`` if needed.

        Idempotent: already being on ``name`` is a no-op, so resuming a run does
        not disturb the working tree.

        Args:
            name: Branch name, e.g. ``milhouse/hello``.

        Returns:
            The branch name, for chaining into run state.

        Raises:
            MilhouseError: The checkout failed, usually because the working tree
                has changes that would be overwritten.
        """
        if self.current_branch() == name:
            return name
        args = ["checkout", name] if self.branch_exists(name) else ["checkout", "-b", name]
        result = self._git(*args, check=False)
        if not result.ok:
            raise MilhouseError(
                f"could not check out branch {name}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return name

    def commits_between(
        self, before: str | None, after: str | None, *, grep: str = ""
    ) -> list[str]:
        """Short shas committed between two revisions, oldest first.

        ``before`` is ``None`` in a repository with no commits yet, in which case
        everything reachable from ``after`` is new.

        Args:
            before: Revision the range starts after, exclusive.
            after: Revision the range ends at, inclusive. ``None`` means the
                repository still has no commits, so the range is empty.
            grep: Only count commits whose message contains this string, matched
                literally. Empty counts every commit in the range.

        Returns:
            Short shas, oldest first, or an empty list when nothing landed.
        """
        if after is None:
            return []
        span = f"{before}..{after}" if before else after
        args = ["log", "--format=%h", "--reverse"]
        if grep:
            args += ["--fixed-strings", f"--grep={grep}"]
        result = self._git(*args, span, check=False)
        if not result.ok:
            return []
        return result.stdout.split()

    def is_dirty(self) -> bool:
        """Whether the working tree has uncommitted or untracked changes."""
        return bool(self._git("status", "--porcelain").stdout.strip())
