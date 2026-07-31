"""The acceptance scenario for the live renderer: a `--count 3` run, watched.

Everywhere else, `LiveRenderer` is driven by hand-built events
(`test_renderer.py`). This file drives it with a real `run()` over
`Parallel`, against a scripted fake runner
(:mod:`tests.doubles`) — the same shape `test_parallel.py` uses for
`--count N`, minus the assertions about what the run decided. What is asserted
here is only how it looked: a table redrawn in place, not a transcript that
scrolled.
"""

from __future__ import annotations

import io
import re

from rich.console import Console

from milhouse.config import Config
from milhouse.models import Issue
from milhouse.parallel import Parallel
from milhouse.policy import unattended
from milhouse.renderer import LiveRenderer
from milhouse.run import run as run_loop

from .doubles import FakeRepo, FakeTracker, build
from .test_parallel import with_worker_lanes

POLICY = unattended(max_attempts=3)

TARGET = Issue(id="bd-e", title="Add a hello command", status="open", issue_type="epic")


def _three_issues() -> FakeTracker:
    """Three independent open issues under `TARGET`, same shape as `test_parallel.py`'s five."""
    tracker = FakeTracker(epic=TARGET)
    tracker.issues = [
        Issue(id=f"bd-e.{n}", title=f"Do thing {n}", status="open", parent="bd-e")
        for n in range(1, 4)
    ]
    return tracker


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _plain(text: str) -> str:
    """Strip rich's colour and cursor-control codes, for content assertions."""
    return _ANSI.sub("", text)


def _forced_terminal(buffer: io.StringIO) -> Console:
    """A console that believes it is a real terminal, regardless of ``TERM``.

    Matches the helper in ``test_renderer.py``: the suite's ``_plain_output``
    fixture sets ``TERM=dumb``, which defeats ``force_terminal`` alone.
    """
    return Console(file=buffer, force_terminal=True, width=100, _environ={"TERM": "xterm"})


def test_a_count_3_run_against_a_fake_runner_redraws_in_place(config: Config) -> None:
    tracker = _three_issues()
    ids = [issue.id for issue in tracker.issues]
    session, _runner = build(
        config,
        tracker=tracker,
        script=["close", "close", "close"],
        repo=FakeRepo(),
        client=with_worker_lanes("bd-e", *ids),
        lane_key="bd-e",
        worker_lanes=True,
    )
    buffer = io.StringIO()
    renderer = LiveRenderer(
        console=_forced_terminal(buffer), scope="3 unfinished under bd-e", max_iterations=50
    )
    session.report = renderer.handle
    running = Parallel(count=3, max_iterations=50, poll_ms=0, sleep=lambda seconds: None)

    with renderer, session as opened:
        run_loop(opened, (TARGET,), policy=POLICY, max_iterations=50, body=running)

    assert all(issue.is_closed for issue in tracker.issues)
    output = buffer.getvalue()
    # Redrawn in place: rich's signature for erasing and repainting a region.
    assert "\x1b[2K" in output
    assert "\x1b[1A" in output or "\x1b[3A" in output
    # And the milestones are still there, scrolling above the table once each —
    # a run that only appended full frames would print every issue's sentence
    # once per frame it was still in, not once.
    plain = _plain(output)
    for issue_id in ids:
        assert plain.count(f"{issue_id}  → success:") == 1


def test_the_same_run_against_a_console_that_is_not_a_terminal_writes_no_redraw_codes(
    config: Config,
) -> None:
    """The counterpart: without a real terminal, nothing is redrawn mid-run.

    This is what makes `auto` safe to leave as the default: a `LiveRenderer`
    handed a file rather than a terminal never emits a control sequence, so
    the CLI's own `--progress auto` chooses `plain` instead and this class is
    not even reached in that case — but if it ever were, it degrades to a
    single final dump rather than a garbled stream of escape codes.
    """
    tracker = _three_issues()
    ids = [issue.id for issue in tracker.issues]
    session, _runner = build(
        config,
        tracker=tracker,
        script=["close", "close", "close"],
        repo=FakeRepo(),
        client=with_worker_lanes("bd-e", *ids),
        lane_key="bd-e",
        worker_lanes=True,
    )
    buffer = io.StringIO()
    renderer = LiveRenderer(console=Console(file=buffer, width=100), max_iterations=50)
    session.report = renderer.handle
    running = Parallel(count=3, max_iterations=50, poll_ms=0, sleep=lambda seconds: None)

    with renderer, session as opened:
        run_loop(opened, (TARGET,), policy=POLICY, max_iterations=50, body=running)

    assert "\x1b[" not in buffer.getvalue()
