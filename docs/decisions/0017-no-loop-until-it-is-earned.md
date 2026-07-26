# 0017 — No loop until one is earned

**Status:** accepted

## Context

[ADR 0014](0014-step-is-the-primitive.md) made one iteration the primitive and left `milhouse run` as a thin loop over it: `Session` plus `ensure_epic`, then N calls to `step()` instead of one.

Having built it, the honest observation is that nobody had run it. The loop's policy is still the open question, and the answer is supposed to come from watching real iterations rather than from reasoning about them. Shipping a loop before that has two costs, and neither is the code:

- **It is what the docs lead with.** `README` and `usage.md` both opened on `milhouse run`, so the thing a new reader reaches for was the thing least understood.
- **It invites tuning the policy from the armchair.** Every question a loop raises — retry or hand back, how many times, what to do with a blocked agent — is answerable in the abstract and only correctly answerable from a transcript. A loop that exists gets its policy argued about. A loop that does not gets its policy written down after the fact.

The technical cost was small, which is the point: it was a second code path to keep green for no observation in return.

## Decision

Remove `milhouse run`, and `loop.py` with it. `milhouse plan` and `milhouse step` are the whole surface for driving work, and both are meant to be typed by a person.

`--dry-run` moves to `step`, where it still earns its keep as the cheapest way to see what the next iteration would be sent.

Four things went with it rather than being left dangling:

| Removed                 | Because                                                                         |
| ----------------------- | ------------------------------------------------------------------------------- |
| `Decision.stop`         | Only the loop read it. The policy now settles the issue and says why.           |
| `LoopAbortedError`      | Already dead: declared, never raised. Exit 9 comes from `typer.Exit`.           |
| `[loop] max_iterations` | Only the loop read it.                                                          |
| The `[loop]` section    | What was left, `turn_timeout_ms`, bounds one agent turn. It moved to `[agent]`. |

The SIGINT and SIGTERM handling that lived in `loop.run()` moved to `Session.__enter__`, which is where it always belonged. Before, a `SIGTERM` during `milhouse step` skipped `Session.__exit__` entirely and left both the claim and the run lock behind.

Repeating a step is a shell's job in the meantime:

```sh
while milhouse step docs/tasks/hello.md; do :; done
```

That is not a substitute, and it is not meant to be. It stops on the first non-zero exit, which is exactly today's policy, and writing it out makes the missing part obvious: everything a real loop would add is the part nobody has earned yet.

## Consequences

- **The de-scope is now visible in the CLI**, not just in the docs. `milhouse --help` lists what milhouse actually does.
- **Putting it back is small and the shape is already known.** A loop over `step()` with a `decide()` that returns something other than "hand back". [ADR 0014](0014-step-is-the-primitive.md)'s split is what keeps it that way, and it still holds: `Session` takes no position on how many iterations happen inside it, and `step()` already accepts the policy as an argument.
- **The thing to watch for is the loop being missed for the wrong reason.** Wanting it because typing `milhouse step` twelve times is tedious is a reason to write it. Wanting it because a run left something half-done and a retry would probably fix it is a reason to read the transcript first, because that is the observation the policy needs.
- **`RalphLoop` is gone as a name.** The ralph methodology's fresh context window per iteration is still what `step` does, and is not what was removed. What was removed is the assumption that the iterations should be strung together automatically before anyone has watched them go wrong.
- The git history has the deleted loop, and it was green when it was deleted. Reverting the commit is a legitimate starting point for the real one.
