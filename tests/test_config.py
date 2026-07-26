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
    assert resolved.loop.max_iterations == 50
    assert resolved.loop.turn_timeout_ms == 1_800_000
    assert resolved.git.branch_strategy == "task"
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
    write_config(repo, "[loop]\nmax_iterations = 7\n")

    resolved = config_module.load(repo)

    assert resolved.loop.max_iterations == 7
    # Untouched keys in the same section keep their defaults.
    assert resolved.loop.turn_timeout_ms == 1_800_000


def test_env_overrides_the_file(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(repo, "[loop]\nmax_iterations = 7\n")
    monkeypatch.setenv("MILHOUSE_MAX_ITERATIONS", "9")

    assert config_module.load(repo).loop.max_iterations == 9


def test_flags_override_the_env(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MILHOUSE_MAX_ITERATIONS", "9")

    resolved = config_module.load(repo, overrides={"loop": {"max_iterations": 11}})

    assert resolved.loop.max_iterations == 11


def test_unset_flags_do_not_erase_configured_values(repo: Path) -> None:
    write_config(repo, "[loop]\nmax_iterations = 7\n")

    resolved = config_module.load(repo, overrides={"loop": {"max_iterations": None}})

    assert resolved.loop.max_iterations == 7


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
    write_config(repo, '[git]\nbranch_strategy = "whatever"\n')

    with pytest.raises(ConfigError) as caught:
        config_module.load(repo)

    assert "git.branch_strategy" in str(caught.value)
    assert caught.value.exit_code == 2


def test_a_non_integer_env_override_is_a_config_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MILHOUSE_MAX_ITERATIONS", "lots")

    with pytest.raises(ConfigError, match="must be an integer"):
        config_module.load(repo)


def test_run_dir_is_under_milhouse_runs(repo: Path) -> None:
    resolved = config_module.load(repo)

    assert resolved.run_dir("hello") == repo / ".milhouse" / "runs" / "hello"
