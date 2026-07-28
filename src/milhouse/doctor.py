"""Preflight checks behind ``milhouse doctor``.

milhouse orchestrates three external tools and fails in confusing ways when one
of them is missing or the herdr server is not running. ``doctor`` turns those
failures into one readable table before a run starts.

Checks are pure data: :func:`run_checks` returns a list of :class:`Check`, and
the CLI renders them. That keeps the logic testable without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import proc
from .config import Config, config_path
from .errors import MilhouseError

__all__ = ["Check", "run_checks"]


@dataclass(frozen=True)
class Check:
    """One preflight check and its result.

    Attributes:
        name: Short label, e.g. ``bd``.
        ok: Whether the check passed.
        detail: Version string, path, or the reason it failed.
        required: Whether a failure should make ``doctor`` exit non-zero.
    """

    name: str
    ok: bool
    detail: str
    required: bool = True


def run_checks(config: Config) -> list[Check]:
    """Run every preflight check and return the results in display order.

    Args:
        config: Resolved configuration, used to find the repo and the agent kind.

    Returns:
        Checks for ``bd``, the beads database, ``herdr``, the herdr server,
        ``git``, the configured agent binary, and the config file.
    """
    checks = [
        _tool("bd", ["bd", "version"]),
        _beads_db(config.repo_root),
        _tool("herdr", ["herdr", "--version"]),
        _herdr_server(),
        _tool("git", ["git", "--version"]),
        _tool(config.agent.kind, [config.agent.kind, "--version"], required=False),
        _config_file(config),
    ]
    return checks


def _tool(name: str, argv: list[str], *, required: bool = True) -> Check:
    """Check that ``name`` is on ``PATH`` and report the version it prints."""
    path = proc.have(argv[0])
    if path is None:
        return Check(name, ok=False, detail="not found on PATH", required=required)
    try:
        result = proc.run(argv, timeout=20)
    except MilhouseError as exc:
        return Check(name, ok=False, detail=str(exc), required=required)
    version = _first_line(result.stdout) or _first_line(result.stderr) or path
    return Check(name, ok=True, detail=version, required=required)


def _beads_db(repo_root: Path) -> Check:
    """Check that this repo has a beads database milhouse can write to."""
    if proc.have("bd") is None:
        return Check("beads db", ok=False, detail="bd not installed")
    try:
        proc.run_json(["bd", "list", "--json", "--limit", "1"], cwd=repo_root, timeout=30)
    except MilhouseError as exc:
        return Check("beads db", ok=False, detail=f"{exc} (run `bd init`)")
    return Check("beads db", ok=True, detail=str(repo_root / ".beads"))


def _herdr_server() -> Check:
    """Check that the herdr server is running and protocol-compatible."""
    if proc.have("herdr") is None:
        return Check("herdr server", ok=False, detail="herdr not installed")
    try:
        result = proc.run(["herdr", "status"], timeout=20)
    except MilhouseError as exc:
        return Check("herdr server", ok=False, detail=str(exc))
    fields = _parse_status(result.stdout)
    if fields.get("status") != "running":
        return Check("herdr server", ok=False, detail="server not running (start `herdr`)")
    if fields.get("compatible") == "no":
        return Check(
            "herdr server",
            ok=False,
            detail=f"protocol mismatch (client {fields.get('protocol', '?')})",
        )
    return Check(
        "herdr server",
        ok=True,
        detail=f"running, protocol {fields.get('protocol', '?')}",
    )


def _config_file(config: Config) -> Check:
    """Report whether a repo config file is in use, which is optional."""
    path = config_path(config.repo_root)
    if path.exists():
        return Check("config", ok=True, detail=str(path), required=False)
    return Check(
        "config", ok=True, detail="using defaults (no .milhouse/config.toml)", required=False
    )


def _parse_status(text: str) -> dict[str, str]:
    """Flatten ``herdr status``'s indented ``key: value`` output into one mapping.

    Later sections win on duplicate keys, which is what the caller wants: the
    ``server:`` block appears after ``client:``, so ``protocol`` ends up being
    the server's.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value:
            fields[key.strip()] = value
    return fields


def _first_line(text: str) -> str:
    """First non-blank line of ``text``, or the empty string."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""
