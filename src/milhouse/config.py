"""Layered configuration: defaults < ``.milhouse/config.toml`` < environment < flags.

Later layers win key by key, not section by section, so a config file that sets
only ``[loop] max_iterations`` keeps every other default. :func:`load` is the
only entry point; it returns a fully resolved, validated :class:`Config`.

Every key here is documented in ``docs/configuration.md`` with its type,
default, and environment override. Adding a key means adding a row there.
"""

from __future__ import annotations

import os
import shlex
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .errors import ConfigError

__all__ = [
    "AgentConfig",
    "Config",
    "GitConfig",
    "HerdrConfig",
    "LoopConfig",
    "TrackerConfig",
    "config_path",
    "load",
    "state_dir",
]

CONFIG_RELPATH = Path(".milhouse/config.toml")
"""Where a repo's committed milhouse config lives, relative to the repo root."""

RUNS_RELPATH = Path(".milhouse/runs")
"""Where per-run bookkeeping lives. Gitignored; safe to delete."""

OnBlocked = Literal["wait", "skip", "abort"]
BranchStrategy = Literal["task", "current"]


class AgentConfig(BaseModel):
    """How to start the interactive agent in a herdr pane."""

    kind: str = "claude"
    """``herdr agent start --kind`` value. Any kind herdr supports."""

    args: list[str] = Field(default_factory=list)
    """Extra arguments passed to the agent binary after ``--``."""

    start_timeout_ms: int = 60_000
    """How long ``herdr agent start`` may take to report the agent ready."""

    exit_timeout_ms: int = 8_000
    """How long to wait for the pane to return to a shell prompt after exit_keys."""

    exit_keys: list[str] = Field(default_factory=lambda: ["c-c", "c-c", "c-d"])
    """Keys returning the pane from the agent TUI to a shell prompt.

    herdr spells control keys ``c-c``, not ``ctrl-c``, which it rejects.
    """


class LoopConfig(BaseModel):
    """Guardrails on the ralph loop."""

    max_iterations: int = 50
    """Hard ceiling on iterations for a whole run."""

    max_attempts: int = 3
    """Failed attempts on one issue before it is marked blocked and skipped."""

    turn_timeout_ms: int = 1_800_000
    """Bound on a single ``herdr agent prompt --wait`` turn. Default 30 minutes."""

    on_blocked: OnBlocked = "wait"
    """What to do when herdr reports the agent is waiting on a human."""

    blocked_timeout_ms: int = 900_000
    """How long ``on_blocked = "wait"`` waits for a human. Default 15 minutes."""


class GitConfig(BaseModel):
    """Where the loop's commits land."""

    branch_strategy: BranchStrategy = "task"
    """``task`` creates one branch per task definition; ``current`` stays put."""

    branch_prefix: str = "milhouse/"
    """Prefix for branches created under the ``task`` strategy."""


class TrackerConfig(BaseModel):
    """How milhouse marks its own issues in beads."""

    label: str = "milhouse"
    """Label applied to every issue milhouse creates."""

    metadata_key: str = "milhouse_task"
    """Bead metadata key holding the task id that owns an epic."""


class HerdrConfig(BaseModel):
    """Workspace and transcript settings for the herdr client."""

    workspace: str | None = None
    """Reuse this workspace id instead of creating one. ``None`` creates one."""

    read_lines: int = 400
    """Lines of pane transcript captured after each turn."""

    read_source: Literal["visible", "recent", "recent-unwrapped", "detection"] = "visible"
    """``herdr agent read --source`` value used for the post-turn transcript."""


class Config(BaseModel):
    """The fully resolved configuration for one milhouse invocation."""

    repo_root: Path
    """Root of the git repository milhouse is operating on."""

    agent: AgentConfig = Field(default_factory=AgentConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    herdr: HerdrConfig = Field(default_factory=HerdrConfig)

    def runs_dir(self) -> Path:
        """Absolute path of ``.milhouse/runs`` for this repo."""
        return self.repo_root / RUNS_RELPATH

    def run_dir(self, task_slug: str) -> Path:
        """Absolute path of the bookkeeping directory for one task.

        Args:
            task_slug: The task's filesystem-safe slug.

        Returns:
            ``<repo>/.milhouse/runs/<task_slug>``. Not created by this call.
        """
        return self.runs_dir() / task_slug


def config_path(repo_root: Path) -> Path:
    """Absolute path of the config file for ``repo_root`` (which need not exist)."""
    return repo_root / CONFIG_RELPATH


def state_dir(repo_root: Path) -> Path:
    """Absolute path of ``.milhouse`` for ``repo_root``."""
    return repo_root / ".milhouse"


def load(repo_root: Path, *, overrides: dict[str, Any] | None = None) -> Config:
    """Resolve configuration for ``repo_root``.

    Layers, in increasing precedence: built-in defaults, ``.milhouse/config.toml``,
    ``MILHOUSE_*`` environment variables, then ``overrides`` (the CLI flags).
    ``None`` values in ``overrides`` are ignored, so an unset flag does not erase
    a configured value.

    Args:
        repo_root: Root of the repository being operated on.
        overrides: Nested ``{"section": {"key": value}}`` mapping from CLI flags.

    Returns:
        A validated :class:`Config`.

    Raises:
        ConfigError: The TOML is malformed, or a value fails validation.
    """
    data: dict[str, Any] = {"repo_root": str(repo_root)}
    _merge(data, _from_file(config_path(repo_root)))
    _merge(data, _from_env(os.environ))
    _merge(data, _prune(overrides or {}))
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {_explain(exc)}") from exc


def _from_file(path: Path) -> dict[str, Any]:
    """Read ``path`` as TOML, returning ``{}`` when it does not exist.

    Raises:
        ConfigError: The file exists but is not readable or not valid TOML.
    """
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


#: Environment variable -> (section, key, parser). Documented in docs/configuration.md.
_ENV_MAP: dict[str, tuple[str, str, str]] = {
    "MILHOUSE_AGENT_KIND": ("agent", "kind", "str"),
    "MILHOUSE_AGENT_ARGS": ("agent", "args", "argv"),
    "MILHOUSE_AGENT_START_TIMEOUT_MS": ("agent", "start_timeout_ms", "int"),
    "MILHOUSE_AGENT_EXIT_TIMEOUT_MS": ("agent", "exit_timeout_ms", "int"),
    "MILHOUSE_MAX_ITERATIONS": ("loop", "max_iterations", "int"),
    "MILHOUSE_MAX_ATTEMPTS": ("loop", "max_attempts", "int"),
    "MILHOUSE_TURN_TIMEOUT_MS": ("loop", "turn_timeout_ms", "int"),
    "MILHOUSE_ON_BLOCKED": ("loop", "on_blocked", "str"),
    "MILHOUSE_BLOCKED_TIMEOUT_MS": ("loop", "blocked_timeout_ms", "int"),
    "MILHOUSE_BRANCH_STRATEGY": ("git", "branch_strategy", "str"),
    "MILHOUSE_BRANCH_PREFIX": ("git", "branch_prefix", "str"),
    # HERDR_WORKSPACE_ID first: later entries win, and an explicit MILHOUSE_
    # setting should beat the one herdr exports into every pane it launches.
    "HERDR_WORKSPACE_ID": ("herdr", "workspace", "str"),
    "MILHOUSE_WORKSPACE": ("herdr", "workspace", "str"),
}


def _from_env(environ: dict[str, str] | os._Environ[str]) -> dict[str, Any]:
    """Translate ``MILHOUSE_*`` (and ``HERDR_WORKSPACE_ID``) into a config mapping.

    ``MILHOUSE_WORKSPACE`` is applied after ``HERDR_WORKSPACE_ID`` so an explicit
    milhouse setting wins over the ambient one herdr exports into its panes.

    Raises:
        ConfigError: A variable that must hold an integer does not.
    """
    out: dict[str, Any] = {}
    for name, (section, key, kind) in _ENV_MAP.items():
        raw = environ.get(name)
        if raw is None or raw == "":
            continue
        if kind == "int":
            try:
                value: Any = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
        elif kind == "argv":
            value = shlex.split(raw)
        else:
            value = raw
        out.setdefault(section, {})[key] = value
    return out


def _prune(overrides: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so an unset CLI flag does not clobber configuration."""
    out: dict[str, Any] = {}
    for section, values in overrides.items():
        if not isinstance(values, dict):
            if values is not None:
                out[section] = values
            continue
        kept = {key: value for key, value in values.items() if value is not None}
        if kept:
            out[section] = kept
    return out


def _merge(base: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Merge ``incoming`` into ``base`` one key deep, in place."""
    for section, values in incoming.items():
        if isinstance(values, dict) and isinstance(base.get(section), dict):
            base[section].update(values)
        else:
            base[section] = values


def _explain(exc: ValidationError) -> str:
    """Render a pydantic validation error as a compact ``section.key: reason`` list."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
