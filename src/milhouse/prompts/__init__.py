"""Prompt templates, and the rendering of them.

The templates ship inside the package rather than being user-configurable, so a
run is reproducible from a milhouse version
(:doc:`ADR 0010 <../../docs/decisions/0010-config-file-schema>`). Each ``.j2``
opens with a comment block stating the contract it imposes on the agent and the
variables it expects; ``docs/prompts.md`` is the prose version.

For a ralph loop the prompt *is* the product, so expect these to change. Every
change is a behaviour change and lands with the doc change describing it.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from ..models import Issue

__all__ = ["render", "render_iterate"]

_env = Environment(
    loader=PackageLoader("milhouse", "prompts"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)
"""Jinja environment for the packaged templates.

``StrictUndefined`` on purpose: a typo in a variable name should fail loudly at
render time, not quietly send an agent a prompt with a hole in it.
"""


def render(template: str, **context: Any) -> str:
    """Render a packaged template.

    Args:
        template: Template filename, e.g. ``iterate.md.j2``.
        **context: Template variables.

    Returns:
        The rendered prompt.
    """
    return _env.get_template(template).render(**context)


def render_iterate(
    issue: Issue,
    *,
    background: str = "",
    branch: str | None = None,
    attempt: int = 1,
    previous: list[dict[str, str]] | None = None,
) -> str:
    """Render the per-issue prompt for one iteration.

    Args:
        issue: The issue to work.
        background: The parent epic's description, which is where the wider
            context the issue serves now lives
            (:doc:`ADR 0018 <../../docs/decisions/0018-no-task-milhouse-works-the-ready-queue>`).
            Empty when the issue has no parent, or the parent says nothing.
        branch: Branch the agent must commit to, or ``None`` to leave it alone.
        attempt: 1-based attempt number for this issue.
        previous: Earlier attempts, as ``{"outcome", "detail"}`` mappings. These
            are what a fresh context window gets instead of memory.

    Returns:
        The rendered prompt.
    """
    return render(
        "iterate.md.j2",
        issue=issue,
        background=background.strip(),
        branch=branch,
        attempt=attempt,
        previous=previous or [],
        acceptance=_field(issue, "acceptance_criteria", "acceptance"),
        notes=_field(issue, "notes"),
    )


def _field(issue: Issue, *names: str) -> str:
    """First non-empty value among ``names`` in the issue's raw bead.

    ``bd`` has moved these field names around between versions, so the prompt
    asks for several rather than assuming one.
    """
    for name in names:
        value = issue.raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
