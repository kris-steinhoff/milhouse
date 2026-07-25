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
from typer.core import TyperGroup

from . import __version__, prompts, sources
from . import doctor as doctor_checks
from .config import Config, load
from .errors import MilhouseError
from .gitrepo import GitRepo, find_repo_root
from .herdr import HerdrClient
from .loop import RalphLoop
from .models import RunState, TaskDefinition
from .tracker import BeadsTracker

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


@app.command()
def run(
    task: Annotated[str, typer.Argument(help=TASK_HELP)],
    max_iterations: Annotated[
        int | None,
        typer.Option("--max-iterations", help="Hard ceiling on iterations for the whole run."),
    ] = None,
    max_attempts: Annotated[
        int | None,
        typer.Option("--max-attempts", help="Failed attempts on one issue before it is skipped."),
    ] = None,
    on_blocked: Annotated[
        str | None,
        typer.Option(
            "--on-blocked",
            help="What to do when the agent waits on a human: wait, skip, or abort.",
        ),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent kind to run, e.g. claude, codex, gemini."),
    ] = None,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Reuse this herdr workspace instead of creating one."),
    ] = None,
    branch_strategy: Annotated[
        str | None,
        typer.Option(
            "--branch-strategy",
            help="task creates one branch per task definition; current stays where you are.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Render the prompts and print the plan; start no agents."),
    ] = False,
    attach: Annotated[
        bool,
        typer.Option("--attach", help="Focus the herdr workspace instead of leaving it hidden."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Create the proposed issues without asking."),
    ] = False,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repository to work in. Defaults to the current one."),
    ] = None,
) -> None:
    """Resolve a task, decompose it if needed, then loop until the work is done.

    The main entry point. Each iteration claims one ready issue and gives it to a
    freshly started agent, so every iteration begins with a clean context window.

    Re-running against the same task resumes it: any claim a previous run left
    behind is re-opened, and the attempt counts carry over.
    """
    config = _config(
        repo,
        {
            "loop": {
                "max_iterations": max_iterations,
                "max_attempts": max_attempts,
                "on_blocked": on_blocked,
            },
            "agent": {"kind": agent},
            "git": {"branch_strategy": branch_strategy},
            "herdr": {"workspace": workspace},
        },
    )
    definition = sources.resolve(task, config.repo_root)

    if dry_run:
        _dry_run(config, definition)
        return

    loop = _loop(config, definition, attach=attach)
    result = loop.run(confirm=None if yes else _confirm_plan)
    typer.echo("")
    typer.secho(
        _summarise(result), fg=typer.colors.GREEN if result.completed else typer.colors.YELLOW
    )
    if not result.completed:
        raise typer.Exit(code=9)


@app.command()
def plan(
    task: Annotated[str, typer.Argument(help=TASK_HELP)],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Create the proposed issues without asking."),
    ] = False,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Reuse this herdr workspace instead of creating one."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent kind to run, e.g. claude, codex, gemini."),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repository to work in. Defaults to the current one."),
    ] = None,
) -> None:
    """Decompose a task into issues, print the tree, and stop.

    Runs the planning agent, shows what it proposes, creates the issues once you
    approve, and does not start the loop. Re-running against a task that already
    has an epic prints the existing tree instead of planning it again.
    """
    config = _config(repo, {"agent": {"kind": agent}, "herdr": {"workspace": workspace}})
    definition = sources.resolve(task, config.repo_root)
    tracker = BeadsTracker(config.repo_root, config.tracker)

    existing = tracker.find_epic(definition)
    if existing is not None:
        typer.echo(f"{definition.task_id} is already decomposed as {existing.id}.")
        _print_tree(tracker, existing)
        return

    loop = _loop(config, definition, tracker=tracker)
    state = loop.state()
    loop._prepare_branch(state)
    loop._open_workspace(state)
    state.save(loop.state_path)
    epic = loop._ensure_epic(state, confirm=None if yes else _confirm_plan)
    _print_tree(tracker, epic)


@app.command()
def status(
    task: Annotated[str, typer.Argument(help=TASK_HELP)],
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repository to inspect. Defaults to the current one."),
    ] = None,
) -> None:
    """Show a task's issue tree and this run's iteration history.

    Reads beads and the run state; starts nothing and changes nothing.
    """
    config = _config(repo)
    definition = sources.resolve(task, config.repo_root)
    tracker = BeadsTracker(config.repo_root, config.tracker)

    typer.echo(f"task    {definition.task_id}")
    epic = tracker.find_epic(definition)
    if epic is None:
        typer.echo("epic    (not decomposed yet — run `milhouse plan`)")
    else:
        typer.echo(f"epic    {epic.id}  {epic.title}")

    state = RunState.load(config.run_dir(definition.slug) / "state.json")
    if state:
        typer.echo(f"branch  {state.branch or '(none)'}")
        if state.workspace_id:
            typer.echo(f"herdr   workspace {state.workspace_id}, pane {state.pane_id}")
        if state.claimed_issue:
            typer.secho(
                f"claim   {state.claimed_issue} is claimed by an unfinished run",
                fg=typer.colors.YELLOW,
            )

    if epic is not None:
        typer.echo("")
        _print_tree(tracker, epic)

    if state and state.iterations:
        typer.echo("")
        typer.echo(f"iterations ({len(state.iterations)})")
        for item in state.iterations:
            colour = _OUTCOME_COLOURS.get(item.outcome, typer.colors.WHITE)
            mark = typer.style(item.outcome.ljust(7), fg=colour)
            typer.echo(f"  {item.number:>3}  {mark}  {item.issue_id}  {item.detail}")


TASK_HELP = "Task definition: a markdown file path, or gh:owner/repo#123."

_OUTCOME_COLOURS = {
    "success": typer.colors.GREEN,
    "blocked": typer.colors.YELLOW,
    "partial": typer.colors.CYAN,
    "stalled": typer.colors.YELLOW,
    "timeout": typer.colors.YELLOW,
    "error": typer.colors.RED,
}


def _loop(
    config: Config,
    definition: TaskDefinition,
    *,
    tracker: BeadsTracker | None = None,
    attach: bool = False,
) -> RalphLoop:
    """Assemble a :class:`~milhouse.loop.RalphLoop` from resolved configuration."""
    return RalphLoop(
        config,
        definition,
        tracker=tracker or BeadsTracker(config.repo_root, config.tracker),
        client=HerdrClient(cwd=config.repo_root),
        repo=GitRepo(config.repo_root),
        report=typer.echo,
        attach=attach,
    )


def _dry_run(config: Config, definition: TaskDefinition) -> None:
    """Print what a run would do, and the prompts it would send, without doing it."""
    tracker = BeadsTracker(config.repo_root, config.tracker)
    epic = tracker.find_epic(definition)

    typer.secho("dry run — no agents will be started", fg=typer.colors.CYAN)
    typer.echo(f"task      {definition.task_id}")
    typer.echo(f"title     {definition.title}")
    branch = (
        f"{config.git.branch_prefix}{definition.slug}"
        if config.git.branch_strategy == "task"
        else GitRepo(config.repo_root).current_branch() or "(detached)"
    )
    typer.echo(f"branch    {branch}")
    typer.echo(f"agent     {config.agent.kind} {' '.join(config.agent.args)}".rstrip())
    typer.echo(
        f"caps      {config.loop.max_iterations} iterations, "
        f"{config.loop.max_attempts} attempts per issue, "
        f"on-blocked {config.loop.on_blocked}"
    )
    typer.echo(f"run dir   {config.run_dir(definition.slug)}")

    if epic is None:
        plan_path = config.run_dir(definition.slug) / "plan.json"
        typer.echo("\nnot decomposed yet; the planning agent would be sent:\n")
        typer.echo(_indent(prompts.render_plan(definition, plan_path=str(plan_path))))
        return

    typer.echo(f"\nepic      {epic.id}  {epic.title}")
    next_issue = tracker.ready(epic.id, claim=False)
    if next_issue is None:
        typer.echo("\nno issues are ready; a run would finish immediately")
        return
    typer.echo(f"\nthe next iteration would work {next_issue.id} and send:\n")
    typer.echo(
        _indent(
            prompts.render_iterate(
                definition,
                next_issue,
                branch=branch,
                attempts_left=config.loop.max_attempts,
            )
        )
    )


def _confirm_plan(proposal: Any) -> bool:
    """Show a proposed decomposition and ask whether to create it."""
    typer.echo("\nthe planning agent proposes:\n")
    typer.echo(proposal.render_tree())
    typer.echo("")
    return typer.confirm(f"create these {len(proposal.issues)} issues?", default=True)


def _print_tree(tracker: BeadsTracker, epic: Any) -> None:
    """Print an epic's children with their status."""
    children = tracker.children(epic.id)
    if not children:
        typer.echo("  (no issues)")
        return
    for child in children:
        colour = typer.colors.GREEN if child.is_closed else typer.colors.WHITE
        mark = "x" if child.is_closed else " "
        typer.echo(
            f"  [{mark}] {typer.style(child.id, fg=colour)}  {child.title}  ({child.status})"
        )


def _summarise(result: Any) -> str:
    """One line describing how a run ended."""
    verb = "finished" if result.completed else "stopped"
    return f"{verb} after {result.iterations} iterations: {result.reason}"


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
