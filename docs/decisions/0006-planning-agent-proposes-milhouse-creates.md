# 0006 — The planning agent proposes a plan; milhouse creates the issues

**Status:** accepted

## Context

The approval guardrail promises that proposed issues are shown before creation.
A planning agent with `bd` on its `PATH` will simply create them, and there is
nothing left to approve. The guardrail has to be structural, not a request.

## Decision

The planning agent does not call `bd create`. It writes a **plan file** —
`.milhouse/runs/<task_slug>/plan.json` — and stops. milhouse reads that file,
shows the tree, asks for confirmation unless `--yes`, and creates the issues
itself.

The plan format is milhouse's own, small enough to state in full in the planning
prompt:

```json
{
  "issues": [
    {
      "key": "add-command",
      "title": "Add the hello subcommand",
      "type": "task",
      "priority": 1,
      "description": "…",
      "acceptance": "…",
      "blocked_by": []
    },
    {
      "key": "document",
      "title": "Document the hello subcommand",
      "type": "task",
      "blocked_by": ["add-command"]
    }
  ]
}
```

`key` is a plan-local handle used only to express `blocked_by`; `bd` assigns the
real ids on creation.

milhouse then creates the issues itself, in dependency order:

```sh
bd create "<title>" --type <type> --parent <epic-id> --description … --json
bd dep add <child-id> <blocker-id>            # once every issue exists
```

Individual `bd create` calls rather than `bd create --graph <plan.json>`: the
graph format is undocumented in `bd --help`, and creating issues one at a time is
what lets milhouse own `--parent`, `--labels`, and the metadata rather than
trusting a file the agent wrote.

Validation before anything is created:

1. The file exists and is valid JSON with a non-empty `issues` array.
2. Every issue has a non-empty `title` and a unique, non-empty `key`.
3. Every `blocked_by` entry names another issue in the same plan.
4. The `blocked_by` graph is acyclic (otherwise `bd ready` would never return
   anything and the loop would exit immediately claiming success).

Any failure is a planning failure: milhouse reports which rule broke, keeps
`plan.json` for inspection, and exits rather than guessing.

## Consequences

- The approval guardrail is real. Nothing reaches beads without milhouse putting
  it there.
- "Did the planning agent succeed?" has a concrete answer — a valid plan file —
  rather than being inferred from a transcript.
- A bad decomposition is visible before it costs a single iteration, and the plan
  file is editable by hand before confirming.
- The planning prompt has to be explicit that `bd create` is off-limits during
  planning. An agent that ignores that produces issues milhouse did not approve;
  the epic-level `milhouse_task` metadata is still applied only by milhouse, so
  those strays are not picked up by the loop, but they do need cleaning up.
- The epic itself is created by milhouse, not described in the plan file, so the
  `milhouse_task` metadata of [ADR 0002](0002-link-issues-via-bead-metadata.md)
  cannot be forged by the agent.
