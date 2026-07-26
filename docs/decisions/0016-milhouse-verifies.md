# 0016 — milhouse verifies a closed issue rather than trusting it

**Status:** accepted

## Context

[ADR 0004](0004-outcome-from-beads-and-git.md) makes "the issue is closed in beads" the success signal, and the agent is the one that closes it. [ADR 0013](0013-iteration-prompt-contract.md) already named the hole this leaves:

> Without it, the incentive is to close the issue and look successful, which is the one failure milhouse cannot detect — `bd` says closed, so [ADR 0004](0004-outcome-from-beads-and-git.md) says success.

The mitigation was a paragraph in the prompt asking the agent not to do that. That is a request, and the whole point of [ADR 0006](0006-planning-agent-proposes-milhouse-creates.md) is that a guardrail an agent can decline is not a guardrail. A run was a self-graded exam.

It is also the failure that compounds. A stalled iteration costs one turn. A falsely closed issue is never offered again, so the epic reports finished with the work undone, and whatever depended on it is built on top.

## Decision

`[verify] command` names the repository's own gate — its tests, its linter, whatever a person would run before believing the work. When an iteration ends with the issue closed, milhouse runs it.

| Result                          | Outcome    | Then                                                   |
| ------------------------------- | ---------- | ------------------------------------------------------ |
| Exit 0                          | `success`  | Nothing. The close stands.                             |
| Non-zero, timed out, or missing | `rejected` | The issue is re-opened with the output as a `bd` note. |

Three details, each of which is the decision as much as the table is:

- **It runs only when the agent claims to be finished.** Running the whole test suite after a stalled turn buys a slow way to learn that an unfinished issue is unfinished.
- **The output goes on the issue, not just in the log.** Notes are what a fresh context window gets instead of memory ([ADR 0013](0013-iteration-prompt-contract.md)), so pasting the failure into one is how the next agent learns why the last one's work was turned down. It is capped at a tail of 2000 characters, because a bead is not a place for a megabyte of pytest.
- **`classify()` stays pure.** It receives the verification as a value. Running the command is `verify.py`'s job, and deciding what to do about it is `policy.py`'s.

Empty by default. Out of the box milhouse still takes the agent at its word, because a wrong verification command fails every iteration and there is no safe guess about what a given repository's gate is.

## Consequences

- **This is what makes milhouse a supervisor rather than a driver.** It is the first check milhouse performs that the agent cannot talk its way past, and it does not depend on the loop being unattended: `milhouse step` gets it too.
- **A slow gate is paid per closed issue.** A ten-issue epic runs the suite ten times. `timeout_ms` bounds each one. If that hurts, name a faster subset — the fast suite, not the full matrix.
- **A flaky gate re-opens good work.** The failure output goes on the issue, so it is visible rather than mysterious, but a repository whose tests fail one time in ten will produce one wrongly rejected issue in ten. That is an argument for fixing the tests, and it is the same argument CI already makes.
- **`rejected` is distinct from `stalled` on purpose.** They are the same instruction to today's policy, but not the same diagnosis. A run full of `rejected` means the agents believe they are finishing work that does not pass, which is a prompt problem. A run full of `stalled` means they are not finishing at all.
- **The verification is milhouse's, not the agent's.** The iteration prompt still tells the agent to run the tests itself ([ADR 0013](0013-iteration-prompt-contract.md)), and that stays: it is cheaper for the agent to find its own failure mid-turn than for milhouse to find it afterwards. This is the backstop, not the primary check.
