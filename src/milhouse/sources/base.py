"""Resolving a task spec into a :class:`~milhouse.models.TaskDefinition`.

A *spec* is what the user typed: a path, or a ``gh:`` reference. A *task
definition* is what milhouse works with: a title, a body, and a stable
``task_id`` that links it to a beads epic
(:doc:`ADR 0002 <../../docs/decisions/0002-link-issues-via-bead-metadata>`).

:func:`resolve` picks the right :class:`Source` for a spec. Adding a source means
writing a class with :meth:`Source.handles` and :meth:`Source.resolve` and adding
it to :data:`SOURCES`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..errors import SourceError
from ..models import TaskDefinition

__all__ = ["SOURCES", "Source", "resolve"]


@runtime_checkable
class Source(Protocol):
    """Something that turns a spec string into a task definition."""

    name: str
    """Short label used in error messages, e.g. ``file``."""

    def handles(self, spec: str, repo_root: Path) -> bool:
        """Whether this source recognises ``spec``.

        Args:
            spec: The raw spec string the user passed.
            repo_root: Root of the repository, for resolving relative paths.

        Returns:
            ``True`` if :meth:`resolve` should be called with this spec.
        """
        ...

    def resolve(self, spec: str, repo_root: Path) -> TaskDefinition:
        """Turn ``spec`` into a task definition.

        Args:
            spec: The raw spec string.
            repo_root: Root of the repository.

        Returns:
            The resolved definition.

        Raises:
            SourceError: The spec is malformed or the target cannot be read.
        """
        ...


def resolve(spec: str, repo_root: Path) -> TaskDefinition:
    """Resolve ``spec`` using the first source that recognises it.

    Sources are tried in :data:`SOURCES` order, so the explicitly prefixed ones
    (``gh:``, ``file:``) get first refusal and the bare-path source is the
    fallback.

    Args:
        spec: What the user passed on the command line.
        repo_root: Root of the repository.

    Returns:
        The resolved task definition.

    Raises:
        SourceError: No source recognises the spec, or resolution failed.
    """
    spec = spec.strip()
    if not spec:
        raise SourceError("no task definition given")
    for source in SOURCES:
        if source.handles(spec, repo_root):
            return source.resolve(spec, repo_root)
    raise SourceError(
        f"cannot resolve task definition {spec!r}: "
        "expected a file path or a gh:owner/repo#123 reference"
    )


def _sources() -> list[Source]:
    """Build the source list, imported late to avoid a circular import."""
    from .file import FileSource
    from .github import GitHubSource

    return [GitHubSource(), FileSource()]


SOURCES: list[Source] = _sources()
"""Every known source, in the order :func:`resolve` tries them."""
