# Prompts

For a ralph loop the prompt _is_ the product. milhouse ships one, inside the package rather than user-configurable, so a run is reproducible from a milhouse version. Every prompt change is a behaviour change, so it lands with a doc change and a commit message that says what the agent will now do differently.

It is a Jinja template rendered with `StrictUndefined`: a typo in a variable name fails at render time rather than quietly sending an agent a prompt with a hole in it. It opens with a comment block stating its contract and its variables.

The exact rendered prompt is saved to `.milhouse/runs/<issue-id>/iter-NNN.prompt` every iteration, so tuning by observation has something to observe.

There used to be a second one, `plan.md.j2`, which decomposed a task definition into issues. It is gone, along with the planning agent and the task definition itself. Planning is the most opinionated thing milhouse did and its prompt was a guess: it dictated issue granularity, insisted documentation belonged inside each issue, and ruled on how `blocked_by` should be used, none of it arrived at by watching runs ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md)). Getting issues into the tracker is now yours, with a prompt you own.

The discipline that prompt enforced is now yours too, and nothing warns you when it slips. [Usage](usage.md#getting-work-into-the-tracker) has the shape that works.

## `iterate.md.j2` — one issue

Rendered once per iteration, for a **fresh agent with no memory of any previous iteration**.

**Variables**

| Variable     | Meaning                                  |
| ------------ | ---------------------------------------- |
| `issue`      | The `Issue` being worked                 |
| `background` | The parent epic's description, or `""`   |
| `acceptance` | Acceptance criteria pulled off the bead  |
| `notes`      | Notes previous attempts left on the bead |
| `branch`     | Branch to commit to, or `None`           |
| `attempt`    | 1-based attempt number for this issue    |
| `previous`   | Earlier attempts, as `{outcome, detail}` |

**What it promises the agent:** exactly one issue, the acceptance criteria, the notes previous attempts left, the parent epic's description as background, and the branch to commit to.

**What it demands** — the five conditions for "done":

1. The change is implemented.
2. It is verified: tests pass, linter clean. Run them, do not assume.
3. The documentation covering the change is updated **in the same commit**.
4. It is committed, with the issue id in the message.
5. `bd close <id>` has been run — and only if 1 through 4 actually happened.

**And the failure path**, which carries as much weight as the success path: commit what works, `bd note` what was learned, and **leave the issue open**.

That last instruction is doing real work. Without it, the incentive is to close the issue and look successful — `bd` says closed, so [ADR 0004](decisions/0004-outcome-from-beads-and-git.md) says success. milhouse can now check the answer with [`[verify] command`](configuration.md), which is the backstop ([ADR 0016](decisions/0016-milhouse-verifies.md)). The prompt still asks, because it is cheaper for an agent to find its own failure mid-turn than for milhouse to find it afterwards.

### Background comes from the epic

The background block used to be the task definition, read verbatim from a markdown file. It is now the description of the issue's parent, read from the tracker. That is a real trade: the epic description is usually shorter, and it is the only remaining place to say what a set of issues is collectively for. An issue with no parent gets no background block at all, which is a working prompt but a thinner one.

### What is deliberately absent

- **`bd prime` output.** `bd init` writes an `AGENTS.md` that already teaches the beads workflow, and every agent kind herdr supports reads `AGENTS.md` or `CLAUDE.md` on startup. Restating it burns context on something the agent has.
- **Repo conventions.** Same reasoning. The prompt says "follow the conventions in `CLAUDE.md` / `AGENTS.md`" and leaves it there.
- **The rest of the issue tree.** The agent does not choose its work ([ADR 0005](decisions/0005-milhouse-owns-the-loop.md)), so showing it what else is pending only invites it to start.

### Retries

On attempt 2 and beyond the prompt says so, lists how the earlier attempts ended, and tells the agent to try a different approach. It does not say how many attempts remain, because there is no cap: every earlier attempt ended a step, and a person typed the next one ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)).

The notes on the bead are the only memory that survives between attempts, which is why the failure path insists on writing them, and why a rejected verification pastes its output into one.

## Changing a prompt

1. Edit the template. Update its header comment if the contract changed.
2. Update this file. A prompt change with no doc change is an incomplete change.
3. Update `tests/test_prompts.py`. Those tests assert on the _contract_ — that the prompt still forbids closing an unfinished issue, that it still scopes the agent to one issue — not on the wording, so tuning prose does not break them but dropping a promise does.
4. Watch a real run. That is the actual test ([ADR 0013](decisions/0013-iteration-prompt-contract.md)).
