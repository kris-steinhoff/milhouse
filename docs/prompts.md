# Prompts

For a ralph loop the prompt _is_ the product. milhouse ships two, both inside the package rather than user-configurable, so a run is reproducible from a milhouse version. Every prompt change is a behaviour change, so it lands with a doc change and a commit message that says what the agent will now do differently.

Both are Jinja templates rendered with `StrictUndefined`: a typo in a variable name fails at render time rather than quietly sending an agent a prompt with a hole in it. Each opens with a comment block stating its contract and its variables.

The exact rendered prompt is saved to `.milhouse/runs/<task>/iter-NNN.prompt` every iteration, so tuning by observation has something to observe.

## `plan.md.j2` — decomposition

Rendered once per task, for a one-shot planning agent.

**Variables**

| Variable     | Meaning                                        |
| ------------ | ---------------------------------------------- |
| `task`       | The `TaskDefinition` — title, body, url        |
| `plan_path`  | Absolute path the agent must write its plan to |
| `max_issues` | Soft ceiling on how many issues to propose     |

**What it promises the agent:** a task definition in full, and a JSON format to write.

**What it demands:**

1. Do **not** run `bd`. Do not create, update, or close anything.
2. Do not implement anything.
3. Write exactly one file — `plan.json` — and stop.

Rule 1 is the whole point. A planning agent with `bd` on its `PATH` will otherwise just create the issues, and there is nothing left for a human to approve. Making the handoff a file is what turns the approval guardrail from a request into a structural fact ([ADR 0006](decisions/0006-planning-agent-proposes-milhouse-creates.md)).

**The decomposition guidance it gives:** one agent-turn of work per issue, each independently verifiable, `blocked_by` only for real ordering constraints, and documentation folded into each issue rather than saved up as a final one. That last point is what makes the [documentation requirement](../README.md) part of the per-issue contract rather than a wish.

It also tells the agent to read the repository — the README, `CLAUDE.md` / `AGENTS.md`, and the code the task touches — before writing the plan. A plan written without looking at the code produces issues that do not fit it.

### The plan format

```json
{
  "issues": [
    {
      "key": "add-command",
      "title": "Add the hello subcommand",
      "type": "task",
      "priority": 1,
      "description": "Add `hello` to cli.py.",
      "acceptance": "`milhouse hello` prints a greeting.",
      "blocked_by": []
    },
    {
      "key": "document",
      "title": "Document the hello subcommand",
      "blocked_by": ["add-command"]
    }
  ]
}
```

| Field         | Required | Meaning                                                           |
| ------------- | -------- | ----------------------------------------------------------------- |
| `key`         | yes      | Plan-local handle, unique in the file. Only used by `blocked_by`. |
| `title`       | yes      | One line, imperative.                                             |
| `type`        | no       | `task` (default), `feature`, `bug`, or `chore`.                   |
| `priority`    | no       | 0 (highest) to 4. Omitted means the tracker's default of 2.       |
| `description` | no       | What to do and why, written for an agent with no context.         |
| `acceptance`  | no       | How that agent knows it is finished.                              |
| `blocked_by`  | no       | Keys of issues in the same plan that must be done first.          |

milhouse validates all of it before anything reaches `bd`:

1. A non-empty `issues` array of objects.
2. Every issue has a non-empty `title` and a unique, non-empty `key`.
3. Every `blocked_by` entry names another issue in the same plan.
4. The `blocked_by` graph is acyclic.

Rule 4 matters more than it looks: a cycle would leave `bd ready` permanently empty, and the loop would exit reporting the epic finished having done nothing.

Any failure is a planning failure. milhouse says which rule broke, keeps `plan.json` for inspection, and exits. The file is plain JSON, so editing it by hand and re-running is a reasonable fix.

## `iterate.md.j2` — one issue

Rendered once per iteration, for a **fresh agent with no memory of any previous iteration**.

**Variables**

| Variable        | Meaning                                      |
| --------------- | -------------------------------------------- |
| `task`          | The `TaskDefinition`, included as background |
| `issue`         | The `Issue` being worked                     |
| `acceptance`    | Acceptance criteria pulled off the bead      |
| `notes`         | Notes previous attempts left on the bead     |
| `branch`        | Branch to commit to, or `None`               |
| `attempt`       | 1-based attempt number for this issue        |
| `attempts_left` | Attempts remaining, including this one       |
| `previous`      | Earlier attempts, as `{outcome, detail}`     |

**What it promises the agent:** exactly one issue, the acceptance criteria, the notes previous attempts left, the task definition as background, and the branch to commit to.

**What it demands** — the five conditions for "done":

1. The change is implemented.
2. It is verified: tests pass, linter clean. Run them, do not assume.
3. The documentation covering the change is updated **in the same commit**.
4. It is committed, with the issue id in the message.
5. `bd close <id>` has been run — and only if 1 through 4 actually happened.

**And the failure path**, which carries as much weight as the success path: commit what works, `bd note` what was learned, and **leave the issue open**.

That last instruction is doing real work. Without it, the incentive is to close the issue and look successful, which is the one failure milhouse cannot detect — `bd` says closed, so [ADR 0004](decisions/0004-outcome-from-beads-and-git.md) says success. The only defences are this instruction and the fact that a human can watch the pane.

### What is deliberately absent

- **`bd prime` output.** `bd init` writes an `AGENTS.md` that already teaches the beads workflow, and every agent kind herdr supports reads `AGENTS.md` or `CLAUDE.md` on startup. Restating it burns context on something the agent has.
- **Repo conventions.** Same reasoning. The prompt says "follow the conventions in `CLAUDE.md` / `AGENTS.md`" and leaves it there.
- **The rest of the issue tree.** The agent does not choose its work ([ADR 0005](decisions/0005-milhouse-owns-the-loop.md)), so showing it what else is pending only invites it to start.

### Retries

On attempt 2 and beyond the prompt says so, says how many attempts remain, lists how the earlier attempts ended, and tells the agent to try a different approach. The notes on the bead are the only memory that survives between attempts, which is why the failure path insists on writing them.

## Changing a prompt

1. Edit the template. Update its header comment if the contract changed.
2. Update this file. A prompt change with no doc change is an incomplete change.
3. Update `tests/test_prompts.py`. Those tests assert on the _contract_ — that the plan prompt still forbids `bd`, that the iterate prompt still forbids closing an unfinished issue — not on the wording, so tuning prose does not break them but dropping a promise does.
4. Watch a real run. That is the actual test ([ADR 0013](decisions/0013-iteration-prompt-contract.md)).
