"""The ``milhouse`` command line.

Three commands: ``step`` works one issue, ``status`` reports on the repository,
and ``doctor`` checks the tools milhouse depends on. Every command and flag
carries help text, so ``milhouse --help`` is a usable reference on its own.

There is no command that repeats a step. Driving it by hand is the point for now
(:doc:`ADR 0017 <../../docs/decisions/0017-no-loop-until-it-is-earned>`). There is
no command that creates issues either: getting work into the tracker belongs to
whoever owns the tracker
(:doc:`ADR 0018 <../../docs/decisions/0018-no-task-milhouse-works-the-ready-queue>`).

This module owns argument parsing and output formatting only. The behaviour
lives in :mod:`milhouse.step`, :mod:`milhouse.session`, and their collaborators,
so it stays testable without a terminal. It reaches for no private attribute of
any of them: everything the CLI needs is on
:class:`~milhouse.session.Session`.
"""

from __future__ import annotations

import logging
import sys
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from . import completion, prompts
from . import doctor as doctor_checks
from .audit import AuditLog
from .config import Config, load
from .errors import MilhouseError
from .gitrepo import GitRepo, find_repo_root
from .herdr import HerdrClient
from .lanes import Lanes
from .models import Issue
from .rundir import LOCK_FILENAME, RunLock
from .session import Session
from .step import nothing_ready
from .step import step as run_step
from .tracker import BeadsTracker

__all__ = ["app", "main"]

app = typer.Typer(
    name="milhouse",
    help=(
        "Work a tracker's ready queue one issue at a time.\n\n"
        "milhouse claims the next ready beads issue and gives it to a freshly "
        "started agent in a herdr pane, one issue per `milhouse step`. Getting "
        "issues into the tracker is your job, not milhouse's."
    ),
    no_args_is_help=True,
    # Gives --install-completion and --show-completion. The values each
    # parameter offers come from milhouse.completion.
    add_completion=True,
    # Click passes this down to every subcommand's context.
    context_settings={"help_option_names": ["-h", "--help"]},
)


class _MilhouseGroup(TyperGroup):
    """Turns a :class:`~milhouse.errors.MilhouseError` into its documented exit code.

    The mapping lives here rather than in :func:`main` so it applies however the
    app is invoked — including from tests, which call the app object directly
    rather than going through the console script.
    """

    def invoke(self, ctx: Any) -> Any:
        """Run the command, reporting an expected failure instead of raising it."""
        try:
            return super().invoke(ctx)
        except MilhouseError as exc:
            typer.secho(f"milhouse: {exc}", fg=typer.colors.RED, err=True)
            if exc.remedy:
                typer.secho(f"  {exc.remedy}", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(code=exc.exit_code) from exc


app.info.cls = _MilhouseGroup


def _version_callback(value: bool) -> None:
    """Print the version and exit, for ``--version``."""
    if value:
        typer.echo(f"milhouse {package_version('milhouse')}")
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
        typer.Option(
            "--repo",
            help="Repository to check. Defaults to the current one.",
            autocompletion=completion.complete_repo,
        ),
    ] = None,
) -> None:
    """Verify the tools milhouse depends on and the herdr server's state.

    Checks ``bd``, ``herdr``, ``git``, and the configured agent, reports each
    version, and confirms the herdr server is running and protocol-compatible.
    Exits non-zero if any required check fails.
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


@app.command()
def step(
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="Agent kind to run, e.g. claude, codex, gemini.",
            autocompletion=completion.complete_agent,
        ),
    ] = None,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Reuse this herdr workspace instead of creating one."),
    ] = None,
    parent: Annotated[
        str | None,
        typer.Option("--parent", help="Only work issues under this epic."),
    ] = None,
    label: Annotated[
        str | None,
        typer.Option("--label", help="Only work issues carrying this label."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Render the prompt and print the plan; start no agent."),
    ] = False,
    attach: Annotated[
        bool,
        typer.Option("--attach", help="Focus the herdr workspace instead of leaving it hidden."),
    ] = False,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Repository to work in. Defaults to the current one.",
            autocompletion=completion.complete_repo,
        ),
    ] = None,
) -> None:
    """Work the next ready issue with a fresh agent, report what happened, and stop.

    The way milhouse is driven. It claims one ready issue, gives it to a freshly
    started agent, and hands straight back to you. You decide whether to go
    again.

    Stepping again is also how you resume: any claim a previous step left behind
    is re-opened first.

    Exits 0 when the issue was finished, and 9 when it was not.
    """
    config = _config(
        repo,
        {
            "agent": {"kind": agent},
            "herdr": {"workspace": workspace},
            "tracker": {"parent": parent, "label": label},
        },
    )

    if dry_run:
        _dry_run(config)
        return

    with _session(config, attach=attach) as session:
        outcome = run_step(session)
        if outcome is None:
            reason, completed = nothing_ready(session)
            typer.echo("")
            typer.secho(reason, fg=typer.colors.GREEN if completed else typer.colors.YELLOW)
            if not completed:
                raise typer.Exit(code=9)
            return
        if outcome.decision.reason:
            typer.echo(f"  {outcome.decision.reason}")

    typer.echo("")
    finished = outcome.iteration.outcome == "success"
    typer.secho(
        f"{outcome.iteration.issue_id}: {outcome.iteration.outcome} — {outcome.iteration.detail}",
        fg=typer.colors.GREEN if finished else typer.colors.YELLOW,
    )
    if not finished:
        raise typer.Exit(code=9)


@app.command()
def status(
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Repository to inspect. Defaults to the current one.",
            autocompletion=completion.complete_repo,
        ),
    ] = None,
) -> None:
    """Show what is in scope, what is claimed, and this repository's iteration history.

    Reads beads, herdr, and git; starts nothing and changes nothing.
    """
    config = _config(repo)
    tracker = BeadsTracker(config.repo_root, config.tracker)
    audit = AuditLog(config.repo_root)
    client = HerdrClient(cwd=config.repo_root)
    label = f"milhouse:{config.repo_root.name}"

    typer.echo(f"repo    {config.repo_root}")
    typer.echo(f"scope   {_scope(config)}")
    typer.echo(f"branch  {GitRepo(config.repo_root).current_branch() or '(detached)'}")
    configured = config.herdr.workspace
    workspace = configured or client.find_workspace(label)
    if workspace:
        source = "configured" if configured else f"labelled {label}"
        typer.echo(f"herdr   workspace {workspace} ({source})")
    for issue_id in audit.unsettled_claims():
        typer.secho(f"claim   {issue_id} is claimed by an unfinished run", fg=typer.colors.YELLOW)
    holder = RunLock(config.run_dir() / LOCK_FILENAME).holder()
    if holder is not None:
        typer.secho(f"lock    held by {holder.describe()}", fg=typer.colors.YELLOW)

    typer.echo("")
    _print_tree(tracker)
    _print_lanes(client, config)

    history = audit.iterations()
    if history:
        typer.echo("")
        typer.echo(f"iterations ({len(history)})")
        for item in history:
            colour = _OUTCOME_COLOURS.get(item.outcome, typer.colors.WHITE)
            mark = typer.style(item.outcome.ljust(8), fg=colour)
            typer.echo(f"  {item.number:>3}  {mark}  {item.issue_id}  {item.detail}")


_OUTCOME_COLOURS = {
    "success": typer.colors.GREEN,
    "rejected": typer.colors.RED,
    "blocked": typer.colors.YELLOW,
    "partial": typer.colors.CYAN,
    "stalled": typer.colors.YELLOW,
    "timeout": typer.colors.YELLOW,
    "error": typer.colors.RED,
}


def _scope(config: Config) -> str:
    """One line naming the ready-queue filter, for ``status`` and ``--dry-run``."""
    parts = []
    if config.tracker.parent:
        parts.append(f"under {config.tracker.parent}")
    if config.tracker.label:
        parts.append(f"labelled {config.tracker.label}")
    return ", ".join(parts) or "every ready issue in the repository"


def _session(
    config: Config,
    *,
    tracker: BeadsTracker | None = None,
    attach: bool = False,
) -> Session:
    """Assemble a :class:`~milhouse.session.Session` from resolved configuration."""
    return Session(
        config,
        tracker=tracker or BeadsTracker(config.repo_root, config.tracker),
        client=HerdrClient(cwd=config.repo_root),
        repo=GitRepo(config.repo_root),
        report=typer.echo,
        attach=attach,
    )


def _dry_run(config: Config) -> None:
    """Print what a step would do, and the prompt it would send, without doing it."""
    tracker = BeadsTracker(config.repo_root, config.tracker)
    repo = GitRepo(config.repo_root)

    typer.secho("dry run — no agent will be started", fg=typer.colors.CYAN)
    typer.echo(f"scope     {_scope(config)}")
    branch = repo.current_branch() or "(detached)"
    typer.echo(f"branch    {branch}")
    typer.echo(f"agent     {config.agent.kind} {' '.join(config.agent.args)}".rstrip())
    verify = " ".join(config.verify.command) or "(none — a closed issue is taken on trust)"
    typer.echo(f"verify    {verify}")
    typer.echo(f"run dir   {config.run_dir()}")

    next_issue = tracker.ready(claim=False)
    if next_issue is None:
        typer.echo("\nno issues are ready; a step would do nothing")
        return
    next_issue = tracker.get(next_issue.id)
    background = tracker.get(next_issue.parent).description if next_issue.parent else ""
    lane_branch, note = _lane_branch(config, next_issue)
    typer.echo(f"lane      {lane_branch}  ({note})")
    typer.echo(f"\nthe next step would work {next_issue.id} and send:\n")
    typer.echo(
        _indent(prompts.render_iterate(next_issue, background=background, branch=lane_branch))
    )


def _lane_branch(config: Config, issue: Issue) -> tuple[str, str]:
    """Which branch the next step would commit to, and why, opening nothing.

    An issue already in a lane, or whose blocker is in one, continues on that
    lane's branch. Anything else gets a new one
    (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).
    """
    fresh = f"{config.lane.branch_prefix}{issue.id}"
    try:
        held = {
            lane.issue_id: lane
            for lane in Lanes(HerdrClient(cwd=config.repo_root), config).registry()
        }
    except MilhouseError as exc:
        return fresh, f"herdr unavailable, so this is a guess: {exc}"
    for issue_id in (issue.id, *issue.blocked_by):
        lane = held.get(issue_id)
        if lane is not None:
            return lane.branch, f"the existing lane for {issue_id}"
    return fresh, "a new lane"


def _print_lanes(client: HerdrClient, config: Config) -> None:
    """Print the lanes herdr is holding for this repository, if any.

    milhouse keeps no lane state, so this is a read of ``herdr worktree list``
    joined to the workspace labels
    (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).
    """
    try:
        lanes = Lanes(client, config).registry()
    except MilhouseError as exc:
        typer.secho(f"\nlanes   unavailable: {exc}", fg=typer.colors.YELLOW)
        return
    if not lanes:
        return
    typer.echo("")
    typer.echo(f"lanes ({len(lanes)})")
    for lane in lanes:
        held = lane.issue_id or "(no workspace holds it)"
        typer.echo(f"  {held}  {lane.branch}  {lane.path}")


def _print_tree(tracker: BeadsTracker) -> None:
    """Print the issues in scope with their status."""
    issues = tracker.children()
    if not issues:
        typer.echo("  (no issues)")
        return
    for issue in issues:
        colour = typer.colors.GREEN if issue.is_closed else typer.colors.WHITE
        mark = "x" if issue.is_closed else " "
        typer.echo(
            f"  [{mark}] {typer.style(issue.id, fg=colour)}  {issue.title}  ({issue.status})"
        )


def _indent(text: str) -> str:
    """Indent a block so a rendered prompt is visibly quoted."""
    return "\n".join(f"    {line}" if line else "" for line in text.splitlines())


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
        # Normally handled by _MilhouseGroup; this catches anything raised
        # outside a command, such as while parsing arguments.
        typer.secho(f"milhouse: {exc}", fg=typer.colors.RED, err=True)
        if exc.remedy:
            typer.secho(f"  {exc.remedy}", fg=typer.colors.YELLOW, err=True)
        raise SystemExit(exc.exit_code) from exc
    except KeyboardInterrupt:
        typer.secho("milhouse: interrupted", fg=typer.colors.YELLOW, err=True)
        raise SystemExit(130) from None
