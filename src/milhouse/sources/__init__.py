"""Task definition sources.

One entry point, :func:`resolve`, which dispatches a spec string to the source
that recognises it. See :mod:`milhouse.sources.base` for the protocol.
"""

from __future__ import annotations

from .base import SOURCES, Source, resolve
from .file import FileSource
from .github import GitHubSource

__all__ = ["SOURCES", "FileSource", "GitHubSource", "Source", "resolve"]
