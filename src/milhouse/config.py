"""Layered configuration: defaults < ``.milhouse/config.toml`` < environment < flags.

Later layers win key by key, not section by section, so a config file that sets
only ``[agent] kind`` keeps every other default. :func:`load` is the
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
    "HerdrConfig",
    "LaneConfig",
    "RunConfig",
    "TrackerConfig",
    "VerifyConfig",
    "config_path",
    "load",
    "state_dir",
]

CONFIG_RELPATH = Path(".milhouse/config.toml")
"""Where a repo's committed milhouse config lives, relative to the repo root."""

RUNS_RELPATH = Path(".milhouse/runs")
"""Where per-run bookkeeping lives. Gitignored; safe to delete."""


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

    turn_timeout_ms: int = 1_800_000
    """Bound on a single ``herdr agent prompt --wait`` turn. Default 30 minutes.

    A wedged agent cannot hang a step forever, and the turn is classified
    ``timeout`` rather than left running.
    """

    exit_keys: list[str] = Field(default_factory=lambda: ["ctrl+c", "ctrl+c", "ctrl+d"])
    """Keys returning the pane from the agent TUI to a shell prompt.

    Use the ``ctrl+`` spelling. herdr also accepts ``c-c`` and ``C-c``, but not
    every control key has a short form: ``c-d`` is rejected with ``invalid_key``
    while ``ctrl+d`` works. ``ctrl-c``, with a hyphen, is rejected too.
    """


class RunConfig(BaseModel):
    """What bounds one ``milhouse run``.

    The guardrails that only mean anything once nobody is watching
    (:doc:`ADR 0022 <../../docs/decisions/0022-the-loop-is-earned>`). The
    section is ``[run]`` rather than the ``[loop]`` that ADR 0017 deleted, so an
    old config file cannot silently start meaning something new.

    A key here says what any run of this repository may not exceed, rather than
    what one invocation is doing, which is why the width is ``max_parallel`` and
    the flag that overrides it is ``--count``
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).
    They are the one pair in milhouse that do not share a name, so both places
    that document configuration carry the mapping explicitly.
    """

    max_iterations: int = Field(default=50, ge=1)
    """Turns one run may take before it stops and reports.

    A ceiling on turns rather than on spend, and turns are not the same size
    (:doc:`ADR 0012 <../../docs/decisions/0012-no-cost-controls-in-v1>`). Zero
    is rejected: a run that stops at the ceiling having done nothing is worse
    than an error, because it reports a stop reason that sounds like progress.
    """

    max_attempts: int = Field(default=3, ge=1)
    """Attempts one issue gets before the run sets it aside and moves on.

    Counted over the whole audit history rather than over one run, so
    re-running does not hand a hopeless issue three more turns.
    """

    max_parallel: int = Field(default=1, ge=1)
    """Turns one run may keep in flight at once. ``--count N`` overrides it.

    One by default, which is the serial run
    (:doc:`ADR 0023 <../../docs/decisions/0023-a-run-has-one-lane>`) exactly:
    no worker lanes, and nothing to merge. Above one, each issue gets a worker
    lane branched from the integration branch and merged back into it
    (:doc:`ADR 0024 <../../docs/decisions/0024-an-integration-lane-and-worker-lanes>`).

    Zero is rejected for the reason ``max_iterations`` rejects it: a run that
    may start no turns is a run that does nothing and reports a stop reason
    sounding like progress. A width above what the dependency graph can use is
    not rejected, because it is harmless and ``--dry-run`` says so.
    """

    poll_ms: int = Field(default=5_000, ge=1)
    """How often a concurrent run checks whether a lane has settled.

    Matches :data:`milhouse.parallel.DEFAULT_POLL_MS`, which is where the
    reasoning about the value lives. Ignored by a serial run, which waits on
    each turn and has nothing to poll.

    Not zero: every poll asks herdr about each open lane and re-reads the audit
    trail, so a run that polled continuously would spend a subprocess per lane
    per pass against turns that take minutes.
    """


class VerifyConfig(BaseModel):
    """How milhouse checks an issue the agent says it finished.

    Empty by default, which means milhouse takes the agent at its word
    (:doc:`ADR 0016 <../../docs/decisions/0016-milhouse-verifies>`).
    """

    command: list[str] = Field(default_factory=list)
    """The repository's own gate, e.g. ``["uv", "run", "pytest", "-q"]``.

    Run in the repository root after an iteration that closed its issue. No
    shell is involved, so this is argv rather than a command line.
    """

    timeout_ms: int = 600_000
    """How long the command may take before it counts as failed. Default 10 minutes."""


class LaneConfig(BaseModel):
    """Where an issue's agent works.

    A lane is a herdr worktree labelled with the issue id
    (:doc:`ADR 0020 <../../docs/decisions/0020-a-lane-is-a-herdr-worktree>`).
    herdr chooses the checkout path, under ``~/.herdr/worktrees``, so nothing
    here says where the lanes go.
    """

    branch_prefix: str = "milhouse/"
    """Prefix for the branch a lane is created on, e.g. ``milhouse/bd-e.1``."""


class TrackerConfig(BaseModel):
    """Which issues in the tracker milhouse is allowed to work.

    Unfiltered by default: a repository whose beads database is only agent work
    needs no fence. Set one where the ready queue also carries issues that were
    never meant for an agent
    (:doc:`ADR 0018 <../../docs/decisions/0018-no-task-milhouse-works-the-ready-queue>`).
    """

    label: str | None = None
    """Only work issues carrying this label. ``None`` considers every issue."""

    parent: str | None = None
    """Only work issues under this epic. ``None`` considers the whole repository."""


class HerdrConfig(BaseModel):
    """Workspace and transcript settings for the herdr client."""

    workspace: str | None = None
    """Reuse this workspace id instead of creating one. ``None`` creates one."""

    self_pane: str | None = None
    """The pane milhouse is itself running in, which it must never start an agent in.

    Set from ``HERDR_PANE_ID``, which herdr exports into every pane it launches.
    Running ``milhouse step`` from inside a pane is the normal case, and that
    pane also belongs to the workspace ``HERDR_WORKSPACE_ID`` names, so without
    this milhouse can pick it to work in and kill the session that launched it.
    """

    read_lines: int = 400
    """Lines of pane transcript captured after each turn."""

    read_source: Literal["visible", "recent", "recent-unwrapped", "detection"] = "visible"
    """``herdr agent read --source`` value used for the post-turn transcript."""


class Config(BaseModel):
    """The fully resolved configuration for one milhouse invocation."""

    repo_root: Path
    """Root of the git repository milhouse is operating on."""

    agent: AgentConfig = Field(default_factory=AgentConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    lane: LaneConfig = Field(default_factory=LaneConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    herdr: HerdrConfig = Field(default_factory=HerdrConfig)

    def run_dir(self) -> Path:
        """Absolute path of ``.milhouse/runs`` for this repo. Not created by this call."""
        return self.repo_root / RUNS_RELPATH


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
    "MILHOUSE_TURN_TIMEOUT_MS": ("agent", "turn_timeout_ms", "int"),
    "MILHOUSE_RUN_MAX_ITERATIONS": ("run", "max_iterations", "int"),
    "MILHOUSE_RUN_MAX_ATTEMPTS": ("run", "max_attempts", "int"),
    "MILHOUSE_RUN_MAX_PARALLEL": ("run", "max_parallel", "int"),
    "MILHOUSE_RUN_POLL_MS": ("run", "poll_ms", "int"),
    "MILHOUSE_VERIFY_COMMAND": ("verify", "command", "argv"),
    "MILHOUSE_VERIFY_TIMEOUT_MS": ("verify", "timeout_ms", "int"),
    "MILHOUSE_LANE_BRANCH_PREFIX": ("lane", "branch_prefix", "str"),
    "MILHOUSE_TRACKER_LABEL": ("tracker", "label", "str"),
    "MILHOUSE_TRACKER_PARENT": ("tracker", "parent", "str"),
    # HERDR_WORKSPACE_ID first: later entries win, and an explicit MILHOUSE_
    # setting should beat the one herdr exports into every pane it launches.
    "HERDR_WORKSPACE_ID": ("herdr", "workspace", "str"),
    "MILHOUSE_WORKSPACE": ("herdr", "workspace", "str"),
    "HERDR_PANE_ID": ("herdr", "self_pane", "str"),
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
