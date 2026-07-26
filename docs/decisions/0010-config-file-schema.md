# 0010 — `.milhouse/config.toml` schema

**Status:** accepted

## Context

The config file was referenced by the design and never defined. It needs to hold at least the agent kind and its args, the default caps, and the turn timeout, without becoming a place where every internal constant leaks into a user-facing contract.

## Decision

Five sections, all optional, resolved as defaults < file < environment < flags, merging key by key. The full reference with types, defaults, and environment overrides is [docs/configuration.md](../configuration.md); this ADR records why the shape is what it is.

| Section     | Holds                                                                                   |
| ----------- | --------------------------------------------------------------------------------------- |
| `[agent]`   | `kind`, `args`, `start_timeout_ms`, `exit_keys`                                         |
| `[loop]`    | `max_iterations`, `max_attempts`, `turn_timeout_ms`, `on_blocked`, `blocked_timeout_ms` |
| `[git]`     | `branch_strategy`, `branch_prefix`                                                      |
| `[tracker]` | `label`, `metadata_key`                                                                 |
| `[herdr]`   | `workspace`, `read_lines`, `read_source`                                                |

Rules that fall out of this:

- **The file is committed.** It describes how this repository should be worked on, which is a property of the repo, not of a machine. Machine-specific settings go in the environment.
- **Every key is validated** by a pydantic model, and a bad key is a `ConfigError` (exit code 2) before anything is started, not a confusing failure three iterations in.
- **`None` overrides are dropped**, so an unset CLI flag never erases a configured value.
- **Prompt templates are not configurable.** They ship in the package and are versioned with the code, so "which milhouse produced this run" is answerable.

## Consequences

Adding a key means adding a field to the pydantic model _and_ a row to `docs/configuration.md`. That coupling is deliberate: an undocumented config key is a bug.

`[tracker]` exists mostly so a repo that already uses the `milhouse` label for something else can move out of the way. Changing it after issues exist orphans them, which is why it is documented as a set-once key rather than a knob.
