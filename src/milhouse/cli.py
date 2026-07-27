"""The ``milhouse`` command line.

Four commands: ``step`` works one issue, ``plan`` stops after decomposition,
``status`` reports on a task, and ``doctor`` checks the tools milhouse depends
on. Every command and flag carries help text, so ``milhouse --help`` is a usable
reference on its own.

There is no command that repeats a step. Driving it by hand is the point for now
(:doc:`ADR 0017 <../../docs/decisions/0017-no-loop-until-it-is-earned>`).

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

from . import completion, prompts, sources
from . import doctor as doctor_checks
from .config import Config, load
from .errors import MilhouseError
from .gitrepo import GitRepo, find_repo_root
from .herdr import HerdrClient
from .models import TaskDefinition
from .session import Session
from .state import RunStore
from .step import nothing_ready
from .step import step as run_step
from .tracker import BeadsTracker

__all__ = ["app", "main"]

app = typer.Typer(
    name="milhouse",
    help=(
        "Decompose a task into tracked issues, then work them one step at a time.\n\n"
        "milhouse resolves a task definition, asks a planning agent to break it into "
        "beads issues, then works one of them per `milhouse step`, each with a fresh "
        "agent running in a herdr pane."
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
def step(
    task: Annotated[
        str,
        typer.Argument(help=TASK_HELP, autocompletion=completion.complete_task),
    ],
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
        typer.Option(
            "--workspace",
            help="Reuse this herdr workspace instead of creating one.",
            autocompletion=completion.complete_workspace,
        ),
    ] = None,
    branch_strategy: Annotated[
        str | None,
        typer.Option(
            "--branch-strategy",
            help="task creates one branch per task definition; current stays where you are.",
            autocompletion=completion.complete_branch_strategy,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Render the prompt and print the plan; start no agent."),
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
        typer.Option(
            "--repo",
            help="Repository to work in. Defaults to the current one.",
            autocompletion=completion.complete_repo,
        ),
    ] = None,
) -> None:
    """Work one ready issue with a fresh agent, report what happened, and stop.

    The way milhouse is driven. It decomposes the task if that has not happened
    yet, claims one ready issue, gives it to a freshly started agent, and hands
    straight back to you. You decide whether to go again.

    Stepping again is also how you resume: any claim a previous step left behind
    is re-opened first.

    Exits 0 when the issue was finished, and 9 when it was not.
    """
    config = _config(
        repo,
        {
            "agent": {"kind": agent},
            "git": {"branch_strategy": branch_strategy},
            "herdr": {"workspace": workspace},
        },
    )
    definition = sources.resolve(task, config.repo_root)

    if dry_run:
        _dry_run(config, definition)
        return

    with _session(config, definition, attach=attach) as session:
        epic = session.ensure_epic(confirm=None if yes else _confirm_plan)
        outcome = run_step(session, epic)
        if outcome is None:
            reason, completed = nothing_ready(session, epic)
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
def plan(
    task: Annotated[
        str,
        typer.Argument(help=TASK_HELP, autocompletion=completion.complete_task),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Create the proposed issues without asking."),
    ] = False,
    workspace: Annotated[
        str | None,
        typer.Option(
            "--workspace",
            help="Reuse this herdr workspace instead of creating one.",
            autocompletion=completion.complete_workspace,
        ),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="Agent kind to run, e.g. claude, codex, gemini.",
            autocompletion=completion.complete_agent,
        ),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Repository to work in. Defaults to the current one.",
            autocompletion=completion.complete_repo,
        ),
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

    with _session(config, definition, tracker=tracker) as session:
        epic = session.ensure_epic(confirm=None if yes else _confirm_plan)
    _print_tree(tracker, epic)


@app.command()
def status(
    task: Annotated[
        str,
        typer.Argument(help=TASK_HELP, autocompletion=completion.complete_task),
    ],
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Repository to inspect. Defaults to the current one.",
            autocompletion=completion.complete_repo,
        ),
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

    store = RunStore(config.run_dir(definition.slug))
    state = store.load()
    if state:
        typer.echo(f"branch  {state.branch or '(none)'}")
        if state.workspace_id:
            typer.echo(f"herdr   workspace {state.workspace_id}, pane {state.pane_id}")
        if state.claimed_issue:
            typer.secho(
                f"claim   {state.claimed_issue} is claimed by an unfinished run",
                fg=typer.colors.YELLOW,
            )
    holder = store.lock.holder()
    if holder is not None:
        typer.secho(f"lock    held by {holder.describe()}", fg=typer.colors.YELLOW)

    if epic is not None:
        typer.echo("")
        _print_tree(tracker, epic)

    history = store.history()
    if history:
        typer.echo("")
        typer.echo(f"iterations ({len(history)})")
        for item in history:
            colour = _OUTCOME_COLOURS.get(item.outcome, typer.colors.WHITE)
            mark = typer.style(item.outcome.ljust(8), fg=colour)
            typer.echo(f"  {item.number:>3}  {mark}  {item.issue_id}  {item.detail}")


TASK_HELP = "Task definition: a markdown file path, or gh:owner/repo#123."

_OUTCOME_COLOURS = {
    "success": typer.colors.GREEN,
    "rejected": typer.colors.RED,
    "blocked": typer.colors.YELLOW,
    "partial": typer.colors.CYAN,
    "stalled": typer.colors.YELLOW,
    "timeout": typer.colors.YELLOW,
    "error": typer.colors.RED,
}


def _session(
    config: Config,
    definition: TaskDefinition,
    *,
    tracker: BeadsTracker | None = None,
    attach: bool = False,
) -> Session:
    """Assemble a :class:`~milhouse.session.Session` from resolved configuration."""
    return Session(
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

    typer.secho("dry run — no agent will be started", fg=typer.colors.CYAN)
    typer.echo(f"task      {definition.task_id}")
    typer.echo(f"title     {definition.title}")
    branch = (
        f"{config.git.branch_prefix}{definition.slug}"
        if config.git.branch_strategy == "task"
        else GitRepo(config.repo_root).current_branch() or "(detached)"
    )
    typer.echo(f"branch    {branch}")
    typer.echo(f"agent     {config.agent.kind} {' '.join(config.agent.args)}".rstrip())
    verify = " ".join(config.verify.command) or "(none — a closed issue is taken on trust)"
    typer.echo(f"verify    {verify}")
    typer.echo(f"run dir   {config.run_dir(definition.slug)}")

    if epic is None:
        plan_path = config.run_dir(definition.slug) / "plan.json"
        typer.echo("\nnot decomposed yet; the planning agent would be sent:\n")
        typer.echo(_indent(prompts.render_plan(definition, plan_path=str(plan_path))))
        return

    typer.echo(f"\nepic      {epic.id}  {epic.title}")
    next_issue = tracker.ready(epic.id, claim=False)
    if next_issue is None:
        typer.echo("\nno issues are ready; a step would do nothing")
        return
    typer.echo(f"\nthe next step would work {next_issue.id} and send:\n")
    typer.echo(_indent(prompts.render_iterate(definition, next_issue, branch=branch)))


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
