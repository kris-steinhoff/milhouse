"""The single subprocess chokepoint.

Every external command milhouse runs (``bd``, ``herdr``, ``gh``, ``git``) goes
through :func:`run` or :func:`run_json` here. That gives one place to add
logging, timeouts, and error mapping, and one seam for tests: faking
:func:`_execute` replaces the whole outside world while leaving the JSON parsing
and error handling in this module under test.

Nothing in this module knows what ``bd`` or ``herdr`` mean. It runs argv and
returns bytes-as-text.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from os import PathLike
from typing import Any

from .errors import ProcessError

__all__ = ["ProcResult", "have", "run", "run_json"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcResult:
    """The outcome of one subprocess invocation.

    Attributes:
        argv: The command that was run, as given.
        returncode: Its exit status.
        stdout: Captured standard output, decoded as UTF-8.
        stderr: Captured standard error, decoded as UTF-8.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command exited zero."""
        return self.returncode == 0


def _execute(
    argv: tuple[str, ...],
    *,
    cwd: str | PathLike[str] | None,
    env: dict[str, str] | None,
    timeout: float | None,
    stdin: str | None,
) -> ProcResult:
    """Run ``argv`` and capture its output.

    This is the only function in milhouse that calls :mod:`subprocess`. Tests
    monkeypatch it to replay recorded output instead.

    Args:
        argv: Command and arguments.
        cwd: Working directory, or ``None`` for the current one.
        env: Full environment for the child, or ``None`` to inherit.
        timeout: Seconds to wait before killing the child, or ``None``.
        stdin: Text written to the child's stdin, or ``None`` for no input.

    Returns:
        The captured result, whatever the exit status.

    Raises:
        ProcessError: The executable was not found, or the timeout expired.
    """
    try:
        # argv is always a list milhouse built; no shell is involved.
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProcessError(f"command not found: {argv[0]}", argv=argv) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProcessError(
            f"command timed out after {timeout}s: {_display(argv)}",
            argv=argv,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
        ) from exc
    return ProcResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def run(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    stdin: str | None = None,
    check: bool = True,
) -> ProcResult:
    """Run a command and return its captured output.

    Args:
        argv: Command and arguments. Never a shell string: milhouse does not
            invoke a shell, so no quoting is needed or honoured.
        cwd: Working directory for the child process.
        env: Full environment for the child, or ``None`` to inherit milhouse's.
        timeout: Seconds before the child is killed.
        stdin: Text to write to the child's stdin.
        check: Raise on a non-zero exit status. Pass ``False`` when a non-zero
            status is a normal answer rather than a failure.

    Returns:
        The captured result.

    Raises:
        ProcessError: The command was not found, timed out, or (with ``check``)
            exited non-zero.
    """
    argv = tuple(argv)
    log.debug("exec: %s", _display(argv))
    result = _execute(argv, cwd=cwd, env=env, timeout=timeout, stdin=stdin)
    if check and not result.ok:
        raise ProcessError(
            f"`{_display(argv)}` exited {result.returncode}: "
            f"{_first_line(result.stderr or result.stdout)}",
            argv=argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result


def run_json(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    stdin: str | None = None,
    allow_empty: bool = False,
) -> Any:
    """Run a command that emits JSON on stdout and return the parsed value.

    ``bd --json`` prints one JSON document on stdout and reports failure through
    its exit status, which is the shape this assumes. Some commands legitimately
    print nothing when there is no result, which ``allow_empty`` accommodates.

    :mod:`milhouse.herdr` deliberately does not come through here. herdr answers
    a failure with a JSON envelope on *stderr* and a non-zero status, so it has
    to read the response before judging the status, which is the opposite order.

    Args:
        argv: Command and arguments.
        cwd: Working directory for the child process.
        env: Full environment for the child, or ``None`` to inherit.
        timeout: Seconds before the child is killed.
        stdin: Text to write to the child's stdin.
        allow_empty: Return ``None`` instead of raising when stdout is blank.

    Returns:
        The parsed JSON document, or ``None`` for blank output under
        ``allow_empty``.

    Raises:
        ProcessError: The command failed, or its stdout was not valid JSON.
    """
    result = run(argv, cwd=cwd, env=env, timeout=timeout, stdin=stdin)
    text = result.stdout.strip()
    if not text:
        if allow_empty:
            return None
        raise ProcessError(
            f"`{_display(result.argv)}` produced no JSON output",
            argv=result.argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProcessError(
            f"`{_display(result.argv)}` produced invalid JSON: {exc}",
            argv=result.argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        ) from exc


def have(tool: str) -> str | None:
    """Return the resolved path of ``tool`` on ``PATH``, or ``None`` if absent.

    Args:
        tool: Executable name, e.g. ``"bd"``.

    Returns:
        The absolute path, or ``None`` when the tool is not installed.
    """
    return shutil.which(tool)


def _display(argv: tuple[str, ...]) -> str:
    """Render argv for an error message, without pretending to be shell-safe."""
    return " ".join(argv)


def _first_line(text: str) -> str:
    """Return the first non-blank line of ``text``, or a placeholder."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "(no output)"


def _as_text(value: str | bytes | None) -> str:
    """Coerce captured output from a timeout to text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value
