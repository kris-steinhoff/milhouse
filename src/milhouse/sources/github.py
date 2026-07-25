"""Task definitions read from a GitHub issue via the ``gh`` CLI.

Uses ``gh`` rather than the REST API directly so authentication is somebody
else's problem: if ``gh auth status`` works, this works, including on GitHub
Enterprise.

The resulting epic also carries ``--external-ref gh-<number>`` so beads can
round-trip the link back to GitHub.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import proc
from ..errors import MilhouseError, MissingDependencyError, SourceError
from ..models import TaskDefinition, slugify

__all__ = ["GitHubSource"]

PREFIX = "gh:"

#: ``gh:owner/repo#123``, ``gh:123``, or ``gh:https://github.com/owner/repo/issues/123``.
_SPEC = re.compile(
    r"""^gh:(?:
        (?:https?://[^/]+/)?(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?:/issues/|\#)(?P<number>\d+)
        |\#?(?P<bare>\d+)
    )$""",
    re.VERBOSE,
)


class GitHubSource:
    """Resolves ``gh:owner/repo#123``, ``gh:123``, and issue URLs."""

    name = "github"

    def handles(self, spec: str, repo_root: Path) -> bool:
        """Accept anything with the ``gh:`` prefix."""
        return spec.startswith(PREFIX)

    def resolve(self, spec: str, repo_root: Path) -> TaskDefinition:
        """Fetch the issue with ``gh`` and build a task definition from it.

        Args:
            spec: A ``gh:`` reference. Without an ``owner/repo``, the repository
                milhouse is running in is used.
            repo_root: Root of the repository, used as ``gh``'s working directory
                so it can infer the repo for a bare issue number.

        Returns:
            The task definition, with ``task_id`` ``gh:<owner>/<repo>#<number>``
            and ``external_ref`` ``gh-<number>``.

        Raises:
            MissingDependencyError: ``gh`` is not installed.
            SourceError: The spec is malformed, or ``gh`` cannot fetch the issue.
        """
        match = _SPEC.match(spec)
        if match is None:
            raise SourceError(
                f"cannot parse {spec!r}: expected gh:owner/repo#123, gh:123, or an issue URL"
            )
        if proc.have("gh") is None:
            raise MissingDependencyError(
                "gh is required for gh: task definitions but is not installed"
            )

        number = match.group("number") or match.group("bare")
        owner, repo = match.group("owner"), match.group("repo")

        argv = ["gh", "issue", "view", number, "--json", "title,body,number,url"]
        if owner and repo:
            argv += ["--repo", f"{owner}/{repo}"]
        try:
            payload = proc.run_json(argv, cwd=repo_root, timeout=60)
        except MilhouseError as exc:
            raise SourceError(f"could not read GitHub issue {spec}: {exc}") from exc

        return self._build(payload, number=number, owner=owner, repo=repo)

    def _build(
        self, payload: Any, *, number: str, owner: str | None, repo: str | None
    ) -> TaskDefinition:
        """Turn ``gh issue view`` JSON into a task definition.

        Raises:
            SourceError: The payload is not the object shape ``gh`` documents.
        """
        if not isinstance(payload, dict):
            raise SourceError(f"unexpected `gh issue view` output for issue {number}")
        title = str(payload.get("title") or f"Issue {number}")
        body = str(payload.get("body") or "")
        url = payload.get("url")
        if owner is None or repo is None:
            owner, repo = _split_url(url) or ("unknown", "unknown")
        return TaskDefinition(
            task_id=f"gh:{owner}/{repo}#{number}",
            title=title,
            body=body,
            kind="github",
            slug=f"gh-{number}-{slugify(title)}"[:60].rstrip("-"),
            external_ref=f"gh-{number}",
            url=str(url) if url else None,
        )


def _split_url(url: Any) -> tuple[str, str] | None:
    """Pull ``owner`` and ``repo`` out of an issue URL, or return ``None``."""
    if not isinstance(url, str):
        return None
    match = re.search(r"://[^/]+/([\w.-]+)/([\w.-]+)/issues/\d+", url)
    return (match.group(1), match.group(2)) if match else None
