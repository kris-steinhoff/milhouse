"""Error hierarchy for milhouse, with the process exit code each error maps to.

Every failure milhouse can produce is one of these. The CLI catches
:class:`MilhouseError` at the top level, prints ``str(exc)`` plus the error's
``remedy``, and exits with ``exit_code``. Anything else escaping to the top level
is a bug and gets a traceback.

Exit codes are stable and documented in ``docs/usage.md``; scripts may branch on
them.
"""

from __future__ import annotations

__all__ = [
    "AgentError",
    "ConfigError",
    "HerdrError",
    "LoopAbortedError",
    "MilhouseError",
    "MissingDependencyError",
    "ProcessError",
    "SourceError",
    "TrackerError",
    "UserAbortError",
]


class MilhouseError(Exception):
    """Base class for every expected milhouse failure.

    Attributes:
        exit_code: Process exit status the CLI uses for this error. ``1``.
        remedy: One-line hint printed after the message telling the user what to
            do about it. Subclasses override it; the base class has none.
    """

    exit_code: int = 1
    remedy: str | None = None


class ConfigError(MilhouseError):
    """`.milhouse/config.toml`, an env override, or a flag is invalid.

    Exit code ``2``. Fix the offending key and re-run. ``docs/configuration.md``
    lists every key, its type, and its default.
    """

    exit_code = 2
    remedy = "Check .milhouse/config.toml against docs/configuration.md."


class SourceError(MilhouseError):
    """A task definition could not be resolved into a :class:`~milhouse.models.TaskDefinition`.

    Exit code ``3``. Raised for a missing file, an unreadable file, a malformed
    ``gh:`` spec, or a GitHub issue that ``gh`` cannot fetch.
    """

    exit_code = 3
    remedy = "Pass a readable file path or a gh:owner/repo#123 spec."


class TrackerError(MilhouseError):
    """A ``bd`` invocation failed or returned something milhouse cannot use.

    Exit code ``4``. Usually means the beads database is missing (``bd init``),
    or an issue id no longer exists.
    """

    exit_code = 4
    remedy = "Run `bd init` in this repo and confirm `bd list` works."


class HerdrError(MilhouseError):
    """A ``herdr`` invocation failed, or the herdr server is unreachable.

    Exit code ``5``. Run ``milhouse doctor`` to check the server is running and
    protocol-compatible with the client.
    """

    exit_code = 5
    remedy = "Run `milhouse doctor`; start the herdr server if it is not running."


class AgentError(MilhouseError):
    """An agent could not be started, prompted, or exited in its pane.

    Exit code ``6``. The pane is left as-is for inspection: attach to the
    workspace milhouse reported and look at it before retrying.
    """

    exit_code = 6
    remedy = "Attach to the herdr workspace and inspect the pane."


class MissingDependencyError(MilhouseError):
    """A required external tool is not on ``PATH``.

    Exit code ``7``. milhouse hard-depends on ``bd`` and ``herdr``; ``gh`` is
    needed only for ``gh:`` task sources, and the agent binary only for real
    runs. ``docs/troubleshooting.md`` has install instructions.
    """

    exit_code = 7
    remedy = "Install the missing tool, then re-run `milhouse doctor`."


class ProcessError(MilhouseError):
    """A subprocess launched through :mod:`milhouse.proc` failed.

    Exit code ``8``. Carries the argv, exit status, and captured output so the
    message is actionable without re-running by hand. Callers in ``tracker/`` and
    ``herdr.py`` normally catch this and re-raise a more specific error.
    """

    exit_code = 8

    def __init__(
        self,
        message: str,
        *,
        argv: tuple[str, ...] = (),
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        """Record the failed command alongside the human-readable message.

        Args:
            message: What went wrong, in one line.
            argv: The command that was run.
            returncode: Its exit status, or ``None`` if it never exited.
            stdout: Captured standard output.
            stderr: Captured standard error.
        """
        super().__init__(message)
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class LoopAbortedError(MilhouseError):
    """The ralph loop stopped before finishing the epic.

    Exit code ``9``. Covers hitting ``--max-iterations``, ``--on-blocked abort``,
    and an issue exhausting ``--max-attempts`` with nothing else ready. The run
    state under ``.milhouse/runs/`` is left in place so the run can be resumed.
    """

    exit_code = 9
    remedy = "Inspect .milhouse/runs/<task>/ then re-run `milhouse run` to resume."


class UserAbortError(MilhouseError):
    """The user interrupted the run (SIGINT/SIGTERM) or declined a confirmation.

    Exit code ``130``, matching the shell convention for SIGINT. The in-flight
    claim is reverted and the agent exited, but the workspace is left open.
    """

    exit_code = 130
    remedy = None
