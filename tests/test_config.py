"""Tests for the configuration layering: defaults < file < env < flags."""

from __future__ import annotations

from pathlib import Path

import pytest

from milhouse import config as config_module
from milhouse.errors import ConfigError


def write_config(repo: Path, body: str) -> None:
    path = config_module.config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_defaults_apply_without_a_config_file(repo: Path) -> None:
    resolved = config_module.load(repo)

    assert resolved.agent.kind == "claude"
    assert resolved.agent.args == []
    assert resolved.agent.turn_timeout_ms == 1_800_000
    assert resolved.tracker.label is None
    assert resolved.tracker.parent is None
    assert resolved.run.max_iterations == 50
    assert resolved.run.max_attempts == 3
    assert resolved.herdr.read_source == "visible"


def test_default_exit_keys_use_the_spelling_herdr_accepts(repo: Path) -> None:
    """The short key forms herdr accepts are a trap, and must not drift back in.

    ``c-c`` is accepted, so the short form looks correct, but ``c-d`` is
    rejected with ``invalid_key``. A dogfood run reached the end of a successful
    turn and then failed to exit the agent for exactly this reason.
    """
    resolved = config_module.load(repo)

    assert resolved.agent.exit_keys == ["ctrl+c", "ctrl+c", "ctrl+d"]
    for key in resolved.agent.exit_keys:
        assert key.startswith("ctrl+"), f"{key} is not the spelling herdr accepts for every key"


def test_file_overrides_defaults_key_by_key(repo: Path) -> None:
    write_config(repo, "[agent]\nturn_timeout_ms = 7000\n")

    resolved = config_module.load(repo)

    assert resolved.agent.turn_timeout_ms == 7000
    # Untouched keys in the same section keep their defaults.
    assert resolved.agent.kind == "claude"


def test_env_overrides_the_file(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(repo, "[agent]\nturn_timeout_ms = 7000\n")
    monkeypatch.setenv("MILHOUSE_TURN_TIMEOUT_MS", "9000")

    assert config_module.load(repo).agent.turn_timeout_ms == 9000


def test_flags_override_the_env(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MILHOUSE_AGENT_KIND", "codex")

    resolved = config_module.load(repo, overrides={"agent": {"kind": "gemini"}})

    assert resolved.agent.kind == "gemini"


def test_unset_flags_do_not_erase_configured_values(repo: Path) -> None:
    write_config(repo, '[agent]\nkind = "codex"\n')

    resolved = config_module.load(repo, overrides={"agent": {"kind": None}})

    assert resolved.agent.kind == "codex"


def test_agent_args_are_shell_split_from_the_env(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MILHOUSE_AGENT_ARGS", "--permission-mode 'accept edits'")

    assert config_module.load(repo).agent.args == ["--permission-mode", "accept edits"]


def test_herdr_workspace_id_is_picked_up_from_the_ambient_pane(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "wA")

    assert config_module.load(repo).herdr.workspace == "wA"


def test_explicit_milhouse_workspace_beats_the_ambient_one(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "wA")
    monkeypatch.setenv("MILHOUSE_WORKSPACE", "wB")

    assert config_module.load(repo).herdr.workspace == "wB"


def test_malformed_toml_is_a_config_error(repo: Path) -> None:
    write_config(repo, "[loop\n")

    with pytest.raises(ConfigError, match="not valid TOML"):
        config_module.load(repo)


def test_an_invalid_enum_value_is_a_config_error(repo: Path) -> None:
    write_config(repo, "[herdr]\nread_source = 'whatever'\n")

    with pytest.raises(ConfigError) as caught:
        config_module.load(repo)

    assert "herdr.read_source" in str(caught.value)
    assert caught.value.exit_code == 2


def test_a_non_integer_env_override_is_a_config_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MILHOUSE_TURN_TIMEOUT_MS", "lots")

    with pytest.raises(ConfigError, match="must be an integer"):
        config_module.load(repo)


def test_run_dir_is_under_milhouse_runs(repo: Path) -> None:
    resolved = config_module.load(repo)

    assert resolved.run_dir() == repo / ".milhouse" / "runs"


def test_a_repository_can_fence_the_ready_queue(repo: Path) -> None:
    """The whole ready queue is the default; a fence is what a repo opts into."""
    write_config(repo, '[tracker]\nlabel = "agent"\nparent = "bd-e"\n')

    resolved = config_module.load(repo)

    assert resolved.tracker.label == "agent"
    assert resolved.tracker.parent == "bd-e"


def test_the_run_caps_are_layered_like_everything_else(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(repo, "[run]\nmax_iterations = 12\nmax_attempts = 5\n")
    monkeypatch.setenv("MILHOUSE_RUN_MAX_ITERATIONS", "20")

    resolved = config_module.load(repo, overrides={"run": {"max_attempts": 2}})

    assert resolved.run.max_iterations == 20
    assert resolved.run.max_attempts == 2


@pytest.mark.parametrize("key", ["max_iterations", "max_attempts"])
@pytest.mark.parametrize("value", [0, -1])
def test_a_run_cap_below_one_is_refused(repo: Path, key: str, value: int) -> None:
    """A ceiling of zero is a run that does nothing and says it hit the ceiling."""
    write_config(repo, f"[run]\n{key} = {value}\n")

    with pytest.raises(ConfigError, match=f"run.{key}"):
        config_module.load(repo)
