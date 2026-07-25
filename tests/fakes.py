"""Test doubles for everything outside the process.

Tests fake at the :mod:`milhouse.proc` boundary rather than at each client, so
the JSON parsing, error mapping, and argv construction in ``tracker/`` and
``herdr.py`` all stay under test. :class:`FakeProc` replaces
``milhouse.proc._execute``, the one function that touches :mod:`subprocess`.

Responses are matched on an argv prefix, longest match first, so a test can set a
broad default and then override one specific command.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

from milhouse import proc

__all__ = ["FakeProc", "Reply", "install"]

Responder = Callable[[tuple[str, ...]], "Reply"]


@dataclass
class Reply:
    """A canned subprocess result.

    Attributes:
        stdout: What the fake command prints.
        stderr: What it prints to stderr.
        returncode: Its exit status.
    """

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


@dataclass
class FakeProc:
    """A stand-in for the outside world, matching commands on an argv prefix.

    Every call is appended to :attr:`calls`, so a test can assert on the exact
    commands milhouse ran as well as on what it did with the output.
    """

    replies: dict[tuple[str, ...], Reply | Responder | list[Reply]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    strict: bool = True
    """Raise on an unmatched command instead of returning empty success."""

    def expect(
        self,
        argv_prefix: str | list[str] | tuple[str, ...],
        reply: Reply | Responder | list[Reply] | str,
    ) -> FakeProc:
        """Register a reply for commands starting with ``argv_prefix``.

        Args:
            argv_prefix: Prefix to match, as a list or a space-separated string.
            reply: A :class:`Reply`, a callable taking argv, a list of replies
                consumed one call at a time, or a string treated as stdout.

        Returns:
            ``self``, so registrations can be chained.
        """
        key = tuple(argv_prefix.split()) if isinstance(argv_prefix, str) else tuple(argv_prefix)
        self.replies[key] = Reply(stdout=reply) if isinstance(reply, str) else reply
        return self

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Any = None,
        env: Any = None,
        timeout: Any = None,
        stdin: Any = None,
    ) -> proc.ProcResult:
        """Stand in for :func:`milhouse.proc._execute`."""
        self.calls.append(argv)
        reply = self._match(argv)
        if reply is None:
            if self.strict:
                raise AssertionError(f"FakeProc has no reply for: {' '.join(argv)}")
            reply = Reply()
        return proc.ProcResult(
            argv=argv,
            returncode=reply.returncode,
            stdout=reply.stdout,
            stderr=reply.stderr,
        )

    def _match(self, argv: tuple[str, ...]) -> Reply | None:
        """Find the longest registered prefix matching ``argv``."""
        for key in sorted(self.replies, key=len, reverse=True):
            if argv[: len(key)] == key:
                return self._resolve(key, argv)
        return None

    def _resolve(self, key: tuple[str, ...], argv: tuple[str, ...]) -> Reply:
        """Turn a registered value into a concrete reply for this call."""
        value = self.replies[key]
        if isinstance(value, Reply):
            return value
        if isinstance(value, list):
            # isinstance cannot carry the element type, and a list subclass could
            # also be a Responder, so the narrowed type is wider than the dict's.
            queue = cast("list[Reply]", value)
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return value(argv)

    def commands(self, *prefix: str) -> Iterator[tuple[str, ...]]:
        """Yield recorded calls starting with ``prefix``.

        Args:
            *prefix: argv words to match at the start of a call.

        Yields:
            Each matching recorded argv, in call order.
        """
        for call in self.calls:
            if call[: len(prefix)] == prefix:
                yield call

    def ran(self, *prefix: str) -> bool:
        """Whether any recorded call starts with ``prefix``."""
        return any(True for _ in self.commands(*prefix))


def install(monkeypatch: Any, fake: FakeProc | None = None) -> FakeProc:
    """Route every subprocess in milhouse through a :class:`FakeProc`.

    Args:
        monkeypatch: pytest's ``monkeypatch`` fixture.
        fake: An existing fake to install, or ``None`` to create one.

    Returns:
        The installed fake.
    """
    fake = fake or FakeProc()
    monkeypatch.setattr(proc, "_execute", fake)
    return fake
