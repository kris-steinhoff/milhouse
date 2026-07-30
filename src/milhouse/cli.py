"""The ``milhouse`` command line.

Six commands. ``step`` works one issue and waits, ``dispatch`` and ``reap`` are
that same turn split in two so several can be in flight at once, ``run`` repeats
it until a target is finished, ``status`` reports on the repository, and
``doctor`` checks the tools milhouse depends on. Every command and flag carries
help text, so ``milhouse --help`` is a usable reference on its own.

``run`` is the only one that repeats a turn, and what it takes is a beads id
rather than a task definition
(:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`). There is still
no command that creates issues: getting work into the tracker belongs to whoever
owns the tracker
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
from .lanes import Lane, Lanes
from .models import Issue, Iteration
from .policy import unattended
from .run import RunResult
from .run import run as run_loop
from .rundir import LOCK_FILENAME, RunLock
from .scope import Scope
from .scope import resolve as resolve_target
from .session import Session, usable_workspace
from .step import dispatch as run_dispatch
from .step import merge_line, nothing_ready
from .step import reap as run_reap
from .step import step as run_step
from .tracker import BeadsTracker

__all__ = ["app", "main"]

app = typer.Typer(
    name="milhouse",
    help=(
        "Work a tracker's ready queue with fresh agents, one issue at a time.\n\n"
        "`milhouse step` claims the next ready beads issue and gives it to a "
        "freshly started agent in a herdr pane, then hands back to you. "
        "`milhouse run <target>` repeats that until a beads epic or issue is "
        "finished. Getting issues into the tracker is your job, not milhouse's."
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
def run(
    target: Annotated[
        str,
        typer.Argument(
            metavar="TARGET",
            help="Beads id to work towards: an epic, or a single issue.",
            show_default=False,
        ),
    ],
    max_iterations: Annotated[
        int | None,
        typer.Option("--max-iterations", min=1, help="Turns this run may take. Default 50."),
    ] = None,
    max_attempts: Annotated[
        int | None,
        typer.Option(
            "--max-attempts",
            min=1,
            help="Attempts one issue gets before it is deferred. Default 3.",
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
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Reuse this herdr workspace instead of creating one."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show the scope and the first prompt; start no agent."),
    ] = False,
    attach: Annotated[
        bool,
        typer.Option("--attach", help="Focus the lane instead of leaving it hidden."),
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
    """Work one target to completion, a fresh agent per issue, and report.

    TARGET is a beads id. An epic means "work everything under it"; a single
    issue means "work it and whatever it is blocked by". The whole run happens
    in one lane, so it lands on one branch you can review as a piece.

    It stops when the target is finished, when nothing is ready but work is
    left, when an agent needs a human, when milhouse itself fails, or at
    `--max-iterations`. An issue that fails `--max-attempts` times is deferred
    with the reason on it and the run carries on.

    There is no `--parent` or `--label`: the target is the scope.

    Exits 0 when the target finished, and 9 when the run stopped short.
    """
    config = _config(
        repo,
        {
            "agent": {"kind": agent},
            "herdr": {"workspace": workspace},
            "run": {"max_iterations": max_iterations, "max_attempts": max_attempts},
        },
    )
    scope = resolve_target(target, repo_root=config.repo_root, config=config.tracker)

    if dry_run:
        _dry_run(config, scope=scope)
        return

    typer.echo(f"target  {scope.target.id}  {scope.target.title}")
    typer.echo(f"scope   {scope.describe()}")
    session = _session(config, tracker=scope.tracker, attach=attach, lane_key=scope.target.id)
    with session as opened:
        result = run_loop(
            opened,
            scope.target,
            policy=unattended(max_attempts=config.run.max_attempts),
            max_iterations=config.run.max_iterations,
        )
        located = opened.lanes.locate(scope.target.id)

    _print_run(result, lane=located[0] if located else None)
    if not result.finished:
        raise typer.Exit(code=9)


@app.command()
def dispatch(
    count: Annotated[
        int,
        typer.Option("--count", "-n", min=1, help="How many ready issues to start at most."),
    ] = 1,
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
    attach: Annotated[
        bool,
        typer.Option("--attach", help="Focus each lane instead of leaving it hidden."),
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
    """Start agents on up to N ready issues and return without waiting.

    Each issue gets a lane of its own, so the turns run at the same time. Use
    `milhouse reap` to collect them when they settle.

    This is not a loop: it starts a bounded number of turns once, and stops.

    Exits 0 when at least one turn was started, and 9 when nothing was ready.
    """
    config = _config(
        repo,
        {
            "agent": {"kind": agent},
            "herdr": {"workspace": workspace},
            "tracker": {"parent": parent, "label": label},
        },
    )
    with _session(config, attach=attach) as session:
        started = run_dispatch(session, limit=count)
        if not started:
            reason, completed = nothing_ready(session)
            typer.echo("")
            typer.secho(reason, fg=typer.colors.GREEN if completed else typer.colors.YELLOW)
            raise typer.Exit(code=0 if completed else 9)

    typer.echo("")
    typer.secho(f"{len(started)} turn(s) in flight:", fg=typer.colors.GREEN)
    for pending in started:
        typer.echo(f"  {pending.issue.id}  {pending.lane.branch}  {pending.lane.path}")
    typer.echo("\nrun `milhouse reap` when they settle.")


@app.command()
def reap(
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Repository to work in. Defaults to the current one.",
            autocompletion=completion.complete_repo,
        ),
    ] = None,
) -> None:
    """Collect every dispatched turn whose agent has settled, and classify it.

    Turns still working are left alone; reap again later. Reaping is safe to run
    at any time and does nothing when there is nothing to collect.

    Exits 0 when everything collected succeeded and nothing is left running, and
    9 otherwise.
    """
    config = _config(repo)
    with _session(config) as session:
        results = run_reap(session)
        outstanding = len(session.audit.dispatches())

    typer.echo("")
    if not results:
        typer.secho(
            f"nothing settled; {outstanding} turn(s) still running"
            if outstanding
            else "nothing to reap",
            fg=typer.colors.YELLOW if outstanding else typer.colors.GREEN,
        )
        if outstanding:
            raise typer.Exit(code=9)
        return

    for result in results:
        colour = _OUTCOME_COLOURS.get(result.iteration.outcome, typer.colors.WHITE)
        typer.secho(
            f"{result.iteration.issue_id}: {result.iteration.outcome} — {result.iteration.detail}",
            fg=colour,
        )
    if outstanding:
        typer.echo(f"\n{outstanding} turn(s) still running.")
    finished = all(result.iteration.outcome == "success" for result in results)
    if not finished or outstanding:
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
    configured, refusal = usable_workspace(client, config.herdr.workspace, config.repo_root)
    if refusal:
        typer.secho(f"herdr   {refusal}", fg=typer.colors.YELLOW)
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
    lane_key: str | None = None,
) -> Session:
    """Assemble a :class:`~milhouse.session.Session` from resolved configuration."""
    return Session(
        config,
        tracker=tracker or BeadsTracker(config.repo_root, config.tracker),
        client=HerdrClient(cwd=config.repo_root),
        repo=GitRepo(config.repo_root),
        report=typer.echo,
        attach=attach,
        lane_key=lane_key,
    )


def _dry_run(config: Config, *, scope: Scope | None = None) -> None:
    """Print what would happen, and the prompt that would be sent, without doing it.

    Shared by ``step`` and ``run``, which differ in two lines: what fences the
    queue, and which branch the work would land on.
    """
    tracker = scope.tracker if scope else BeadsTracker(config.repo_root, config.tracker)
    repo = GitRepo(config.repo_root)

    typer.secho("dry run — no agent will be started", fg=typer.colors.CYAN)
    if scope is not None:
        typer.echo(f"target    {scope.target.id}  {scope.target.title}")
    typer.echo(f"scope     {scope.describe() if scope else _scope(config)}")
    branch = repo.current_branch() or "(detached)"
    typer.echo(f"branch    {branch}")
    typer.echo(f"agent     {config.agent.kind} {' '.join(config.agent.args)}".rstrip())
    verify = " ".join(config.verify.command) or "(none — a closed issue is taken on trust)"
    typer.echo(f"verify    {verify}")
    if scope is not None:
        caps = f"{config.run.max_iterations} iterations"
        typer.echo(f"caps      {caps}, {config.run.max_attempts} attempts per issue")
    typer.echo(f"run dir   {config.run_dir()}")

    next_issue = tracker.ready(claim=False)
    if next_issue is None:
        doer = "this run" if scope else "a step"
        typer.echo(f"\nno issues are ready; {doer} would do nothing")
        return
    next_issue = tracker.get(next_issue.id)
    background = tracker.get(next_issue.parent).description if next_issue.parent else ""
    if scope is not None:
        # A run works one lane, named after the target (ADR 0023), so there is
        # nothing to guess and no need to ask herdr what exists.
        lane_branch = f"{config.lane.branch_prefix}{scope.target.id}"
        note = "one lane for the whole run"
    else:
        lane_branch, note = _lane_branch(config, next_issue)
    typer.echo(f"lane      {lane_branch}  ({note})")
    typer.echo(f"\nthe next iteration would work {next_issue.id} and send:\n")
    typer.echo(
        _indent(prompts.render_iterate(next_issue, background=background, branch=lane_branch))
    )


def _print_run(result: RunResult, *, lane: Lane | None) -> None:
    """Report a finished run: every turn, what it gave up on, and why it stopped.

    This is read after an hour away rather than glanced at, so it says more than
    the one line a step ends with. The colours are ``status``'s, because a
    second vocabulary for the same outcomes would be one to learn twice.

    A concurrent run has two things to say that a serial one does not, and both
    are printed only when there is something to print, so a serial run reads
    exactly as it always did
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
    Which turns landed on the integration branch, since "closed" and "on the
    branch you are about to review" stop being the same thing. And what was
    still running when the run stopped, because a report whose numbers look
    complete while two agents are still working is the one thing worse than a
    short one.
    """
    typer.echo("")
    if result.iterations:
        typer.echo(f"iterations ({len(result.iterations)}, {_minutes(result.elapsed)})")
        for item in result.iterations:
            colour = _OUTCOME_COLOURS.get(item.outcome, typer.colors.WHITE)
            mark = typer.style(item.outcome.ljust(8), fg=colour)
            typer.echo(f"  {item.number:>3}  {mark}  {item.issue_id}  {item.detail}")

    _print_merges(result)

    if result.deferred:
        typer.echo("")
        typer.secho(f"deferred ({len(result.deferred)})", fg=typer.colors.YELLOW)
        for issue_id, reason in result.deferred:
            typer.echo(f"  {issue_id}  {reason}")
        typer.echo("  `bd undefer <id>` puts one back in the queue.")

    if result.still_running:
        typer.echo("")
        typer.secho(f"still running ({len(result.still_running)})", fg=typer.colors.YELLOW)
        for issue_id in result.still_running:
            typer.echo(f"  {issue_id}")
        typer.echo("  These turns could not be collected. `milhouse reap` finishes one")
        typer.echo("  once its lane settles, and does not merge it.")

    if lane is not None:
        typer.echo("")
        typer.echo(f"branch  {lane.branch}")
        typer.echo(f"lane    {lane.path}")

    typer.echo("")
    parts = [f"{len(result.closed())} issue(s) closed"]
    merged, unmerged = result.merged(), result.unmerged()
    if merged or unmerged:
        parts.append(f"{len(merged)} merged")
    if result.still_running:
        parts.append(f"{len(result.still_running)} still running")
    summary = f"{result.target.id}: {', '.join(parts)} — {result.halt.detail}"
    typer.secho(summary, fg=typer.colors.GREEN if result.finished else typer.colors.YELLOW)


def _print_merges(result: RunResult) -> None:
    """What a concurrent run landed on the integration branch, and what it could not.

    Silent for a serial run, which works in the integration lane itself and so
    merges nothing into it. A branch that did not land gets its own block rather
    than a line in this one: it is why the run stopped, the issue is closed
    anyway, and landing it is somebody's next job.
    """
    merged, unmerged = result.merged(), result.unmerged()
    if merged:
        typer.echo("")
        typer.echo(f"merged ({len(merged)})")
        for line in _merge_lines(merged):
            typer.echo(line)

    if unmerged:
        typer.echo("")
        typer.secho(f"not merged ({len(unmerged)})", fg=typer.colors.RED)
        for line in _merge_lines(unmerged):
            typer.echo(line)
        typer.echo("  The issue is closed and the work is on its branch. Land it by hand.")


def _merge_lines(items: list[Iteration]) -> list[str]:
    """One line per turn saying what became of its branch, issue id first.

    The wording is :func:`milhouse.step.merge_line`, which is what the run
    printed as each merge happened, so the report and the transcript above it do
    not describe the same event twice in two vocabularies.
    """
    return [f"  {item.issue_id}  {merge_line(item.merge)}" for item in items if item.merge]


def _minutes(seconds: float) -> str:
    """A run's duration, in whichever unit reads better."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f}m"


def _lane_branch(config: Config, issue: Issue) -> tuple[str, str]:
    """Which branch the next step would commit to, and why, opening nothing.

    An issue already in a lane, or whose blocker is in one, continues on that
    lane's branch. Anything else gets a new one
    (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).
    """
    fresh = f"{config.lane.branch_prefix}{issue.id}"
    try:
        held = {
            lane.key: lane for lane in Lanes(HerdrClient(cwd=config.repo_root), config).registry()
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

    The first column is the label, which is an issue id for a lane ``dispatch``
    opened and a target id for the integration lane a ``run`` did
    (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`). Both are
    beads ids, so the column is headed rather than explained per row.

    A run's **worker** lane carries an issue id too, so the branch is what tells
    the two apart: ``milhouse/bd-e.1`` came from ``dispatch``, and
    ``milhouse/bd-e/bd-e.1`` is that issue inside a run of ``bd-e``
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
    That is what namespacing the branch under the target is for, and it is why
    the branch is in the listing rather than only the label.
    """
    try:
        lanes = Lanes(client, config).registry()
    except MilhouseError as exc:
        typer.secho(f"\nlanes   unavailable: {exc}", fg=typer.colors.YELLOW)
        return
    if not lanes:
        return
    typer.echo("")
    typer.echo(f"lanes ({len(lanes)})  issue or target, branch, checkout")
    for lane in lanes:
        held = lane.key or "(no workspace holds it)"
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
