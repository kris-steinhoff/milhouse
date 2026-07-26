# 0014 — The step is the primitive; the loop is a policy over it

**Status:** accepted, and taken further by [ADR 0017](0017-no-loop-until-it-is-earned.md)

The split below stands and is what everything is built on. `milhouse run`, the loop half of it, has since been removed: the split is what makes putting one back cheap, so there is no reason to ship one before it has been earned.

## Context

milhouse was built loop-first. `RalphLoop` owned the run lock's worth of state, the branch, the workspace and pane, the planner, the claim/work/classify cycle, the retry ladder, the blocked-agent policy, the iteration ceiling, teardown, and signal handling. Everything that was not argv lived in one class.

Two things said the seam was in the wrong place.

The first was in the code: `milhouse plan` had to call `loop._prepare_branch`, `loop._open_workspace`, and `loop._ensure_epic` to do a supervised operation that is not a loop. When a command breaks encapsulation to avoid the loop, the loop is not the right centre.

The second was in what the loop promised. `--max-attempts`, `--on-blocked {wait,skip,abort}`, `blocked_timeout_ms`, and the per-issue attempt ladder are answers to questions that only arise once a run is unattended: how many times to retry without a human, how long to wait for one, when to give up on an issue and move to the next. Those were guesses. Nobody had run milhouse unattended, and the ralph methodology says the loop's shape is found by observation.

Meanwhile the part that had been observed to work — claim one issue, hand it to a fresh agent, see what changed — had no way to be invoked on its own.

## Decision

**One iteration is the unit. Everything else is built from it.**

The old `RalphLoop` splits four ways, and the split is the decision:

| Module       | Owns                                                           | Pure? |
| ------------ | -------------------------------------------------------------- | ----- |
| `session.py` | The lock, branch, workspace, pane, runner, epic, and the claim | no    |
| `step.py`    | One iteration: claim, prompt, turn, classify, record, settle   | no    |
| `outcome.py` | What the iteration achieved                                    | yes   |
| `policy.py`  | What happens next, and whether the run stops                   | yes   |
| `loop.py`    | Repeating a step until something says stop                     | no    |

`milhouse step` calls `step()` once and hands back to a person. `milhouse run` called it in a loop, and nothing else differed between them, which is why removing it later cost one file ([ADR 0017](0017-no-loop-until-it-is-earned.md)).

### One policy, and it is supervised

`policy.decide()` implements a single rule: **stop at the first iteration that does not succeed, and say what needs a person.** Any unfinished issue is re-opened so the next claim can see it, which is not optional housekeeping — a claimed issue is `in_progress`, and `bd ready` excludes those.

That deletes, rather than reimplements:

- `--on-blocked` and its three modes, and `blocked_timeout_ms`. A blocked agent stops the run and names the workspace to attach to.
- `--max-attempts`, `RunState.attempts`, and the retry-then-block ladder. A person decides whether to try again.
- `Tracker.block`. Marking an issue blocked was how the attempt cap gave up on one, and giving up is now a person's decision.
- `AgentRunner.wait_for_unblock` and `HerdrClient.wait_for_status`, which existed only to serve `--on-blocked wait`.

### `--max-iterations` counts this invocation

It was compared against `len(state.iterations)`, which made it a lifetime cap per task: `milhouse run --max-iterations 2` against a task with six recorded iterations did nothing and exited 9. It now bounds the invocation. Iteration _numbers_ still count across invocations, because they name `iter-NNN.prompt`.

### The history is an event log

`state.json` carried an unbounded `iterations` list and was rewritten whole on every save. The history moved to an append-only `events.jsonl`, and `state.json` keeps only the session facts needed to pick a task back up. `RunState` is version 2; a version 1 file still loads, minus its history.

## Consequences

- **The supervised workflow is a first-class command**, not a loop with the ceiling set to 1.
- **The interesting question is now a function.** "How should the ralph loop behave" is answered by writing a second `decide()` and testing it as a table, not by rewriting the loop. `step()` already takes the policy as an argument.
- **The docs no longer promise unattended behaviour that has never been observed.** [ADR 0005](0005-milhouse-owns-the-loop.md)'s guardrails, [ADR 0009](0009-permission-posture.md)'s unattended posture, and [ADR 0012](0012-no-cost-controls-in-v1.md)'s cost discussion are marked deferred rather than deleted: they are the shape of the ralph policy when it lands.
- **A run that hits any trouble stops.** On a good day that is the point. On a bad one it means a long batch ends early on something a retry would have fixed, and the human re-runs. That is the cost of not guessing, and it is paid back the first time the observed failures tell us what the retry rule should be.
- **`milhouse run` still exists and still terminates on its own** when the work goes well, so nothing about the ralph vision is foreclosed. The loop got thinner, not weaker.
- The class is still called `RalphLoop`. The name is aspirational, and renaming it when the ralph policy lands would be a rename in the wrong direction.
