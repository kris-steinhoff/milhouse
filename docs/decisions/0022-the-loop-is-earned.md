# 0022 — The loop is earned, as `milhouse run <target>`

**Status:** accepted

## Context

[ADR 0017](0017-no-loop-until-it-is-earned.md) removed `milhouse run` and said why in a sentence worth repeating: a loop that exists gets its policy argued about, and a loop that does not gets its policy written down after the fact. It did not claim a loop was wrong. It set a condition, and named the failure mode to watch for while waiting:

> Wanting it because typing `milhouse step` twelve times is tedious is a reason to write it. Wanting it because a run left something half-done and a retry would probably fix it is a reason to read the transcript first, because that is the observation the policy needs.

Since then the primitive has been driven by hand often enough to answer the questions 0017 said only a transcript could answer:

- **A retry after a `stalled` turn usually stalls again.** The agent that could not start is not helped by being asked a second time with the same issue text. What changes an outcome is a `bd note` from the failed attempt, which is why the prompt carries them. Three attempts is where the returns stop.
- **A `blocked` agent stays blocked.** Every permission prompt seen in practice was a class of prompt rather than a one-off, so the next issue hits it too. Skipping to the next issue converts one stop into N stops and burns the budget doing it.
- **`rejected` is rare and informative.** It has always meant the issue was thinner than the work, not that the gate was flaky, so it belongs on the retry ladder rather than on the abort path.

That is enough to write the policy from observation rather than from the armchair, which is the whole condition 0017 imposed.

The other thing that changed is what a target can be. When 0017 removed the loop, `milhouse run` took a task definition. [ADR 0018](0018-no-task-milhouse-works-the-ready-queue.md) removed that for reasons that still hold, and it is what makes this ADR something other than a revert.

## Decision

**`milhouse run <target>`, where the target is a beads id.**

```sh
milhouse run <target> [--max-iterations 50] [--max-attempts 3]
                      [--agent K] [--workspace ID] [--attach] [--dry-run] [--repo P]
```

- **An epic target** is done when nothing in its scope is unfinished. `bd ready --parent` and `bd list --parent` already scope both questions, so this is mostly configuration.
- **A leaf issue target** is done when it closes. Its unmet blockers are worked first, because `bd ready` will not offer a blocked issue and the target cannot close without them.

Nothing else about a turn changes. The agent gets one issue and a fresh context window, and "done" keeps the meaning [ADR 0016](0016-milhouse-verifies.md) gave it: the agent runs `bd close`, and `[verify] command` is the gate that can overturn it. Acceptance criteria are already rendered into the iteration prompt and stay the agent's to satisfy.

**Serial.** One issue at a time. Ralph's defining property is the fresh context window per iteration, not parallelism, and running turns concurrently would put two unresolved questions on the critical path: dependency-graph joins, which milhouse currently refuses outright, and landing N lanes, which nothing does. `run.py` takes its loop body as an argument so a later `--count N` swaps `step` for dispatch-then-reap without the loop learning anything new.

**The halt table**, which is a pure function over the finished iteration and the counters:

| Condition                                  | The run                                    |
| ------------------------------------------ | ------------------------------------------ |
| Nothing ready, nothing unfinished in scope | stops, finished                            |
| Nothing ready, unfinished issues remain    | stops, deadlocked, and names them          |
| Outcome `blocked`                          | stops. Nobody is there to approve          |
| Outcome `error`                            | stops. milhouse failed, not the agent      |
| A closed issue left the tree dirty         | stops. The next iteration would inherit it |
| Iterations used reaches `max_iterations`   | stops                                      |
| Anything else                              | continues                                  |

**Attempts are capped, and the cap defers.** A failing outcome on attempt `max_attempts` runs `bd defer <id> --reason=...` and the run moves to the next ready issue. The final report names every issue it gave up on.

**Why defer rather than block.** [ADR 0014](0014-step-is-the-primitive.md) removed `block` from `IssueAction` on the grounds that giving up is a person's decision. Deferring does not take that decision, it hands it over: `bd defer` hides the issue from `bd ready` while leaving it in `bd list`, so it stays unfinished, the report names it, and `bd undefer` is how a person picks it back up. `bd update --status blocked` was rejected because blocked in bd means the issue has an unmet dependency, and using it here would put a false statement in the tracker.

Two things 0017 deleted come back in a different shape, which is worth being precise about because neither is a revert:

| 0017 removed     | Comes back as                                                                                                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `Decision.stop`  | Nothing on `Decision`. Halting is a separate pure function, so the issue's fate and the run's fate stay independent                   |
| `[loop]` section | `[run] max_iterations` and `[run] max_attempts`. The old name is not reused, so an old config file cannot silently mean something new |

## Rationale

The condition 0017 imposed was not "wait a while", it was "watch iterations fail and write down what you saw". The three observations above are that, and each of them is a row of the table. The retry ladder is three deep because attempts four and five were never observed to work. The blocked agent aborts because skipping it was observed to multiply the stop rather than route around it.

Keeping the target a beads id is what stops this being a revert of 0017's companion decision. The task definition was removed because planning is the most opinionated thing milhouse could do and because a markdown file describing work that also exists as issues is two records that drift. A beads id introduces neither. Issues still arrive in the tracker by whatever process the human wants, and `milhouse run` still creates nothing.

## Alternatives considered

**Concurrent from the start**, a supervisor over `dispatch` and `reap` with N turns in flight. Rejected for now. Dependency-graph joins are currently refused outright and nothing merges N green branches, so both open questions would become load-bearing on the first unattended run, at the hour when nobody is watching. The loop body is one call, so this stays additive.

**A reviewer agent judging acceptance criteria independently.** Rejected as unearned, which is the same argument this ADR is built on. If `success` turns out to be wrong often enough to notice, that observation earns its own ADR, and [ADR 0016](0016-milhouse-verifies.md) is where it attaches.

**Stop the whole run on the first exhausted issue.** Closest to today's supervised posture and the safest option. Rejected because one impossible issue then stops a run that could have finished the other eight, and the deferral report makes the give-up visible without stopping.

**No per-issue cap, only the global ceiling.** Truest to unattended ralph, and the most expensive way to discover that an issue cannot be done.

## Consequences

- **`--max-iterations` is now the only thing bounding an overnight run.** [ADR 0012](0012-no-cost-controls-in-v1.md) said no cost controls and that is unchanged, but the exposure is much larger than it was when one step was one turn and a person decided whether there was another. The ceiling is a turn count and turns are not the same size.
- **Deferred issues stay unfinished.** A run that gave up on two of nine reports finishing seven and deferring two, rather than reporting success. This is the same failure 0017's `nothing_ready` fix was about, and it is worth not regressing.
- **A run full of deferrals is a prompt or decomposition problem**, not a reason to raise the cap. Raising it buys three more attempts at whatever the fourth attempt was already going to do.
- **The unattended posture is unchanged and still opt-in.** [ADR 0009](0009-permission-posture.md) is no longer merely deferred, because `run` is the first thing that makes unattended meaningful, but nothing here grants permissions. `[agent] args` is still the only escape hatch, and the agent's consent screen still has to be accepted by hand once.
- **`milhouse step` is untouched.** It keeps the supervised policy, and it stays the way to watch one turn before turning a run loose on the same repository.
- **The Repetition layer stops being empty.** `docs/architecture.md` argued that having the layer named and unoccupied is what would make filling it cheap. That claim is now testable, and the same test applies to `--count N`.

## What the first runs taught

Four runs against a scratch repository, watched. The table above survived, and three things about it are worth writing down before they are forgotten.

**The commonest failure was not stalling, it was not closing.** Three of the first five turns ended `partial` with a commit that named the issue and did the work. What was missing was `bd close`. The retry ladder handled it — a later attempt read the commit and closed the issue in one turn — but a run pays a full turn for a missing command. That is a prompt problem rather than a policy problem, so nothing here changes, and it is the first thing to look at in `iterate.md.j2`.

**A turn can burn an attempt having done nothing at all.** One iteration timed out with the agent still at 0% context: it was started, it was prompted, and it never processed the prompt. `timeout` counts as an attempt, so an issue can be deferred having had two real tries and one that never happened. The counter-argument is that milhouse cannot tell "the agent thought for thirty minutes and got nowhere" from "the agent never woke up", and inventing a distinction it cannot observe is worse than charging an attempt. Left as it is, deliberately, and recorded because the deferral report will occasionally be unfair for this reason.

**Deferral is not the end of an issue, and reads as if it is.** The issue deferred in these runs was implemented, committed, and one `bd close` from done. The report said "did not finish in 3 attempt(s)", which is true and sounds much worse than the state of the work. The note on the issue carries the last outcome, so the information is there. The wording is a small thing to improve rather than a decision to revisit.

Nothing observed contradicted the halt table. The blocked-agent row was not exercised, because the posture used never produced a blocked agent, so it remains the row with the weakest evidence behind it.

## Revisit when

The blocked-agent row is exercised by a real run rather than reasoned about, or a run is watched with `[verify] command` set — every run so far took the agent at its word, so `rejected` has still never been seen in a loop.
