---
name: beads
description: Use when working in a repository that uses bd or Beads for durable project task tracking, issue dependencies, blocker management, multi-session handoff, or shared work memory. Trigger when the user asks to find ready work, claim or close tasks, create follow-up work, inspect blockers, recover project context, share issues with collaborators, sync a workspace, set up a fresh clone, or choose between local planning and persistent project tracking.
---

# Beads

Use Beads as the shared project task system. Local plans, scratch files, and personal memories are useful, but they are not the durable source of truth for project work.

## First Step

Run:

```bash
bd prime
```

If that prints nothing, check whether the repository has an active Beads workspace:

```bash
bd where
```

## Preferred Route

Use the `bd` CLI when shell access is available. It is the most compact and direct Beads interface.

## Core CLI Workflow

1. Find work:

```bash
bd ready
bd list --status=open
bd list --status=in_progress
```

2. Inspect before editing:

```bash
bd show <id>
```

3. Claim work atomically:

```bash
bd update <id> --claim
```

4. Create durable follow-up work when implementation reveals new tasks:

```bash
bd create "Short title" --description="Why this exists and what needs to be done" --type=task --priority=2
```

5. Close completed work:

```bash
bd close <id> --reason="Completed"
```

## Sync

Issue data does not travel with `git push`. One git remote carries two independent things under different refs:

| What | Ref | Command |
| ----------- | ------------------ | ------------------------------- |
| Source code | `refs/heads/main` | `git push` and `git pull` |
| Issue data  | `refs/dolt/data`   | `bd dolt push` and `bd dolt pull` |

The local Dolt database is the source of truth. It lives under `.beads/` and is gitignored, so an unpushed database exists on exactly one machine.

Pull at the start of a session, before claiming anything:

```bash
bd dolt pull
```

Push after closing work, so the next person or agent sees it:

```bash
bd dolt push
```

On a fresh clone, join the existing database rather than creating a new one:

```bash
bd bootstrap
```

`bd init` creates a new database and is not how you join an existing project.

Dolt three-way merges issue data, so concurrent edits to different issues merge without conflict.

### Sync And The Conservative Profile

The conservative profile says not to sync without being asked. Read that as a rule about publishing:

- `bd dolt pull` and `bd bootstrap` only read from the remote. They are safe to run unprompted, and running them avoids working from a stale view.
- `bd dolt push` publishes. Ask first, unless the active profile grants that authority.

If work is closed but unpushed, say so at handoff and give the exact command, rather than leaving it unmentioned.

## What Belongs In Beads

Use Beads for:

- shared project tasks
- blockers and dependencies
- discovered follow-up work
- work that must survive thread reset, compaction, or handoff
- status that another person or agent should be able to resume

Use agent-local planning tools only for the current turn's execution checklist. Do not treat them as shared project state.

## Rules

- Do not create markdown TODO files as the source of truth when Beads is available.
- Do not use `bd edit`; it opens an interactive editor. Use `bd update` flags instead.
- Prefer `--json` when parsing `bd` output programmatically.
- If hooks are installed, `bd prime` may already be injected. Run it manually when context is missing.
- Do not auto-close or mutate tasks unless the work is actually complete.
