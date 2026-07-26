"""Running one iteration's agent, start to exit.

:class:`AgentRunner` owns the four steps that make up a turn: start a fresh
agent in the pane, prompt it once and wait for the turn to settle, capture the
pane transcript, then exit the agent so the pane is back at a shell prompt for
the next iteration.

Starting fresh every time is the point. It is what gives the loop the clean
context window a ralph loop depends on
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

    def run_turn(self, prompt: str, *, iteration: int) -> TurnResult:
        """Run one whole turn and leave the pane ready for the next one."""
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
    ) -> None:
        """Bind a runner to one pane.

        Args:
            client: The herdr client to drive.
            config: Resolved configuration, for the agent kind, args, and timeouts.
            run_dir: ``.milhouse/runs/<slug>``, where prompts and transcripts go.
            pane_id: The pane to run agents in. May change if a pane has to be
                replaced; read :attr:`pane_id` back after each turn.
            agent_name: The herdr agent name, e.g. ``milhouse-hello``.
        """
        self.client = client
        self.config = config
        self.run_dir = run_dir
        self.pane_id = pane_id
        self.agent_name = agent_name

    def run_turn(self, prompt: str, *, iteration: int) -> TurnResult:
        """Run one whole turn and always leave the pane at a shell prompt.

        The transcript is captured *before* the agent is exited, so a
        post-mortem still has it even when the exit falls back to replacing the
        pane and losing its scrollback.

        Args:
            prompt: The rendered prompt to send.
            iteration: 1-based iteration number, used to name the artifacts.

        Returns:
            What happened, for :func:`milhouse.outcome.classify` to interpret.
        """
        prompt_path = self._write(f"iter-{iteration:03d}.prompt", prompt)
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

        try:
            result.agent_state = self.client.prompt(
                self.agent_name,
                prompt,
                timeout_ms=self.config.loop.turn_timeout_ms,
            )
        except TurnTimeoutError:
            result.timed_out = True
            result.agent_state = self.client.agent_status(self.agent_name)
        except HerdrError as exc:
            result.error = f"could not prompt the agent: {exc}"

        result.transcript_path = self._capture(iteration)
        self.exit_agent()
        return result

    def exit_agent(self) -> None:
        """Return the pane to a shell prompt, replacing it if the keys do not work.

        Idempotent, and safe to call when no agent is running — which is what
        makes it usable from the loop's teardown path.

        Raises:
            AgentError: The pane could not be replaced either, which leaves the
                run with nowhere to start the next agent.
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
            new_pane = self.client.split_pane(old, self.config.repo_root)
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

    def _capture(self, iteration: int) -> Path | None:
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
        return self._write(f"iter-{iteration:03d}.term", transcript)

    def _write(self, name: str, text: str) -> Path:
        """Write a run artifact into the run directory and return its path."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / name
        path.write_text(text, encoding="utf-8")
        return path
