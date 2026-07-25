"""One-shot decomposition of a task into tracked issues.

The planning agent does not create issues. It writes a plan file and stops;
milhouse validates it, shows it for approval, and does the creating itself. That
is what makes the approval guardrail structural rather than a polite request
(:doc:`ADR 0006 <../../docs/decisions/0006-planning-agent-proposes-milhouse-creates>`).

This module owns the plan format, its validation rules, and the creation pass.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import prompts
from .config import Config
from .errors import MilhouseError, UserAbortError
from .models import Issue, TaskDefinition
from .runner import AgentRunner
from .tracker.base import Tracker

__all__ = ["Plan", "PlanError", "PlanIssue", "Planner"]

PLAN_FILENAME = "plan.json"
"""Where the planning agent writes its proposal, inside the run directory."""

VALID_TYPES = ("task", "feature", "bug", "chore")
"""Issue types the plan format accepts. A superset confuses `bd`; a subset is fine."""


class PlanError(MilhouseError):
    """The planning agent produced no usable plan.

    Exit code ``1``. The plan file, if any, is left in place for inspection.
    Milhouse reports which validation rule broke rather than guessing at a fix.
    """

    remedy = "Inspect .milhouse/runs/<task>/plan.json, fix it, and re-run."


@dataclass
class PlanIssue:
    """One issue a plan proposes.

    Attributes:
        key: Plan-local handle, unique within the plan, used by ``blocked_by``.
        title: One-line imperative summary. Required.
        type: One of :data:`VALID_TYPES`.
        priority: 0 (highest) to 4, or ``None`` for the tracker's default.
        description: What to do and why, written for an agent with no context.
        acceptance: How that agent knows it is finished.
        blocked_by: Keys of issues in the same plan that must be done first.
    """

    key: str
    title: str
    type: str = "task"
    priority: int | None = None
    description: str = ""
    acceptance: str = ""
    blocked_by: list[str] = field(default_factory=list)


@dataclass
class Plan:
    """A validated decomposition, ready to be created in the tracker."""

    issues: list[PlanIssue]

    @classmethod
    def parse(cls, payload: Any) -> Plan:
        """Validate a plan document and build a :class:`Plan` from it.

        Four rules, all checked before anything reaches the tracker:

        1. A non-empty ``issues`` array of objects.
        2. Every issue has a non-empty ``title`` and a unique, non-empty ``key``.
        3. Every ``blocked_by`` entry names another issue in the same plan.
        4. The ``blocked_by`` graph is acyclic — a cycle would leave ``bd ready``
           permanently empty, and the loop would exit claiming success.

        Args:
            payload: The parsed JSON document.

        Returns:
            The validated plan.

        Raises:
            PlanError: Any of the four rules is broken.
        """
        if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
            raise PlanError("the plan must be an object with an `issues` array")
        raw_issues = payload["issues"]
        if not raw_issues:
            raise PlanError("the plan proposes no issues")

        issues: list[PlanIssue] = []
        keys: set[str] = set()
        for index, raw in enumerate(raw_issues):
            issue = cls._parse_issue(raw, index)
            if issue.key in keys:
                raise PlanError(f"duplicate issue key {issue.key!r}")
            keys.add(issue.key)
            issues.append(issue)

        for issue in issues:
            for blocker in issue.blocked_by:
                if blocker not in keys:
                    raise PlanError(f"issue {issue.key!r} is blocked by unknown key {blocker!r}")
        _reject_cycles(issues)
        return cls(issues=issues)

    @staticmethod
    def _parse_issue(raw: Any, index: int) -> PlanIssue:
        """Validate one issue object.

        Raises:
            PlanError: The object is malformed.
        """
        where = f"issues[{index}]"
        if not isinstance(raw, dict):
            raise PlanError(f"{where} is not an object")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise PlanError(f"{where} has no title")
        key = str(raw.get("key") or "").strip()
        if not key:
            raise PlanError(f"{where} ({title!r}) has no key")
        issue_type = str(raw.get("type") or "task").strip()
        if issue_type not in VALID_TYPES:
            raise PlanError(f"{where} has unknown type {issue_type!r}; use one of {VALID_TYPES}")
        blocked_by = raw.get("blocked_by") or []
        if not isinstance(blocked_by, list) or not all(isinstance(k, str) for k in blocked_by):
            raise PlanError(f"{where} has a malformed blocked_by; expected a list of keys")
        priority = raw.get("priority")
        if priority is not None and not isinstance(priority, int):
            raise PlanError(f"{where} has a non-integer priority")
        return PlanIssue(
            key=key,
            title=title,
            type=issue_type,
            priority=priority,
            description=str(raw.get("description") or "").strip(),
            acceptance=str(raw.get("acceptance") or "").strip(),
            blocked_by=list(blocked_by),
        )

    def render_tree(self) -> str:
        """Render the plan for a human to approve, one line per issue."""
        lines = []
        for issue in self.issues:
            blockers = f"  (after {', '.join(issue.blocked_by)})" if issue.blocked_by else ""
            priority = f" P{issue.priority}" if issue.priority is not None else ""
            lines.append(f"  [{issue.type}{priority}] {issue.title}{blockers}")
        return "\n".join(lines)


class Planner:
    """Runs the planning agent, validates its plan, and creates the issues."""

    def __init__(
        self,
        config: Config,
        tracker: Tracker,
        runner: AgentRunner,
        *,
        run_dir: Path,
        max_issues: int = 12,
    ) -> None:
        """Wire the planner to its collaborators.

        Args:
            config: Resolved configuration.
            tracker: Where the issues will be created.
            runner: The agent runner that will run the one planning turn.
            run_dir: ``.milhouse/runs/<slug>``, where ``plan.json`` lives.
            max_issues: Soft ceiling passed to the planning prompt.
        """
        self.config = config
        self.tracker = tracker
        self.runner = runner
        self.run_dir = run_dir
        self.max_issues = max_issues

    @property
    def plan_path(self) -> Path:
        """Where the planning agent must write its proposal."""
        return self.run_dir / PLAN_FILENAME

    def propose(self, task: TaskDefinition) -> Plan:
        """Run the planning agent once and return its validated plan.

        Any stale plan from an earlier run is removed first, so a planning agent
        that writes nothing cannot be mistaken for one that succeeded.

        Args:
            task: The task to decompose.

        Returns:
            The validated plan.

        Raises:
            PlanError: The agent wrote no plan, or wrote an invalid one.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.plan_path.unlink(missing_ok=True)

        prompt = prompts.render_plan(
            task, plan_path=str(self.plan_path), max_issues=self.max_issues
        )
        result = self.runner.run_turn(prompt, iteration=0)
        if result.error:
            raise PlanError(f"the planning agent could not be run: {result.error}")
        return self.read_plan()

    def read_plan(self) -> Plan:
        """Read and validate the plan file the agent was asked to write.

        Returns:
            The validated plan.

        Raises:
            PlanError: The file is missing, unreadable, or invalid.
        """
        if not self.plan_path.exists():
            raise PlanError(
                f"the planning agent did not write {self.plan_path}; "
                "see the transcript in the same directory"
            )
        try:
            payload = json.loads(self.plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PlanError(f"{self.plan_path} is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise PlanError(f"cannot read {self.plan_path}: {exc}") from exc
        return Plan.parse(payload)

    def create(self, task: TaskDefinition, plan: Plan) -> tuple[Issue, list[Issue]]:
        """Create the epic and its children from an approved plan.

        The epic is created by milhouse with the ``milhouse_task`` metadata, so
        an agent cannot forge one the loop would then pick up
        (:doc:`ADR 0002 <../../docs/decisions/0002-link-issues-via-bead-metadata>`).

        Args:
            task: The task being decomposed.
            plan: The approved plan.

        Returns:
            The epic and the created children.
        """
        epic = self.tracker.create_epic(task)
        children = self.tracker.create_children(epic.id, list(plan.issues))
        return epic, children

    def plan(
        self,
        task: TaskDefinition,
        *,
        confirm: Callable[[Plan], bool] | None = None,
    ) -> tuple[Issue, list[Issue]]:
        """Decompose ``task`` end to end: propose, approve, create.

        Args:
            task: The task to decompose.
            confirm: Called with the validated plan; returning ``False`` aborts
                before anything is created. ``None`` creates without asking,
                which is what ``--yes`` does.

        Returns:
            The epic and the created children.

        Raises:
            PlanError: Planning failed.
            UserAbortError: ``confirm`` declined the plan.
        """
        proposal = self.propose(task)
        if confirm is not None and not confirm(proposal):
            raise UserAbortError("decomposition declined; nothing was created")
        return self.create(task, proposal)


def _reject_cycles(issues: list[PlanIssue]) -> None:
    """Raise if ``blocked_by`` contains a cycle.

    A cycle is worse than it sounds: ``bd ready`` would return nothing, the loop
    would see an empty result, and it would report the epic finished having done
    no work at all.

    Raises:
        PlanError: A cycle exists, naming one issue in it.
    """
    blockers = {issue.key: issue.blocked_by for issue in issues}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(key: str) -> None:
        if key in done:
            return
        if key in visiting:
            raise PlanError(f"the plan's dependencies form a cycle through {key!r}")
        visiting.add(key)
        for blocker in blockers.get(key, []):
            visit(blocker)
        visiting.discard(key)
        done.add(key)

    for key in blockers:
        visit(key)
