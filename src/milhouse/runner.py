"""Running one iteration's agent, start to exit.

:class:`AgentRunner` owns the four steps that make up a turn: start a fresh
agent in the pane, prompt it once and wait for the turn to settle, capture the
pane transcript, then exit the agent so the pane is back at a shell prompt for
the next iteration.

Starting fresh every time is the point. It is what gives each iteration the
clean context window a ralph loop depends on
(:doc:`ADR 0003 <../../docs/decisions/0003-agents-run-in-herdr-panes>`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .errors import AgentError, HerdrError, TurnTimeoutError
from .herdr import AgentStatus, HerdrClient
from .rundir import ensure_run_dir

__all__ = ["AgentRunner", "Runner", "TurnResult"]

log = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """What one turn produced, before it is classified.

    Attributes:
        agent_state: The lifecycle state herdr left the agent in.
        timed_out: The turn did not settle within the turn timeout.
        prompt_path: Where the rendered prompt was saved.
        transcript_path: Where the pane transcript was saved, if one was captured.
        error: A milhouse-side failure, if the turn could not be run at all.
    """

    agent_state: AgentStatus
    timed_out: bool = False
    prompt_path: Path | None = None
    transcript_path: Path | None = None
    error: str | None = None


class Runner(Protocol):
    """What :func:`milhouse.step.step` needs from whatever runs the agent.

    :class:`AgentRunner` is the only implementation, and it drives a TUI in a
    herdr pane. The protocol exists because that is not the only way to run a
    turn: a headless agent has an exit code and a usage block, which is what
    would retire both the exit-key fragility of
    :doc:`ADR 0011 <../../docs/decisions/0011-exiting-the-agent>` and the cost
    blindness of :doc:`ADR 0012 <../../docs/decisions/0012-no-cost-caps>`.

    It is not speculative generality: the tests implement it too.
    """

    pane_id: str
    """The pane in use, which may change when a pane has to be replaced."""

    agent_name: str
    """The herdr agent name, e.g. ``milhouse-hello``."""

    workdir: Path
    """The directory the agent works in, which is what a turn is classified against.

    A lane's worktree
    (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`), so
    reading ``HEAD`` anywhere else would attribute one lane's commits to another.
    """

    def run_turn(self, prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        """Run one whole turn and leave the pane ready for the next one.

        Blocks until the agent settles. ``milhouse step`` uses this; anything
        that starts several turns at once uses the three below instead.
        """
        ...

    def start_turn(self, prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        """Start an agent, submit the prompt, and return without waiting for the turn.

        The returned result carries the prompt path and any failure to start or
        to submit. Its ``agent_state`` is the state herdr observed once the
        prompt had landed, which is not the state the turn will end in.

        An implementation must not report a turn as started until the prompt is
        known to have been taken. A turn nobody waits for has no later signal
        saying it never began.
        """
        ...

    def settled(self) -> AgentStatus | None:
        """The state the turn ended in, or ``None`` while the agent is working.

        An agent herdr no longer knows about counts as settled: whatever
        happened to it, it is not still working.
        """
        ...

    def finish_turn(self, iteration: int, *, issue_id: str | None = None) -> TurnResult:
        """Capture the transcript of a settled turn and exit the agent.

        The mirror of :meth:`start_turn`, and the half of :meth:`run_turn` that
        happens after the wait.
        """
        ...

    def exit_agent(self) -> None:
        """Return the pane to a shell prompt. Idempotent."""
        ...


class AgentRunner:
    """Starts, prompts, reads, and exits one agent per iteration."""

    def __init__(
        self,
        client: HerdrClient,
        config: Config,
        *,
        run_dir: Path,
        pane_id: str,
        agent_name: str,
        workdir: Path | None = None,
    ) -> None:
        """Bind a runner to one pane.

        Args:
            client: The herdr client to drive.
            config: Resolved configuration, for the agent kind, args, and timeouts.
            run_dir: ``.milhouse/runs``, where prompts and transcripts go.
            pane_id: The pane to run agents in. May change if a pane has to be
                replaced; read :attr:`pane_id` back after each turn.
            agent_name: The herdr agent name, e.g. ``milhouse-hello``.
            workdir: The directory the pane sits in, and therefore the checkout
                the turn is classified against. Defaults to the repository root.
        """
        self.client = client
        self.config = config
        self.run_dir = run_dir
        self.pane_id = pane_id
        self.agent_name = agent_name
        self.workdir = workdir or config.repo_root

    def run_turn(self, prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        """Run one whole turn and always leave the pane at a shell prompt.

        The transcript is captured *before* the agent is exited, so a
        post-mortem still has it even when the exit falls back to replacing the
        pane and losing its scrollback.

        Args:
            prompt: The rendered prompt to send.
            iteration: 1-based iteration number, used to name the artifacts.
            issue_id: The issue this turn works, which gets its own artifact
                directory. ``None`` writes to the run directory itself, for a
                turn that has no issue to file under.

        Returns:
            What happened, for :func:`milhouse.outcome.classify` to interpret.
        """
        result = self._begin(prompt, iteration, issue_id=issue_id)
        if result.error:
            return result

        try:
            result.agent_state = self.client.prompt(
                self.agent_name,
                prompt,
                timeout_ms=self.config.agent.turn_timeout_ms,
                wait=True,
            )
        except TurnTimeoutError:
            result.timed_out = True
            result.agent_state = self.client.agent_status(self.agent_name)
        except HerdrError as exc:
            result.error = f"could not prompt the agent: {exc}"

        result.transcript_path = self._collect(iteration, issue_id=issue_id)
        return result

    def start_turn(self, prompt: str, *, iteration: int, issue_id: str | None = None) -> TurnResult:
        """Start an agent, submit the prompt, and return without waiting for the turn.

        The waiting is the only difference from :meth:`run_turn`: herdr is not
        asked to block until the turn ends, so several turns can be in flight at
        once and a later :func:`milhouse.step.reap` collects them
        (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).

        It does wait for the **submission**, through
        :meth:`~milhouse.herdr.HerdrClient.submit`, and that is not a smaller
        version of waiting for the turn. Returning the instant the prompt was
        handed over used to mean returning without knowing whether it had been
        taken, and a prompt swallowed by a just-started agent leaves an agent
        that reports ``idle`` forever after — indistinguishable from one that
        finished, so the poller collected a turn that never happened and called
        it the agent's failure
        (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
        Confirming here is what stops that state existing, and it costs the
        fraction of a second herdr needs to see the agent react.

        Args:
            prompt: The rendered prompt to send.
            iteration: 1-based iteration number, used to name the artifacts.
            issue_id: The issue this turn works.

        Returns:
            The prompt path, the state herdr observed once the prompt landed,
            and any failure to start or submit.
        """
        result = self._begin(prompt, iteration, issue_id=issue_id)
        if result.error:
            return result
        try:
            result.agent_state = self.client.submit(
                self.agent_name,
                prompt,
                timeout_ms=self.config.agent.submit_timeout_ms,
                attempts=self.config.agent.submit_attempts,
            )
        except HerdrError as exc:
            result.error = f"could not prompt the agent: {exc}"
        return result

    def settled(self) -> AgentStatus | None:
        """The state this turn ended in, or ``None`` while the agent is working.

        ``unknown`` counts as settled. An agent herdr has lost track of is not
        going to finish, and leaving the claim in flight forever is worse than
        classifying a turn nobody can explain.
        """
        status = self.client.agent_status(self.agent_name)
        return None if status == "working" else status

    def finish_turn(self, iteration: int, *, issue_id: str | None = None) -> TurnResult:
        """Capture a settled turn's transcript and exit the agent.

        The transcript is captured *before* the agent is exited, so a
        post-mortem still has it even when the exit falls back to replacing the
        pane and losing its scrollback.

        Args:
            iteration: The iteration number, used to name the transcript.
            issue_id: The issue this turn worked.

        Returns:
            The state herdr left the agent in, and the transcript path.
        """
        state = self.client.agent_status(self.agent_name)
        return TurnResult(
            agent_state=state, transcript_path=self._collect(iteration, issue_id=issue_id)
        )

    def _collect(self, iteration: int, *, issue_id: str | None) -> Path | None:
        """Save the transcript and put the pane back at a shell prompt."""
        transcript = self._capture(iteration, issue_id=issue_id)
        self.exit_agent()
        return transcript

    def _begin(self, prompt: str, iteration: int, *, issue_id: str | None) -> TurnResult:
        """Save the prompt and start a fresh agent in the pane."""
        prompt_path = self._write(f"iter-{iteration:03d}.prompt", prompt, issue_id=issue_id)
        result = TurnResult(agent_state="unknown", prompt_path=prompt_path)
        try:
            self._ensure_shell()
            self.client.start_agent(
                self.agent_name,
                kind=self.config.agent.kind,
                pane_id=self.pane_id,
                args=self.config.agent.args,
                timeout_ms=self.config.agent.start_timeout_ms,
            )
        except HerdrError as exc:
            result.error = f"could not start the agent: {exc}"
        return result

    def exit_agent(self) -> None:
        """Return the pane to a shell prompt, replacing it if the keys do not work.

        Idempotent, and safe to call when no agent is running — which is what
        makes it usable from the session's teardown path.

        Raises:
            AgentError: The pane could not be replaced either, which leaves the
                session with nowhere to start the next agent.
        """
        try:
            if self.client.pane_agent(self.pane_id) is None:
                return
            self.client.send_keys(self.pane_id, self.config.agent.exit_keys)
            timeout_s = self.config.agent.exit_timeout_ms / 1000
            if self.client.wait_for_shell(self.pane_id, timeout_s=timeout_s):
                return
        except HerdrError as exc:
            log.debug("exit keys failed, replacing the pane: %s", exc)

        log.warning("agent did not exit from key presses; replacing pane %s", self.pane_id)
        self._replace_pane()

    def _replace_pane(self) -> None:
        """Close the pane and split a fresh one, recording the new id.

        Raises:
            AgentError: herdr would not give us a usable pane back.
        """
        old = self.pane_id
        try:
            new_pane = self.client.split_pane(old, self.workdir)
            self.client.close_pane(old)
        except HerdrError as exc:
            raise AgentError(f"could not replace pane {old}: {exc}") from exc
        self.pane_id = new_pane

    def _ensure_shell(self) -> None:
        """Make sure the pane is at a shell prompt before starting an agent.

        Raises:
            AgentError: The pane could not be returned to a shell prompt.
        """
        if self.client.pane_agent(self.pane_id) is None:
            return
        log.debug("pane %s still has an agent; exiting it first", self.pane_id)
        self.exit_agent()
        if self.client.pane_agent(self.pane_id) is not None:
            raise AgentError(f"pane {self.pane_id} will not return to a shell prompt")

    def _capture(self, iteration: int, *, issue_id: str | None = None) -> Path | None:
        """Save the pane transcript, the primary post-mortem artifact."""
        try:
            transcript = self.client.read_agent(
                self.agent_name,
                source=self.config.herdr.read_source,
                lines=self.config.herdr.read_lines,
            )
        except HerdrError as exc:
            log.debug("could not read the agent transcript: %s", exc)
            return None
        if not transcript.strip():
            return None
        return self._write(f"iter-{iteration:03d}.term", transcript, issue_id=issue_id)

    def _write(self, name: str, text: str, *, issue_id: str | None = None) -> Path:
        """Write a run artifact and return its path.

        Artifacts for an issue go in a directory of their own, so every attempt
        at one issue sits together and two agents working different issues
        cannot collide on a filename. That collision is not reachable today,
        because a run works one issue at a time, but the filename is the only
        thing keeping them apart and iteration numbers are handed out per run
        rather than per issue.

        Args:
            name: The artifact filename, e.g. ``iter-007.prompt``.
            text: What to write.
            issue_id: Whose artifact this is, or ``None`` for a turn that
                belongs to the run rather than to an issue.

        Returns:
            The path written.
        """
        ensure_run_dir(self.run_dir)
        directory = self.run_dir / issue_id if issue_id else self.run_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path
