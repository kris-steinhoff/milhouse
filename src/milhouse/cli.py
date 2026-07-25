"""The ``milhouse`` command line.

Four commands: ``run`` drives the whole loop, ``plan`` stops after
decomposition, ``status`` reports on a task, and ``doctor`` checks the tools
milhouse depends on. Every command and flag carries help text, so
``milhouse --help`` is a usable reference on its own.

This module owns argument parsing and output formatting only. The behaviour
lives in :mod:`milhouse.loop`, :mod:`milhouse.planner`, and their collaborators,
so it stays testable without a terminal.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__
from . import doctor as doctor_checks
from .config import Config, load
from .errors import MilhouseError
from .gitrepo import find_repo_root

__all__ = ["app", "main"]

app = typer.Typer(
    name="milhouse",
    help=(
        "Decompose a task into tracked issues, then drive a ralph loop over them.\n\n"
        "milhouse resolves a task definition, asks a planning agent to break it into "
        "beads issues, then repeatedly claims one ready issue and gives it to a fresh "
        "agent running in a herdr pane."
    ),
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    """Print the version and exit, for ``--version``."""
    if value:
        typer.echo(f"milhouse {__version__}")
        raise typer.Exit()


@app.callback()
def main_options(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the milhouse version and exit.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Log every subprocess milhouse runs."),
    ] = False,
) -> None:
    """Global options that apply to every subcommand."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


@app.command()
def doctor(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repository to check. Defaults to the current one."),
    ] = None,
) -> None:
    """Verify the tools milhouse depends on and the herdr server's state.

    Checks ``bd``, ``herdr``, ``git``, ``gh``, and the configured agent, reports
    each version, and confirms the herdr server is running and protocol-
    compatible. Exits non-zero if any required check fails.
    """
    config = _config(repo)
    checks = doctor_checks.run_checks(config)
    width = max(len(check.name) for check in checks)
    failed = []
    for check in checks:
        if check.ok:
            mark, style = "ok  ", typer.colors.GREEN
        elif check.required:
            mark, style = "FAIL", typer.colors.RED
            failed.append(check)
        else:
            mark, style = "warn", typer.colors.YELLOW
        typer.echo(f"{typer.style(mark, fg=style)} {check.name.ljust(width)}  {check.detail}")
    if failed:
        names = ", ".join(check.name for check in failed)
        typer.echo("")
        typer.secho(f"required checks failed: {names}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=7)


def _config(repo: Path | None, overrides: dict[str, Any] | None = None) -> Config:
    """Resolve configuration for ``repo``, defaulting to the enclosing repository.

    Args:
        repo: Explicit repository root, or ``None`` to discover one.
        overrides: Nested CLI flag overrides passed through to :func:`config.load`.

    Returns:
        The resolved configuration.
    """
    root = find_repo_root(repo)
    return load(root, overrides=overrides)


def main() -> None:
    """Console-script entry point: run the app and map errors to exit codes.

    Any :class:`~milhouse.errors.MilhouseError` is reported as a single line on
    stderr, with the error's remedy when it has one, and exits with the error's
    documented code. Anything else propagates as a traceback, because it is a bug.
    """
    try:
        app()
    except MilhouseError as exc:
        typer.secho(f"milhouse: {exc}", fg=typer.colors.RED, err=True)
        if exc.remedy:
            typer.secho(f"  {exc.remedy}", fg=typer.colors.YELLOW, err=True)
        raise SystemExit(exc.exit_code) from exc
    except KeyboardInterrupt:
        typer.secho("milhouse: interrupted", fg=typer.colors.YELLOW, err=True)
        raise SystemExit(130) from None
