"""Where an agent works.

A **lane** is a herdr worktree with a label on it: a checkout of its own, on a
branch of its own, in a workspace of its own
(:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`). That
container is what lets several agents work at once without treading on each
other, and herdr already had it, so milhouse did not build one.

**The label is the unit somebody will review**, which differs between the two
ways of driving milhouse
(:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`):

- ``milhouse dispatch`` reviews an issue, so :meth:`Lanes.open` labels a lane
  with the issue id and assigns it by the dependency rules below.
- ``milhouse run`` reviews a target, so :meth:`Lanes.open_for` gives the whole
  run one lane labelled with the target id and no rules at all.

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

import hashlib
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

_PREFIX = "milhouse-"
"""What every agent milhouse starts is named after.

herdr's registry carries no other marker for whose agent an agent is, so this is
how ``herdr agent list`` shows milhouse's. It spends nine of the thirty-two
characters a name has, and buying some of them back by shortening it would
orphan every agent already running under the longer form.
"""

_MAX_NAME = 32
"""Characters herdr allows in an agent name, prefix included.

Its grammar is ``^[a-z][a-z0-9_-]{0,31}$``. The leading-letter rule is satisfied
by :data:`_PREFIX`, so the length and the character set are what a key has to be
made to fit.
"""

_DIGEST_BYTES = 3
"""Digest bytes, so six hex characters, ending a name that had to be shortened.

Enough to keep the handful of lanes one repository holds apart, and short enough
to leave sixteen characters of the key legible to whoever reads the name.
"""

_UNSAFE = re.compile(r"[^a-z0-9_-]+")
"""What herdr's character set does not allow, once the key is lowercased.

The dot is handled before this runs. It is the one excluded character that
carries meaning (beads suffixes every child issue with ``.N``), so collapsing it
along with spaces and slashes would throw that away.
"""


@dataclass(frozen=True)
class Lane:
    """One working directory, and the herdr container holding it.

    Attributes:
        key: What the lane is labelled with, which is the unit somebody will
            review: the issue for ``dispatch``, the target for ``run``
            (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).
        path: The checkout the agent works in.
        branch: The branch it commits to.
        workspace_id: The herdr workspace holding the lane.
        pane_id: The pane to start the next agent in.
    """

    key: str
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
        """The herdr agent name for this lane's turns, e.g. ``milhouse-bd-e_1``.

        A key is not a name herdr will take. Its grammar is
        ``^[a-z][a-z0-9_-]{0,31}$``, and beads suffixes every child issue with
        ``.N``, an id can arrive in any case, and ``milhouse-`` in front of a
        long repository prefix reaches thirty-two characters on its own. So the
        key is lowercased, its dots become underscores, anything else outside the
        character set collapses to ``-``, and a name that would overflow keeps
        the front of the key and ends in a digest of the whole of it.

        Two properties of that transform are load-bearing, because this name is
        milhouse's handle on the agent for the rest of the turn:
        :mod:`milhouse.runner` prompts it, polls it and exits it by name, and
        ``milhouse reap`` recomputes it in a later process.

        - **Distinct keys keep distinct names.** Hence ``_`` for the dot rather
          than ``-``: ``-`` would make ``a.2`` and ``a-2`` one agent, and
          milhouse would drive an agent working somebody else's issue. Plain
          truncation collides the same way, which is what the digest answers.
        - **The name is the same in every process.** The digest comes from
          :mod:`hashlib` rather than from :func:`hash`, which is salted per
          process and would have ``reap`` address an agent that does not exist.

        Returns:
            A name inside herdr's grammar, for any key.
        """
        safe = _UNSAFE.sub("-", self.key.lower().replace(".", "_"))
        if len(_PREFIX) + len(safe) <= _MAX_NAME:
            return f"{_PREFIX}{safe}"
        digest = hashlib.blake2b(self.key.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()
        stem = safe[: _MAX_NAME - len(_PREFIX) - len(digest) - 1].rstrip("-_")
        return f"{_PREFIX}{stem}-{digest}"


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
                key=labels.get(worktree.workspace_id, ""),
                path=worktree.path,
                branch=worktree.branch,
                workspace_id=worktree.workspace_id,
            )
            for worktree in self.client.worktrees(self.config.repo_root)
            if worktree.path != self.config.repo_root
        ]

    def locate(self, key: str) -> tuple[Lane, str | None] | None:
        """The lane labelled ``key`` and the tab it is in, without a pane.

        Two places carry an id: a lane's own workspace label, and the label of a
        tab added to somebody else's lane for a stacked issue. Both are looked
        at, because an issue does not know which kind it got.

        Choosing a pane is deliberately not part of this. Doing so can *create*
        one, which is wrong for anyone who only wants to know whether the lane
        exists — reconciling, or reaping a turn whose agent already has a pane.

        Args:
            key: The label to look for: an issue id, or a run's target id.

        Returns:
            The lane and its tab id (``None`` for a lane of its own), or ``None``
            when no lane carries the label.
        """
        labels = self.client.workspace_labels()
        open_worktrees = [
            worktree
            for worktree in self.client.worktrees(self.config.repo_root)
            if worktree.workspace_id
        ]
        for worktree in open_worktrees:
            if labels.get(worktree.workspace_id) == key:
                return self._lane(key, worktree), None
        for worktree in open_worktrees:
            for tab in self.client.tabs(worktree.workspace_id):
                if tab.get("label") == key:
                    return self._lane(key, worktree), str(tab["tab_id"])
        return None

    def find(self, key: str) -> Lane | None:
        """The lane labelled ``key``, with a pane ready for an agent.

        Args:
            key: The label to look for.

        Returns:
            The lane, or ``None`` when there is none.
        """
        located = self.locate(key)
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
            where = ", ".join(f"{lane.key} on {lane.branch}" for lane in predecessors)
            raise MilhouseError(
                f"{issue.id} depends on work in more than one lane ({where}), and which "
                "branch it should continue from is not decided. Land one of them first."
            )
        if predecessors:
            return self._stack_on(predecessors[0], issue, focus=focus)
        return self._new_lane(issue.id, source_workspace=source_workspace, base=base, focus=focus)

    def open_for(self, key: str, *, source_workspace: str, base: str, focus: bool = False) -> Lane:
        """The one lane labelled ``key``, creating it if there is none.

        What ``milhouse run`` opens. None of the dependency rules in :meth:`open`
        apply, because every issue in a run shares this lane by construction:
        there is one base branch, so a join has nothing to choose between, and
        an issue whose blocker ran elsewhere does not drag the run into
        somebody else's lane
        (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`).

        Args:
            key: What to label the lane with, which for a run is the target id.
            source_workspace: The workspace of the primary checkout.
            base: Ref a new lane branches from.
            focus: Bring a newly created lane to the front.

        Returns:
            The lane, with a pane ready for an agent.
        """
        existing = self.find(key)
        if existing is not None:
            return existing
        return self._new_lane(key, source_workspace=source_workspace, base=base, focus=focus)

    def _stack_on(self, predecessor: Lane, issue: Issue, *, focus: bool) -> Lane:
        """Give ``issue`` a tab in the lane its blocker ran in.

        Same checkout, same branch, fresh agent. Branching from the predecessor
        instead would fork work that is meant to be one line of it.
        """
        pane_id = self.client.create_tab(
            predecessor.workspace_id, predecessor.path, issue.id, focus=focus
        )
        return Lane(
            key=issue.id,
            path=predecessor.path,
            branch=predecessor.branch,
            workspace_id=predecessor.workspace_id,
            pane_id=pane_id,
        )

    def _new_lane(self, key: str, *, source_workspace: str, base: str, focus: bool) -> Lane:
        """Open a worktree labelled ``key``, re-using the checkout if one survived."""
        branch = f"{self.config.lane.branch_prefix}{key}"
        sleeping = self.dormant(branch)
        if sleeping is not None:
            worktree = self.client.open_worktree(
                source_workspace=source_workspace,
                path=sleeping.path,
                label=key,
                focus=focus,
            )
        else:
            worktree = self.client.create_worktree(
                source_workspace=source_workspace,
                branch=branch,
                base=base,
                label=key,
                focus=focus,
            )
        self._keep_git_out_of_it(worktree.path)
        return Lane(
            key=key,
            path=worktree.path,
            branch=worktree.branch,
            workspace_id=worktree.workspace_id,
            pane_id=worktree.pane_id,
        )

    # -- plumbing ---------------------------------------------------------

    def _lane(self, key: str, worktree: Worktree) -> Lane:
        """Build a :class:`Lane` from a registry entry, with no pane chosen."""
        return Lane(
            key=key,
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
