"""Where an issue's agent works.

A **lane** is a herdr worktree labelled with the issue id: a checkout of its own,
on a branch of its own, in a workspace of its own
(:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`). That
container is what lets several agents work at once without treading on each
other, and herdr already had it, so milhouse did not build one.

**herdr is the registry.** ``herdr worktree list`` answers what lanes exist and
on what branches, and ``herdr workspace list`` answers which issue each one is
for, because the id is the workspace label. milhouse stores no lane state, the
same rule it applies to issues: do not keep a copy, ask the tool that owns it.

Assignment follows the dependency graph onto that hierarchy, and it is the only
thing here that is milhouse's own judgement:

- An issue already in a lane goes back to it. That is how a second attempt lands
  on the branch the first one committed to.
- An issue whose blocker ran in a live lane gets **a new tab in that lane**,
  continuing on the same branch, rather than a worktree branched from it.
- Anything else gets a new worktree, branched from the primary checkout.

An issue with two blockers in separate lanes has two candidate bases and no rule
picking between them, which is deliberately undecided. milhouse refuses rather
than guessing, and names both lanes so a person can.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Config
from .errors import MilhouseError
from .herdr import HerdrClient, Worktree
from .models import Issue

__all__ = ["Lane", "Lanes"]

log = logging.getLogger(__name__)

EXCLUDE_RELPATH = Path(".git/info/exclude")
"""Where a lane inside the repository gets ignored.

Local and untracked, so nothing has to be committed for it to work. herdr puts
linked worktrees under ``~/.herdr/worktrees`` and this never fires, but a lane
that git could see would appear as untracked files in every other lane's
``is_dirty()`` check, and that is too quiet a failure to leave to a default.
"""

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Lane:
    """One issue's working directory, and the herdr container holding it.

    Attributes:
        issue_id: The issue this lane was opened for.
        path: The checkout the agent works in.
        branch: The branch it commits to.
        workspace_id: The herdr workspace holding the lane.
        pane_id: The pane to start this issue's agent in.
    """

    issue_id: str
    path: Path
    branch: str
    workspace_id: str
    pane_id: str = ""
    """A pane to start this issue's agent in. Empty when nobody asked for one.

    Choosing a pane can create one, so :meth:`Lanes.registry` leaves it out and
    only the calls that are about to run an agent fill it in.
    """

    @property
    def agent_name(self) -> str:
        """The herdr agent name for this lane's turns, e.g. ``milhouse-bd-e.1``."""
        return f"milhouse-{_UNSAFE.sub('-', self.issue_id)}"


class Lanes:
    """The lane registry, which is herdr's, read through one repository."""

    def __init__(self, client: HerdrClient, config: Config) -> None:
        """Bind to the lanes of one repository.

        Args:
            client: The herdr client.
            config: Resolved configuration, for the repo root and branch prefix.
        """
        self.client = client
        self.config = config

    # -- the registry -----------------------------------------------------

    def registry(self) -> list[Lane]:
        """Every lane herdr is holding for this repository, without panes.

        A lane is any worktree of this repository other than the primary
        checkout, and the issue it belongs to is its workspace's label.

        Returns:
            The lanes, in herdr's order, with an empty ``pane_id``.
        """
        labels = self.client.workspace_labels()
        return [
            Lane(
                issue_id=labels.get(worktree.workspace_id, ""),
                path=worktree.path,
                branch=worktree.branch,
                workspace_id=worktree.workspace_id,
            )
            for worktree in self.client.worktrees(self.config.repo_root)
            if worktree.path != self.config.repo_root
        ]

    def locate(self, issue_id: str) -> tuple[Lane, str | None] | None:
        """The lane carrying ``issue_id`` and the tab it is in, without a pane.

        Two places carry an id: a lane's own workspace label, and the label of a
        tab added to somebody else's lane for a stacked issue. Both are looked
        at, because an issue does not know which kind it got.

        Choosing a pane is deliberately not part of this. Doing so can *create*
        one, which is wrong for anyone who only wants to know whether the lane
        exists — reconciling, or reaping a turn whose agent already has a pane.

        Args:
            issue_id: The issue to look for.

        Returns:
            The lane and its tab id (``None`` for a lane of its own), or ``None``
            when no lane carries the issue.
        """
        labels = self.client.workspace_labels()
        open_worktrees = [
            worktree
            for worktree in self.client.worktrees(self.config.repo_root)
            if worktree.workspace_id
        ]
        for worktree in open_worktrees:
            if labels.get(worktree.workspace_id) == issue_id:
                return self._lane(issue_id, worktree), None
        for worktree in open_worktrees:
            for tab in self.client.tabs(worktree.workspace_id):
                if tab.get("label") == issue_id:
                    return self._lane(issue_id, worktree), str(tab["tab_id"])
        return None

    def find(self, issue_id: str) -> Lane | None:
        """The lane carrying ``issue_id``, with a pane ready for an agent.

        Args:
            issue_id: The issue to look for.

        Returns:
            The lane, or ``None`` when there is none.
        """
        located = self.locate(issue_id)
        if located is None:
            return None
        lane, tab_id = located
        return replace(lane, pane_id=self._pane_in(lane, tab_id=tab_id))

    def dormant(self, branch: str) -> Worktree | None:
        """A worktree on ``branch`` that no workspace currently holds open.

        Closing a lane's workspace leaves the checkout and its branch alone, so
        resuming that issue means re-opening rather than creating.
        """
        for worktree in self.client.worktrees(self.config.repo_root):
            if worktree.branch == branch and not worktree.workspace_id:
                return worktree
        return None

    # -- assignment -------------------------------------------------------

    def open(self, issue: Issue, *, source_workspace: str, base: str, focus: bool = False) -> Lane:
        """Return the lane ``issue`` should be worked in, creating one if needed.

        Args:
            issue: The claimed issue.
            source_workspace: The workspace of the primary checkout, which is how
                herdr knows which repository a new worktree comes from.
            base: Ref a new lane branches from.
            focus: Bring a newly created lane to the front.

        Returns:
            The lane, with a pane ready for an agent.

        Raises:
            MilhouseError: The issue depends on work done in two different
                lanes, which has no settled base branch.
        """
        existing = self.find(issue.id)
        if existing is not None:
            return existing

        predecessors = [lane for lane in map(self.find, issue.blocked_by) if lane is not None]
        if len(predecessors) > 1:
            where = ", ".join(f"{lane.issue_id} on {lane.branch}" for lane in predecessors)
            raise MilhouseError(
                f"{issue.id} depends on work in more than one lane ({where}), and which "
                "branch it should continue from is not decided. Land one of them first."
            )
        if predecessors:
            return self._stack_on(predecessors[0], issue, focus=focus)
        return self._new_lane(issue, source_workspace=source_workspace, base=base, focus=focus)

    def _stack_on(self, predecessor: Lane, issue: Issue, *, focus: bool) -> Lane:
        """Give ``issue`` a tab in the lane its blocker ran in.

        Same checkout, same branch, fresh agent. Branching from the predecessor
        instead would fork work that is meant to be one line of it.
        """
        pane_id = self.client.create_tab(
            predecessor.workspace_id, predecessor.path, issue.id, focus=focus
        )
        return Lane(
            issue_id=issue.id,
            path=predecessor.path,
            branch=predecessor.branch,
            workspace_id=predecessor.workspace_id,
            pane_id=pane_id,
        )

    def _new_lane(self, issue: Issue, *, source_workspace: str, base: str, focus: bool) -> Lane:
        """Open a worktree for ``issue``, re-using the checkout if one survived."""
        branch = f"{self.config.lane.branch_prefix}{issue.id}"
        sleeping = self.dormant(branch)
        if sleeping is not None:
            worktree = self.client.open_worktree(
                source_workspace=source_workspace,
                path=sleeping.path,
                label=issue.id,
                focus=focus,
            )
        else:
            worktree = self.client.create_worktree(
                source_workspace=source_workspace,
                branch=branch,
                base=base,
                label=issue.id,
                focus=focus,
            )
        self._keep_git_out_of_it(worktree.path)
        return Lane(
            issue_id=issue.id,
            path=worktree.path,
            branch=worktree.branch,
            workspace_id=worktree.workspace_id,
            pane_id=worktree.pane_id,
        )

    # -- plumbing ---------------------------------------------------------

    def _lane(self, issue_id: str, worktree: Worktree) -> Lane:
        """Build a :class:`Lane` from a registry entry, with no pane chosen."""
        return Lane(
            issue_id=issue_id,
            path=worktree.path,
            branch=worktree.branch,
            workspace_id=worktree.workspace_id,
        )

    def _pane_in(self, lane: Lane, *, tab_id: str | None) -> str:
        """Choose a pane in ``lane`` to start an agent in, splitting one if needed."""
        return self.client.pane_to_work_in(
            lane.workspace_id,
            lane.path,
            avoid=self.config.herdr.self_pane,
            tab_id=tab_id,
        )

    def _keep_git_out_of_it(self, path: Path) -> None:
        """Ignore a lane that landed inside the repository.

        herdr checks linked worktrees out under ``~/.herdr/worktrees``, so this
        normally does nothing. It is here because the failure it prevents is
        silent: a checkout git can see is untracked files in every other lane's
        dirty check, and ``dirty_after`` is part of how a turn is classified.
        """
        try:
            relative = path.relative_to(self.config.repo_root)
        except ValueError:
            return
        exclude = self.config.repo_root / EXCLUDE_RELPATH
        entry = f"/{relative.as_posix()}/"
        try:
            lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
            if entry in lines:
                return
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text("\n".join([*lines, entry]) + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("could not ignore the lane at %s: %s", path, exc)
