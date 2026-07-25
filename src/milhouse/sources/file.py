"""Task definitions read from a local markdown file.

The common case: point milhouse at a file describing what you want done.

The file's repo-relative path is the ``task_id``, which makes the link between
file and epic stable across runs but *not* across renames. Renaming a task file
orphans its epic; ``docs/troubleshooting.md`` says how to repoint it.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import SourceError
from ..models import TaskDefinition, slugify

__all__ = ["FileSource"]

PREFIX = "file:"


class FileSource:
    """Resolves ``docs/tasks/hello.md`` or ``file:docs/tasks/hello.md``."""

    name = "file"

    def handles(self, spec: str, repo_root: Path) -> bool:
        """Accept an explicit ``file:`` prefix, or any spec that is not ``gh:``.

        This is the fallback source, so it claims anything left over and reports
        a clear "no such file" rather than letting :func:`~.base.resolve` report
        a vaguer "unrecognised spec".
        """
        return spec.startswith(PREFIX) or not spec.startswith("gh:")

    def resolve(self, spec: str, repo_root: Path) -> TaskDefinition:
        """Read the file and derive a title, slug, and ``task_id`` from it.

        Args:
            spec: A path, optionally prefixed with ``file:``. Relative paths are
                resolved against the current directory first, then the repo root.
            repo_root: Root of the repository, used to make the path relative.

        Returns:
            The task definition. ``title`` is the file's first markdown heading,
            falling back to the filename stem.

        Raises:
            SourceError: The file does not exist, is a directory, is empty, or
                is not valid UTF-8.
        """
        raw = spec[len(PREFIX) :] if spec.startswith(PREFIX) else spec
        path = self._locate(raw, repo_root)
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SourceError(f"{path} is not valid UTF-8 text") from exc
        except OSError as exc:
            raise SourceError(f"cannot read {path}: {exc}") from exc
        if not body.strip():
            raise SourceError(f"{path} is empty; a task definition needs a description")

        relative = self._relative(path, repo_root)
        title = _heading(body) or path.stem
        return TaskDefinition(
            task_id=f"file:{relative}",
            title=title,
            body=body,
            kind="file",
            slug=slugify(path.stem),
        )

    def _locate(self, raw: str, repo_root: Path) -> Path:
        """Find the file, trying the current directory then the repo root.

        Raises:
            SourceError: Nothing readable is at either location.
        """
        candidates = [Path(raw)] if Path(raw).is_absolute() else [Path(raw), repo_root / raw]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        if any(candidate.is_dir() for candidate in candidates):
            raise SourceError(f"{raw} is a directory; pass a task definition file")
        raise SourceError(f"no such task definition: {raw}")

    def _relative(self, path: Path, repo_root: Path) -> str:
        """Path relative to the repo root, or absolute if it lies outside."""
        try:
            return path.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


def _heading(body: str) -> str | None:
    """First markdown ``#`` heading in ``body``, or ``None`` if there is none."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading
    return None
