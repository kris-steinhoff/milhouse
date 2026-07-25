"""Shared fixtures.

The important one is ``fake_proc``: it installs :class:`tests.fakes.FakeProc` over
:func:`milhouse.proc._execute`, so no test in the default suite can accidentally
run a real ``bd``, ``herdr``, or ``git``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from milhouse.config import Config

from .fakes import FakeProc, install


@pytest.fixture
def fake_proc(monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    """Route every subprocess through a fake, and record the calls."""
    return install(monkeypatch)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An empty directory standing in for a repository root."""
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def config(repo: Path) -> Config:
    """A default configuration rooted at the ``repo`` fixture.

    ``exit_timeout_ms`` is zeroed so tests that exercise the pane-replacement
    fallback do not spend the real eight-second poll waiting for a fake.
    """
    config = Config(repo_root=repo)
    config.agent.exit_timeout_ms = 0
    return config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own MILHOUSE_/HERDR_ environment out of the tests."""
    for name in [n for n in os.environ if n.startswith(("MILHOUSE_", "HERDR_"))]:
        monkeypatch.delenv(name, raising=False)
