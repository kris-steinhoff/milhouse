"""Tests for the preflight checks behind ``milhouse doctor``."""

from __future__ import annotations

import pytest

from milhouse import doctor, proc
from milhouse.config import Config

from .fakes import FakeProc, Reply

HERDR_STATUS_OK = """\
client:
  version: 0.7.5
  protocol: 17

server:
  status: running
  version: 0.7.5
  protocol: 17
  compatible: yes
  socket: /home/agent/.config/herdr/herdr.sock
"""

HERDR_STATUS_STOPPED = """\
client:
  version: 0.7.5
  protocol: 17

server:
  status: not running
"""

HERDR_STATUS_INCOMPATIBLE = """\
client:
  version: 0.7.5
  protocol: 18

server:
  status: running
  protocol: 17
  compatible: no
"""


@pytest.fixture
def all_present(monkeypatch: pytest.MonkeyPatch, fake_proc: FakeProc) -> FakeProc:
    """Every tool installed, herdr healthy, beads initialised."""
    monkeypatch.setattr(proc, "have", lambda tool: f"/usr/bin/{tool}")
    fake_proc.expect("bd version", Reply(stdout="bd version 1.1.0 (8e4e59d3)\n"))
    fake_proc.expect("bd list", Reply(stdout="[]"))
    fake_proc.expect("herdr --version", Reply(stdout="herdr 0.7.5\n"))
    fake_proc.expect("herdr status", Reply(stdout=HERDR_STATUS_OK))
    fake_proc.expect("git --version", Reply(stdout="git version 2.43.0\n"))
    fake_proc.expect("gh --version", Reply(stdout="gh version 2.96.0\n"))
    fake_proc.expect("claude --version", Reply(stdout="2.1.220 (Claude Code)\n"))
    return fake_proc


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {check.name: check for check in checks}


def test_everything_green(config: Config, all_present: FakeProc) -> None:
    checks = by_name(doctor.run_checks(config))

    assert all(check.ok for check in checks.values())
    assert checks["bd"].detail == "bd version 1.1.0 (8e4e59d3)"
    assert checks["herdr server"].detail == "running, protocol 17"


def test_the_agent_row_follows_the_configured_kind(config: Config, all_present: FakeProc) -> None:
    config.agent.kind = "codex"
    all_present.expect("codex --version", Reply(stdout="codex 1.0.0\n"))

    checks = by_name(doctor.run_checks(config))

    assert "codex" in checks
    assert "claude" not in checks


def test_a_missing_required_tool_fails(
    config: Config, all_present: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc, "have", lambda tool: None if tool == "bd" else f"/usr/bin/{tool}")

    checks = by_name(doctor.run_checks(config))

    assert checks["bd"].ok is False
    assert checks["bd"].required is True
    assert checks["beads db"].ok is False


def test_a_missing_optional_tool_is_not_required(
    config: Config, all_present: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc, "have", lambda tool: None if tool == "gh" else f"/usr/bin/{tool}")

    checks = by_name(doctor.run_checks(config))

    assert checks["gh"].ok is False
    assert checks["gh"].required is False


def test_an_uninitialised_beads_database_is_reported(config: Config, all_present: FakeProc) -> None:
    all_present.expect("bd list", Reply(stderr="no beads database found\n", returncode=1))

    checks = by_name(doctor.run_checks(config))

    assert checks["beads db"].ok is False
    assert "bd init" in checks["beads db"].detail


def test_a_stopped_herdr_server_fails(config: Config, all_present: FakeProc) -> None:
    all_present.expect("herdr status", Reply(stdout=HERDR_STATUS_STOPPED))

    checks = by_name(doctor.run_checks(config))

    assert checks["herdr server"].ok is False
    assert "not running" in checks["herdr server"].detail


def test_a_protocol_mismatch_fails(config: Config, all_present: FakeProc) -> None:
    all_present.expect("herdr status", Reply(stdout=HERDR_STATUS_INCOMPATIBLE))

    checks = by_name(doctor.run_checks(config))

    assert checks["herdr server"].ok is False
    assert "protocol mismatch" in checks["herdr server"].detail
