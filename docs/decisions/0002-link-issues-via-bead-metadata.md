# 0002 — Link issues to task definitions with bead metadata

**Status:** retired by [ADR 0018](0018-no-task-milhouse-works-the-ready-queue.md). Kept as the record of why the machinery existed.

## Context

Re-running `milhouse run` against the same task definition must find the existing decomposition instead of planning it again. That needs a stable, queryable link between "the markdown file the user pointed at" and "the epic in beads".

## Decision

Every task definition gets a stable `task_id`:

| Source       | `task_id`                    |
| ------------ | ---------------------------- |
| Local file   | `file:<repo-relative-path>`  |
| GitHub issue | `gh:<owner>/<repo>#<number>` |

milhouse creates one **epic** carrying `--metadata '{"milhouse_task":"<task_id>"}'` plus `--labels milhouse`, with children under `--parent <epic-id>`.

GitHub-sourced tasks also set `--external-ref gh-<number>` so beads can round-trip the link.

Both the label and the metadata key are configurable (`[tracker]`), but changing either orphans issues created under the old value.

## Consequences

Three questions the loop needs are answered by `bd` directly, with no state of our own:

- **Decomposed already?** `bd list --metadata-field milhouse_task=<id> --type epic --json`
- **What next?** `bd ready --parent <epic-id> --claim --json --limit 1`
- **Done?** the same `bd ready` returns empty

Note the asymmetry in the `bd` surface: `bd create` takes `--metadata` with a JSON blob, while `--set-metadata key=value` exists only on `bd update`.

The cost is that `task_id` is only as stable as the path. Renaming a task file orphans its epic, and milhouse will plan the task a second time. That is recoverable (`bd update <epic> --set-metadata milhouse_task=file:<new-path>`) and is documented in [troubleshooting](../troubleshooting.md).
