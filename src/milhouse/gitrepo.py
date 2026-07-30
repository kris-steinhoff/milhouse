"""The small amount of git milhouse needs.

Decision 4 classifies an iteration partly on whether ``HEAD`` moved, and the
branching strategy (ADR 0007) needs one branch created per task. A concurrent
run adds one more thing, landing a worker lane in its integration branch
(:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
That is the whole surface: locate the repo, read ``HEAD``, put the loop on a
branch, and merge one branch into another.

A :class:`GitRepo` is bound to a **working directory**, not to the repository
root. Every read is therefore scoped to the checkout the question is about: a
worktree has its own ``HEAD``, its own branch, and its own status, so classifying
a turn against the directory that turn ran in is the only reading that stays
true once several agents work at once
(:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`). Bound
to the repository root it also happens to be what it always was.

Everything here goes through :mod:`milhouse.proc`, so tests fake it the same way
they fake ``bd`` and ``herdr``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import proc
from .errors import MilhouseError

__all__ = ["GitRepo", "Merge", "find_repo_root"]


@dataclass(frozen=True)
class Merge:
    """What one merge did to the working directory it ran in.

    A conflict is an outcome a run reports rather than a bug, so it arrives here
    as :attr:`conflicts` rather than as an exception
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

    Attributes:
        sha: Full sha of the commit the merge left at ``HEAD``, or ``None`` when
            nothing was merged: the branch was already contained, or it
            conflicted and was aborted.
        fast_forwarded: Whether ``HEAD`` simply moved to the merged branch. False
            for a merge commit, and false when nothing was merged.
        conflicts: Paths git reported as conflicted, empty when there were none.
            The merge has already been aborted when this is non-empty.
    """

    sha: str | None
    fast_forwarded: bool
    conflicts: tuple[str, ...] = ()

    @property
    def joined(self) -> bool:
        """Whether this merge combined two histories nobody has tested together.

        The signal ADR 0024 keeps by not passing ``--no-ff``: a fast-forward
        leaves the tree the merged branch was already verified against, and only
        a real merge commit produces one that nothing has run a gate over.
        """
        return self.sha is not None and not self.fast_forwarded


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
    """One working directory milhouse reads ``HEAD`` from and commits into."""

    def __init__(self, path: Path) -> None:
        """Bind to the checkout at ``path``.

        Args:
            path: Absolute path of a directory inside the repository. The
                repository root for a plain run, a lane's worktree otherwise.
        """
        self.path = path

    def at(self, path: Path) -> GitRepo:
        """Another :class:`GitRepo` bound to ``path``.

        Args:
            path: The working directory to read instead of this one.

        Returns:
            A repo scoped to ``path``, or ``self`` when it is already the one
            asked for.
        """
        return self if path == self.path else GitRepo(path)

    def _git(self, *args: str, check: bool = True) -> proc.ProcResult:
        """Run a git command in this working directory."""
        return proc.run(["git", "-C", str(self.path), *args], check=check)

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

    def merge(self, branch: str, *, message: str = "") -> Merge:
        """Merge ``branch`` into the branch checked out here.

        Merging, not rebasing: the audit log names the short shas a turn
        produced (:doc:`ADR 0021
        <../../docs/decisions/0021-iteration-history-goes-in-the-beads-audit-log>`),
        and a rebase would rewrite them. A fast-forward is allowed rather than
        forced into a merge commit, because whether git had to join two
        histories is what decides whether the result needs verifying again
        (:doc:`ADR 0024
        <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

        A conflict is reported, not raised: ``git merge --abort`` runs first, so
        this working directory is left exactly where it was.

        Args:
            branch: Name of the branch to merge in.
            message: Commit message for a merge commit. Empty leaves git its own
                ``Merge branch '<branch>'``, and is ignored on a fast-forward,
                which makes no commit.

        Returns:
            What the merge did: the resulting sha, whether it fast-forwarded,
            and the conflicted paths when there were any.

        Raises:
            MilhouseError: The merge failed for a reason that is not a conflict,
                or a conflicted merge could not be aborted.
        """
        before = self.head()
        tip = self._git("rev-parse", "--verify", f"{branch}^{{commit}}", check=False)
        args = ["merge", "--no-edit"]
        if message:
            args += ["-m", message]
        result = self._git(*args, branch, check=False)

        if not result.ok:
            conflicts = self._conflicts()
            if not conflicts:
                raise MilhouseError(
                    f"could not merge {branch}: {result.stderr.strip() or result.stdout.strip()}"
                )
            abort = self._git("merge", "--abort", check=False)
            if not abort.ok:
                raise MilhouseError(
                    f"merging {branch} conflicted and the merge could not be aborted: "
                    f"{abort.stderr.strip() or abort.stdout.strip()}"
                )
            return Merge(sha=None, fast_forwarded=False, conflicts=conflicts)

        after = self.head()
        if after is None or after == before:
            # Already contained, so git made no commit and moved nothing.
            return Merge(sha=None, fast_forwarded=False)
        # A fast-forward leaves HEAD *on* the merged tip; a merge commit is a new
        # commit that no branch pointed at before, so the two never look alike.
        return Merge(sha=after, fast_forwarded=after == tip.stdout.strip())

    def _conflicts(self) -> tuple[str, ...]:
        """Paths left unmerged by a merge that has not been resolved or aborted."""
        result = self._git("diff", "--name-only", "--diff-filter=U", check=False)
        if not result.ok:
            return ()
        return tuple(line for line in result.stdout.splitlines() if line.strip())

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
