# Usage

Every command and flag, with worked examples. Output shown here was captured from a real run.

## Global options

```
milhouse [--version] [--verbose] <command> [options]
```

| Option                 | Meaning                                                            |
| ---------------------- | ------------------------------------------------------------------ |
| `--version`            | Print the milhouse version and exit.                               |
| `--verbose`, `-v`      | Log every subprocess milhouse runs, to stderr. The debugging tool. |
| `--install-completion` | Install shell completion for milhouse, then exit.                  |
| `--show-completion`    | Print the completion script instead of installing it.              |

## Shell completion

Install it once, then restart the shell:

```console
$ milhouse --install-completion
zsh completion installed in /home/you/.zfunc/_milhouse
Completion will take effect once you restart the terminal
```

The shell is detected from the process tree, and bash, zsh, fish, and PowerShell are supported. `--show-completion` prints the same script instead of writing anything, which is what you want when your shell config is generated or version-controlled elsewhere.

What completes:

| Parameter | Offers                                                             |
| --------- | ------------------------------------------------------------------ |
| `--repo`  | Directories.                                                       |
| `--agent` | The common herdr agent kinds. Any kind herdr supports still works. |

Nothing here contacts the herdr server or `bd`, so completion stays instant and works with the server down. That is why `--workspace` is not on the list: milhouse writes no workspace id down any more, and the only thing that knows one is herdr itself.

## Getting work into the tracker

milhouse does not do this, and it has no planning agent. It claims whatever `bd ready` offers and works it ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md)).

That means issue quality is entirely yours to keep up. `iterate.md.j2` hands one issue to an agent with no context and nothing else ([ADR 0013](decisions/0013-iteration-prompt-contract.md)), so an issue with a thin description simply produces a worse iteration, and nothing warns you. Useful shape:

- **One agent-turn of work per issue**, independently verifiable.
- **A description written for a stranger**, plus `--acceptance` saying how they know they are done.
- **`bd dep add` for real ordering constraints only.** A dependency that is not real just serialises work that could have run in parallel, and it is what decides how much [`--count N`](#--count-n-several-issues-at-once) a target can use.
- **An epic over the set.** Its description becomes the background every child's prompt carries, which is the only place the wider context lives now.

### Fencing the queue

By default milhouse considers every ready issue in the repository. Where the beads database also carries work that was never meant for an agent, fence it with a label or a parent, either on the command line or once in [`.milhouse/config.toml`](configuration.md#tracker):

```toml
[tracker]
parent = "milhouse-6or"   # only issues under this epic
label = "agent"           # only issues carrying this label
```

Epics are never offered whatever the fence says: an epic is a container for work, not a unit of it.

## `milhouse doctor`

Verify the tools milhouse depends on and the state of the herdr server. Run this first, and again whenever a run fails in a way that does not make sense.

```
milhouse doctor [--repo PATH]
```

| Option   | Default            | Meaning                                      |
| -------- | ------------------ | -------------------------------------------- |
| `--repo` | the enclosing repo | Repository to check, if not the current one. |

```console
$ milhouse doctor
ok   bd            bd version 1.1.0 (8e4e59d39: HEAD@8e4e59d39f34)
ok   beads db      /home/agent/code/github.com/kris-steinhoff/milhouse/.beads
ok   herdr         herdr 0.7.5
ok   herdr server  running, protocol 17
ok   git           git version 2.47.3
ok   claude        2.1.220 (Claude Code)
ok   config        using defaults (no .milhouse/config.toml)
```

Three result levels:

- `ok` — the check passed.
- `warn` — an optional tool is missing. The agent binary is only needed for real runs, so a `warn` there is fine until you need it.
- `FAIL` — a required tool or service is missing. `doctor` exits `7`.

The agent row uses whatever `[agent] kind` is configured, so it checks the agent you will actually run.

## `milhouse step`

One iteration, then back to you. It claims the next ready issue, hands it to a **freshly started** agent, classifies what happened, and stops.

This is the primitive, and the thing to use while you are still learning what your prompts and your issue descriptions do. [`milhouse run`](#milhouse-run) repeats it for you once you trust it.

```
milhouse step [options]
```

| Option        | Default              | Meaning                                                 |
| ------------- | -------------------- | ------------------------------------------------------- |
| `--agent`     | `claude`             | Agent kind to run. Any kind herdr supports.             |
| `--workspace` | `HERDR_WORKSPACE_ID` | Reuse this herdr workspace instead of creating one.     |
| `--parent`    | unfenced             | Only work issues under this epic.                       |
| `--label`     | unfenced             | Only work issues carrying this label.                   |
| `--dry-run`   | off                  | Render the prompt and print the plan; start no agent.   |
| `--attach`    | off                  | Focus the herdr workspace instead of leaving it hidden. |
| `--repo`      | the enclosing repo   | Repository to work in.                                  |

Everything except `--dry-run` and `--attach` can also be set in [`.milhouse/config.toml`](configuration.md).

Run from inside a herdr pane, `--workspace` defaults to the workspace that pane is in. Otherwise milhouse looks for an open workspace labelled `milhouse:<repo>` — the one an earlier step left behind — and creates one if there is none. Either way, that workspace is only the **source**: no agent runs in it, so the pane you typed into is never taken.

### Lanes

The agent works in a **lane**: a herdr worktree of its own, on a branch of its own, labelled with the issue id ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)). Your own checkout is left alone — milhouse never checks anything out in it.

Which lane an issue gets follows the dependency graph:

| The issue                               | Gets                                                         |
| --------------------------------------- | ------------------------------------------------------------ |
| already has a lane                      | that lane, so a retry lands on the branch the last one used  |
| has one blocker in a live lane          | a new tab in that lane, continuing on the same branch        |
| has none, or blockers with no live lane | a new worktree, branched from your current branch            |
| has blockers in **two** lanes           | nothing — milhouse refuses and names both. Land one of them. |

herdr checks lanes out under `~/.herdr/worktrees/<repo>/<branch>`, outside the repository, so they cannot show up as untracked files in another lane. `milhouse status` lists them, and `herdr worktree list` is where they actually live — milhouse keeps no record of its own.

A `dispatch` lane stays a branch. milhouse reports it, you merge it. [`milhouse run`](#milhouse-run) is the path that does land its own lanes: the whole target has one integration branch, and above `--count 1` each issue's worker branch is merged back into it as its turn settles ([ADR 0023](decisions/0023-a-run-has-one-lane.md), [ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)). Nothing merges a `dispatch` lane, and whether a run's answer generalises to lanes another process opened is [still open](decisions/README.md#still-open).

Two things to know before pointing this at a real repository:

- **A fresh worktree has no `.venv` and no `node_modules`.** `[verify] command` runs in the lane, so a gate that assumes a built environment fails for environmental reasons rather than real ones. Leave it unset, or point it at something that bootstraps itself (`uv run …` does).
- **Two green lanes can be red together.** Serial work on one branch could not produce that. `milhouse run --count N` runs the gate again on the integration branch after a merge that joined two histories, which is the only place that combination is ever looked at. Nothing checks it for `dispatch`.

Exits `0` when the issue was finished and `9` when it was not.

A shell loop over it is not the same thing as [`milhouse run`](#milhouse-run):

```sh
while milhouse step; do :; done   # not a substitute
```

That stops at the first iteration that does not succeed, gives each issue a lane and branch of its own, merges none of them, and retries an issue forever if it keeps almost working. `run` caps the attempts, lands everything on one branch, and knows the difference between a queue that is finished and one that is stuck.

### Resuming

Stepping again **is** the resume mechanism. Any claim a previous step left behind is re-opened first ([ADR 0008](decisions/0008-crash-recovery-by-reconciliation.md)). There is no separate `resume` command.

Iteration numbers keep counting across invocations, because they name `<issue-id>/iter-NNN.prompt`, and the history in the beads audit log spans all of them.

### One turn per lane

A turn holds a lock on its own lane. A second `milhouse step` in the same repository is fine — it claims a different issue and works a different lane. What the lock stops is two processes driving the same lane, which would mean two agents in one pane and each re-opening the other's claim ([ADR 0015](decisions/0015-one-run-at-a-time.md)):

```console
$ milhouse step
milhouse: another milhouse run is working bd-e.2 (pid 48213 on carbon, since 2026-07-26T09:14:02+00:00)
  Wait for the other run, or delete .milhouse/runs/<issue-id>/lock.json.
```

A lock left behind by a dead process is taken over automatically, with a line saying so.

### What each iteration does

1. `bd ready --claim --limit 1 --exclude-type epic`, plus the configured fence — an empty result means nothing is ready.
2. `bd show` it, for its blockers and its parent's description.
3. Find or create its lane, per the table above.
4. Render `iterate.md.j2` for that issue and save it to `<issue-id>/iter-NNN.prompt`.
5. `herdr agent start` a **new** agent in the lane's pane.
6. `herdr agent prompt --wait` until the turn settles.
7. Capture the pane transcript to `<issue-id>/iter-NNN.term`.
8. Exit the agent, returning the pane to a shell prompt.
9. If the issue is closed and `[verify] command` is set, run it **in the lane**.
10. Classify the outcome from beads, git in the lane, and that command, and record it.

| Outcome    | Means                                         | Issue becomes | Exits |
| ---------- | --------------------------------------------- | ------------- | ----- |
| `success`  | The issue is closed, and verification passed. | closed        | `0`   |
| `rejected` | The issue is closed, but verification failed. | re-opened     | `9`   |
| `partial`  | Still open, but commits landed.               | re-opened     | `9`   |
| `stalled`  | Still open and nothing was committed.         | re-opened     | `9`   |
| `timeout`  | The turn did not settle in time.              | re-opened     | `9`   |
| `blocked`  | The agent is waiting on a human.              | re-opened     | `9`   |
| `error`    | herdr or `bd` failed.                         | re-opened     | `9`   |

Re-opening matters: a claimed issue is `in_progress`, and `bd ready` excludes those, so an unfinished issue that was simply left alone would never be offered again and the work would look finished with the work undone.

Git is read in the directory the turn ran in, not at the repository root, so a commit made anywhere else is not attributed to this issue ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)).

`partial` distinguishes a commit that names the issue from one that does not, because `HEAD` moving on its own could be anyone — a hook, or you in another terminal. The shas either way are in the audit entry ([ADR 0004](decisions/0004-outcome-from-beads-and-git.md)).

A turn that leaves the working tree dirty is reported too, whatever its outcome, because the next agent would inherit changes it did not make and cannot explain.

`rejected` is the one milhouse would otherwise miss. `bd close` is run by the agent, so "the issue is closed" is the agent grading its own exam. Point [`[verify] command`](configuration.md) at the repository's own gate and milhouse checks the answer, re-opening the issue with the failing output as a `bd` note ([ADR 0016](decisions/0016-milhouse-verifies.md)). It is empty by default.

What happens after an iteration is one pure function, `policy.decide()`. Changing how milhouse behaves between iterations means writing a second one, and that is where a loop's policy will go when there is one to write ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)).

### What a step prints

```console
$ milhouse step
created herdr workspace wG (milhouse:greet)
iteration 1: dogfood-6i2.1 Add goodbye(name) to src/greet/__init__.py and document it in README.md
  lane wL4 on milhouse/dogfood-6i2.1 (/home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-1)
  → success: dogfood-6i2.1 closed in beads
lane wL4 is left open (/home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-1)

dogfood-6i2.1: success — dogfood-6i2.1 closed in beads
```

Exit `0`. Step again for the next issue.

The last line is where the work happened. A lane is deliberately left open, whether or not the turn succeeded, so its panes and its checkout can be inspected ([ADR 0005](decisions/0005-milhouse-owns-the-loop.md)). The source workspace above it is not named again: no agent runs in it, and it is the checkout you ran milhouse from.

An iteration that does not finish its issue re-opens it and says what happened:

```console
$ milhouse step
iteration 2: dogfood-6i2.2 Make the src-layout greet package importable when running python -m pytest
  lane wL5 on milhouse/dogfood-6i2.2 (/home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-2)
  → stalled: dogfood-6i2.2 is still open and nothing was committed
  dogfood-6i2.2 did not finish (stalled: dogfood-6i2.2 is still open and nothing was committed)
lane wL5 is left open (/home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-2)

dogfood-6i2.2: stalled — dogfood-6i2.2 is still open and nothing was committed
```

Exit `9`. Read `iter-002.term`, fix whatever it shows, and step again. The issue is back in the ready queue, and the next agent is told this is attempt 2 and how attempt 1 ended.

### Nothing ready is two opposite things

A step also does nothing when `bd ready` offers nothing, which means either that everything in scope is closed or that everything left is stuck behind something. milhouse tells them apart by listing what is unfinished:

```console
$ milhouse step

nothing is ready but 3 issue(s) are unfinished (dogfood-6i2.1, dogfood-6i2.2, dogfood-6i2.3); `bd blocked` says what is stuck
```

That exits `9`. Only "no issues are ready; everything in scope is closed" exits `0`. A step that did no work has to be distinguishable from one that finished the work, or the `while` loop above never terminates.

Repo-wide this question is weaker than it was under an epic: "unfinished" now means every open issue in scope, so a fence usually makes the answer worth more. `bd blocked` is the tool for the follow-up.

Notice what is missing from that output: there is no `lane … is left open` line, because a step that claimed nothing opened no lane and so has nothing to point at.

### The queue grows while you step through it

An agent that spots work outside its issue is told to file it rather than do it ([ADR 0013](decisions/0013-iteration-prompt-contract.md)), so the ready queue can gain issues while you are working through it. In a dogfood run an agent working the third issue filed a fourth, and the next step picked it up.

This is intended, and it is why [`milhouse run`](#milhouse-run) has a ceiling rather than trusting an empty queue: agents can add issues as fast as they are closed, so "work until the queue is empty" is not guaranteed to terminate. Stepping by hand needs no configuration for that. A run needs `--max-iterations`, and whether re-planning wants a cap of its own is [still open](decisions/README.md#still-open).

### `--dry-run`

Shows exactly what the next step would do, including the prompt it would send, and starts nothing:

```console
$ milhouse step --dry-run
dry run — no agent will be started
scope     every ready issue in the repository
branch    main
agent     claude
verify    (none — a closed issue is taken on trust)
run dir   /home/agent/code/github.com/kris-steinhoff/milhouse/.milhouse/runs
lane      milhouse/dogfood-6i2.2  (a new lane)

the next step would work dogfood-6i2.2 and send:

    You are working **one issue** for the milhouse orchestrator. milhouse picked it,
    milhouse decides what happens next, and this session ends when the issue does.
    …
```

It is the cheapest way to see the effect of a prompt, fence, or config change.

## `milhouse run`

Repeats a step until every target is finished, one turn at a time by default and up to `--count N` at once. A target is a beads id, so nothing here is a task definition ([ADR 0022](decisions/0022-the-loop-is-earned.md)).

```
milhouse run TARGET... [--max-iterations N] [--max-attempts N] [--count N] [--agent KIND] [--workspace ID] [--dry-run] [--attach] [--repo PATH]
```

| Option             | Default               | Meaning                                                                                   |
| ------------------ | --------------------- | ----------------------------------------------------------------------------------------- |
| `TARGET...`        | required, one or more | Beads id(s) to work towards: epics, issues, or a mix.                                     |
| `--max-iterations` | `50`                  | Turns this run may take before it stops and reports.                                      |
| `--max-attempts`   | `3`                   | Attempts one issue gets before it is deferred.                                            |
| `--count`, `-n`    | `1`                   | Turns to keep in flight at once, overriding [`[run] max_parallel`](configuration.md#run). |
| `--agent`          | `claude`              | Agent kind to run. Any kind herdr supports.                                               |
| `--workspace`      | `HERDR_WORKSPACE_ID`  | Reuse this herdr workspace instead of creating one.                                       |
| `--dry-run`        | off                   | Show the scope, the caps, the waves, and the first prompt; start no agent.                |
| `--attach`         | off                   | Focus the lane instead of leaving it hidden.                                              |
| `--repo`           | the enclosing repo    | Repository to work in.                                                                    |

There is no `--parent` or `--label`: the targets are the scope.

`--count` and `[run] max_parallel` are the same setting under two names, and they are the one such pair in milhouse ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)). [Configuration](configuration.md#run) carries the mapping.

### What the target means

| Target           | In scope                                          | Finished when                  |
| ---------------- | ------------------------------------------------- | ------------------------------ |
| an **epic**      | everything under it, which is `bd ready --parent` | nothing under it is unfinished |
| a **leaf issue** | it, plus everything it is transitively blocked by | it closes                      |

A leaf target pulls in its blockers because `bd ready` will not offer a blocked issue, so the target cannot close until they do. `milhouse run <issue> --dry-run` prints what it worked out.

### Several targets

`milhouse run` takes more than one target, and works their union as one scope rather than as two runs ([ADR 0025](decisions/0025-a-multi-target-run-shares-one-lane.md)). This is the one case a single target's fence cannot express: an issue under one target that is blocked by an issue under another. Given both targets in one run, that issue is offered by `bd ready` the moment its blocker closes, in the same run. Given them to two separate runs, neither one can finish — the run whose fence excludes the blocker stops with work left, and the operator is left interleaving the two runs by hand.

One target still works exactly as it always has, lane name included. Above that, each target's own scope — an epic's descendants, or a leaf's blockers — is computed the same way a single target's is and unioned, in the order given, with duplicates dropped. `milhouse run <a> <b> --dry-run` prints the union and every target in it.

### One integration branch, and worker branches under it

Whatever the count, a run has an **integration lane**, and that is the branch you review ([ADR 0023](decisions/0023-a-run-has-one-lane.md)). At one target its branch is `milhouse/<target-id>`. Above one target there is no single id to name it after, so the branch is every target's id, sorted and joined with `+`: `milhouse/<a>+<b>` ([ADR 0025](decisions/0025-a-multi-target-run-shares-one-lane.md)). Sorting means the order you type the targets in does not change which lane you land on. Every iteration gets a **fresh agent**, which is what makes this ralph: the fresh context window comes from restarting the agent rather than from the worktree, so reusing the checkout costs nothing.

At `--count 1` the turns happen in that lane, one after another, and nothing is merged because there is nothing to merge. Above it, each issue in flight gets a **worker lane** of its own, on `<integration-branch>--<issue-id>`, branched from the integration branch as it stands at that moment and merged back into it when its turn succeeds ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)).

The separator is `--` and not `/`, because git cannot hold `milhouse/bd-e` and `milhouse/bd-e/bd-e.1` at the same time: refs are a directory hierarchy, and the second is refused with `cannot lock ref`. The first watched concurrent run died on exactly that, before it started an agent.

Either way you get one branch to review as a piece, and either way a run never hits the two-blockers-two-lanes refusal that `dispatch` can: a worker lane's base is the integration branch by construction, so there is never a second candidate.

Re-running the same target(s) finds those lanes again and carries on where it left off, on the same branches. Resuming is just running it again, targets in whatever order — sorting is what makes that true above one target too.

### When it stops

| Condition                                  | Exit | Report                                                                                                           |
| ------------------------------------------ | ---- | ---------------------------------------------------------------------------------------------------------------- |
| Nothing ready, nothing in scope unfinished | `0`  | finished                                                                                                         |
| Nothing ready, work still unfinished       | `9`  | deadlocked, and names what is left                                                                               |
| An agent stopped waiting on a human        | `9`  | nobody is there to approve, and the next turn would too                                                          |
| milhouse itself failed (`bd`, herdr)       | `9`  | its own failure rather than the agent's, including a turn whose agent would not start or would not take a prompt |
| A closed issue left uncommitted changes    | `9`  | serially the next turn in this lane inherits them; in a worker lane nobody does, but they are not merged either  |
| A worker branch did not land               | `9`  | `conflict`, naming both branches — only a person can land it                                                     |
| The gate failed on the integration branch  | `9`  | `integration`, the merge stands and the output is on the issue                                                   |
| `--max-iterations` reached                 | `9`  | the ceiling                                                                                                      |

The two merge rows only fire above `--count 1`, where there is something to merge. A halt **stops starting work rather than abandoning work already started**: the run reports `draining N turn(s) already in flight` and waits for them, because dropping them would leave claimed issues with live agents and branches nobody merges. Whatever settles during the drain is in the report, and none of it changes why the run stopped.

Whether those turns are also **merged** depends on why the run stopped. After anything but a failed merge they are, and the line says so. After a `conflict` they are not: the first merge that does not land is the last merge of the run, so the drained turns are finished, verified, recorded and settled, and their branches are left where they are. The line says that instead (`nothing more is merged into milhouse/bd-e, which has already refused milhouse/bd-e--bd-e.2`), and the report names every unlanded branch in the order it has to be landed ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)).

A turn that never began is one of those rows and not a quiet one. A wide run stops dispatching at the first agent it cannot start or cannot get a prompt into, because the next issue would be handed to the same herdr and the same agent binary, and the turn it settled goes through the table like any other — so a run whose agent side is sick stops saying `milhouse itself failed: …` rather than reporting a queue that looks stuck.

An issue that fails `--max-attempts` times does **not** stop the run. It is deferred with the reason on it, and the run moves to the next ready issue. A deferred issue is hidden from `bd ready` and still listed by `bd list`, so it still counts as unfinished — which is why a run that deferred anything exits `9` rather than claiming success. `bd undefer <id>` puts one back.

Attempts are counted over the whole audit history rather than over one run, so re-running a target does not hand a hopeless issue three more turns.

### `--count N`: several issues at once

`--count N` keeps up to N turns in flight. Each is still one issue, one fresh agent, one lane. The run dispatches up to N, polls the lanes it started every [`[run] poll_ms`](configuration.md#run), and finishes them one at a time as they settle ([ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md)).

**Ask what the count is worth before typing it.** The ready queue is the frontier of the dependency graph, so a target's shape decides how much concurrency it can use, and `--dry-run` is where that is printed:

```console
$ milhouse run bd-e --count 4 --dry-run
dry run — no agent will be started
target    bd-e  Add a hello command
scope     every ready issue under bd-e
branch    main
agent     claude
verify    uv run pytest -q
caps      50 iterations, 3 attempts per issue
run dir   /home/you/code/hello/.milhouse/runs
waves     3 unfinished issue(s) in 2 wave(s), widest 2
    1  bd-e.1, bd-e.2
    2  bd-e.3
count     4 requested, but no wave is wider than 2, so this is --count 2 with extra words
lane      milhouse/bd-e  (the integration lane; bd-e.1 would work on milhouse/bd-e--bd-e.1)
```

The waves assume every turn succeeds first time, so a real run drifts off that shape as soon as anything is retried or deferred. They answer one question, which is whether `--count 8` is worth typing against a chain of eight.

#### What the branches look like

One integration branch, and one worker branch per issue the run had in flight:

```console
$ git branch --list 'milhouse/*'
  milhouse/bd-e            # the integration branch — what you review
  milhouse/bd-e--bd-e.1    # worker branch, merged into it
  milhouse/bd-e--bd-e.2    # worker branch, merged into it
  milhouse/bd-e--bd-e.3    # worker branch, did not land
```

`milhouse status` prints the same thing joined to the lanes herdr is holding, with the worker lanes indented under the integration lane they land in:

```console
lanes (4)  issue or target, branch, checkout
  bd-e  milhouse/bd-e  /home/you/.herdr/worktrees/hello/milhouse-bd-e
      bd-e.1  milhouse/bd-e--bd-e.1  /home/you/.herdr/worktrees/hello/milhouse-bd-e--bd-e-1
      bd-e.2  milhouse/bd-e--bd-e.2  /home/you/.herdr/worktrees/hello/milhouse-bd-e--bd-e-2
  bd-x.9  milhouse/bd-x.9  /home/you/.herdr/worktrees/hello/milhouse-bd-x-9
```

The last row is a `dispatch` lane: it carries an issue id exactly as a worker lane does, and the branch is what tells them apart.

#### Inspecting and landing what a run produced

The integration branch is the unit. Everything that landed is on it, in the order the turns settled:

```sh
git log --oneline --graph main..milhouse/bd-e     # what the run built
git switch main && git merge milhouse/bd-e        # landing it is still yours
```

A worker branch is only interesting when it did **not** land. The report's `not merged` block names it, and so does the halt line:

```sh
git log --oneline milhouse/bd-e..milhouse/bd-e--bd-e.3   # the commits that are not on the branch
```

#### A `conflict` halt, and how to get out of it

A merge that did not land stops the run, and it is a halt rather than a deferral because nothing an agent could be asked next would fix it: the issue is closed, the work is done, and only a person can land the branch.

```console
merged (2)
  bd-e.1  milhouse/bd-e fast-forwarded to 9f3c1a20b6d4
  bd-e.2  merged milhouse/bd-e--bd-e.2 into milhouse/bd-e as 4c81e0f2aa19

not merged (1)
  bd-e.3  milhouse/bd-e--bd-e.3 conflicts with milhouse/bd-e in 2 file(s): src/hello.py, tests/test_hello.py. Both branches are intact; land milhouse/bd-e--bd-e.3 by hand.
  Each issue is closed and its work is on its branch. Land it by hand.

bd-e: 3 issue(s) closed, 2 merged, 1 not merged — bd-e.3 is closed but its work is not on milhouse/bd-e: …
```

Nothing was lost and nothing is half-done. milhouse ran `git merge --abort` before reporting, so the integration lane is exactly where it was, the worker branch is intact, and the issue stays closed. Redo the merge by hand in the integration lane, which is the checkout `milhouse status` names:

```sh
cd ~/.herdr/worktrees/hello/milhouse-bd-e
git merge milhouse/bd-e--bd-e.3     # same merge, this time with you resolving it
# fix the conflicts, run the gate, commit
milhouse run bd-e --count 4         # carry on: the run resumes on the same branches
```

Merging into your own checkout instead works too. Doing it in the integration lane is only so that the next run finds the branch where it expects it.

**A wide run can leave more than one branch here, and the order is the instruction.** The turns already in flight when the conflict fired are still finished and still closed, but nothing is merged after a merge that failed, so each of them appears under `not merged` saying which branch has to be landed first:

```console
not merged (3)
  bd-e.2  milhouse/bd-e--bd-e.2 conflicts with milhouse/bd-e in 3 file(s): … Both branches are intact; land milhouse/bd-e--bd-e.2 by hand.
  bd-e.3  milhouse/bd-e--bd-e.3 was not merged into milhouse/bd-e, because milhouse/bd-e--bd-e.2 did not land in milhouse/bd-e. Land that branch first, then this one.
  bd-e.4  milhouse/bd-e--bd-e.4 was not merged into milhouse/bd-e, because milhouse/bd-e--bd-e.2 did not land in milhouse/bd-e. Land that branch first, then this one.
  Each issue is closed and its work is on its branch.
  The first of these stopped the run, and nothing was merged after it.
  Land them by hand in the order above, which is the order this run
  would have merged them.
```

Land them top to bottom. That is the order the run would have merged them in, and the only one whose conflicts the report describes. The next `milhouse run` on the same target picks up from wherever you leave the integration branch.

Two conflicts are worth expecting rather than being surprised by. Agents told to "add it at the end" of a file all pick the same end, and the first turn to settle is the one that lands. Of the eight merges in the watched runs recorded in [ADR 0024](decisions/0024-an-integration-lane-and-worker-lanes.md#what-the-first-concurrent-runs-taught), three fast-forwarded, four conflicted, and one produced a merge commit.

#### A rough edge to know about

Open, reachable today, and not fixed:

- **`milhouse step` can report a prompt the agent never took as milhouse's own failure.** A submission from a non-working state has five seconds to be seen to land, whatever `[agent] turn_timeout_ms` says, and the first prompt after an agent starts is regularly swallowed outright. `step` submits once, so herdr's `agent_prompt_stalled` arrives as outcome `error`, which reads as milhouse's own failure, for a turn that never began. No tokens are spent and stepping again usually takes, but the history carries it as a failed attempt at the issue.

  `run`, `dispatch` and `reap` do not have this: they re-submit an unobserved prompt up to `[agent] submit_attempts` times, and one that still will not take stops the run there and then, naming the agent, rather than being collected a poll later as `stalled`.

### Reading the report

`--dry-run` first, always. It resolves the target, names the lane, and prints the prompt the first iteration would send, without starting anything:

```console
$ milhouse run greet-qit --repo ../greet --dry-run
dry run — no agent will be started
target    greet-qit  Add a goodbye function
scope     every ready issue under greet-qit
branch    main
agent     claude
verify    (none — a closed issue is taken on trust)
caps      50 iterations, 3 attempts per issue
run dir   /tmp/greet/.milhouse/runs
waves     2 unfinished issue(s) in 2 wave(s), widest 1
    1  greet-qit.1
    2  greet-qit.2
count     1 turn at a time, in the integration lane, with no worker lanes
lane      milhouse/greet-qit  (one lane for the whole run)

the next iteration would work greet-qit.1 and send:

    You are working **one issue** for the milhouse orchestrator. milhouse picked it,
    …
```

Point it at a leaf issue and the scope line is the other kind:

```console
$ milhouse run greet-qit.2 --repo ../greet --dry-run
target    greet-qit.2  Document goodbye in the README
scope     greet-qit.2 and its 1 unmet blocker(s)
…
the next iteration would work greet-qit.1 and send:
```

Note which issue that would work: the blocker, not the target. A leaf target is a goal, not an assignment.

A real run, from the dogfood repository, that did not finish:

```console
$ milhouse run greet-qit --repo ../greet --max-iterations 4
target  greet-qit  Add a goodbye function
scope   every ready issue under greet-qit
reconciling: re-opening greet-qit.1, claimed by a run that did not finish
created herdr workspace wG (milhouse:greet)
iteration 2: greet-qit.1 Add greet.goodbye() (attempt 2)
  lane wH on milhouse/greet-qit (/home/you/.herdr/worktrees/greet/milhouse-greet-qit)
  → timeout: the turn did not finish within the turn timeout
iteration 3: greet-qit.1 Add greet.goodbye() (attempt 3)
  lane wH on milhouse/greet-qit (/home/you/.herdr/worktrees/greet/milhouse-greet-qit)
  → partial: greet-qit.1 is still open, but 1 commit landed for it
stopping: nothing is ready but 2 issue(s) are unfinished (greet-qit.1, greet-qit.2); `bd blocked` says what is stuck, and this run deferred 1 of them
lane wH is left open (/home/you/.herdr/worktrees/greet/milhouse-greet-qit)

iterations (2, 2m)
    2  timeout   greet-qit.1  the turn did not finish within the turn timeout
    3  partial   greet-qit.1  greet-qit.1 is still open, but 1 commit landed for it

deferred (1)
  greet-qit.1  greet-qit.1 did not finish in 3 attempt(s) (last: partial, greet-qit.1 is still open, but 1 commit landed for it); deferred so the run can move on
  `bd undefer <id>` puts one back in the queue.

branch  milhouse/greet-qit
lane    /home/you/.herdr/worktrees/greet/milhouse-greet-qit

greet-qit: 0 issue(s) closed — nothing is ready but 2 issue(s) are unfinished (greet-qit.1, greet-qit.2); `bd blocked` says what is stuck, and this run deferred 1 of them
```

Worth reading closely, because most of what a run does is visible in it:

- **Iteration numbers keep counting across runs.** This one starts at 2, because iteration 1 belonged to an earlier attempt. They name the artifact files.
- **`reconciling:`** is the previous run's abandoned claim being re-opened. Running again is the resume mechanism ([ADR 0008](decisions/0008-crash-recovery-by-reconciliation.md)).
- **Both iterations name the same lane.** That is the point of [ADR 0023](decisions/0023-a-run-has-one-lane.md): the second attempt continues on the branch the first one committed to.
- **`partial` means a commit landed and the issue did not close.** The work may be nearly done, which is exactly why the note the agent leaves matters more than the outcome word.
- **The deferral is not a verdict.** `greet-qit.1` had in fact been implemented and committed by the third attempt; what it had not done was `bd close`.
- **Exit `9`, and `0 issue(s) closed`.** Nothing here pretends the target is done.

`bd undefer greet-qit.1`, then the same command again. A fourth attempt saw the commit the third one left and closed the issue:

```console
$ milhouse run greet-qit --repo ../greet --max-iterations 4
target  greet-qit  Add a goodbye function
scope   every ready issue under greet-qit
iteration 4: greet-qit.1 Add greet.goodbye() (attempt 4)
  lane wH on milhouse/greet-qit (/home/you/.herdr/worktrees/greet/milhouse-greet-qit)
  → success: greet-qit.1 closed in beads
iteration 5: greet-qit.2 Document goodbye in the README
  lane wH on milhouse/greet-qit (/home/you/.herdr/worktrees/greet/milhouse-greet-qit)
  → success: greet-qit.2 closed in beads
stopping: no issues are ready; everything in scope is closed
lane wH is left open (/home/you/.herdr/worktrees/greet/milhouse-greet-qit)

iterations (2, 2m)
    4  success   greet-qit.1  greet-qit.1 closed in beads
    5  success   greet-qit.2  greet-qit.2 closed in beads

branch  milhouse/greet-qit
lane    /home/you/.herdr/worktrees/greet/milhouse-greet-qit

greet-qit: 2 issue(s) closed — no issues are ready; everything in scope is closed
```

Both iterations name the same lane, and the result is one branch:

```console
$ git -C /home/you/.herdr/worktrees/greet/milhouse-greet-qit log --oneline main..HEAD
cbb3902 greet-qit.2: document goodbye() in the README
8a0e442 greet-qit.1: add greet.goodbye()
```

That is what there is to review. Exit `0`.

### Before an unattended run

- **Set `[verify] command`.** Without it milhouse takes every `bd close` at face value ([ADR 0016](decisions/0016-milhouse-verifies.md)), and a falsely closed issue is the one failure it cannot detect. Unattended is exactly when nobody is checking.
- **Deal with permissions first.** A default agent stops at its first permission prompt, the run halts, and you have spent one turn learning that. `[agent] args` is where the escape hatch goes ([ADR 0009](decisions/0009-permission-posture.md)), and an agent's consent screen still has to be accepted by hand once.
- **`--max-iterations` bounds turns, not spend.** Turns are not the same size, and milhouse cannot see cost through a herdr pane ([ADR 0012](decisions/0012-no-cost-controls-in-v1.md)).
- **Watch one `milhouse step` first.** It costs one turn to find out that your issue descriptions are too thin for an agent with no context, and fifty to find out the expensive way.
- **Raise `--count` after a watched run, not before one.** A wide run multiplies everything, including the failures: N agents starting at once, a gate run per merge that joined anything, and conflicts to resolve by hand. `--count 1` is [ADR 0023](decisions/0023-a-run-has-one-lane.md) exactly, so the serial path stays available as the thing you already trust.

## `milhouse dispatch` and `milhouse reap`

`step` is one turn with the waiting included. `dispatch` and `reap` are that same turn cut in half, so several can be in flight at once ([ADR 0020](decisions/0020-a-lane-is-a-herdr-worktree.md)):

```
milhouse dispatch [-n COUNT] [--agent KIND] [--workspace ID] [--parent ID] [--label NAME] [--attach] [--repo PATH]
milhouse reap [--repo PATH]
```

`dispatch` claims up to `--count` ready issues, sets each one up in its own lane, starts its agent, and returns without waiting for any of them. `reap` finds the turns that have settled since and finishes them: transcript, classification, verification, and whatever the policy says becomes of the issue.

```console
$ milhouse dispatch -n 3
iteration 7: dogfood-6i2.2 Make the src-layout greet package importable
  lane wL5 on milhouse/dogfood-6i2.2 (/home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-2)
  → dispatched to wL5
iteration 8: dogfood-6i2.3 Add tests/test_greet.py covering hello and goodbye
  lane wL6 on milhouse/dogfood-6i2.3 (/home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-3)
  → dispatched to wL6

2 turn(s) in flight:
  dogfood-6i2.2  milhouse/dogfood-6i2.2  /home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-2
  dogfood-6i2.3  milhouse/dogfood-6i2.3  /home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-3

run `milhouse reap` when they settle.
```

Reaping is safe to run at any time. A turn still working is left alone and picked up next time:

```console
$ milhouse reap
dogfood-6i2.3 is still working
reaping iteration 7: dogfood-6i2.2
  → success: dogfood-6i2.2 closed in beads

dogfood-6i2.2: success — dogfood-6i2.2 closed in beads

1 turn(s) still running.
```

`dispatch` exits `0` when it started at least one turn and none failed to start, and `9` when nothing was ready but work is outstanding, or when a turn could not be started. `reap` exits `0` when everything it collected succeeded and nothing is left running, and `9` otherwise — so `until milhouse reap; do sleep 60; done` is a wait.

**A turn that will not start ends the call.** Its issue is re-opened rather than left claimed with no agent behind it, and the failure is printed with the issue id. The issues behind it in the queue are not claimed at all: the next one would be handed to the same herdr and the same agent binary, and each one tried would be charged an attempt in the history a later `milhouse run` counts against `--max-attempts`.

**This is still not a loop.** `dispatch` starts a bounded number of turns once and stops; nothing here decides whether there should be more ([ADR 0017](decisions/0017-no-loop-until-it-is-earned.md)). What it removes is the requirement that the turns be serial.

Three things follow from that:

- **The lock is per lane, not per repository.** Two dispatchers in the same repository are safe, because `bd ready --claim` is atomic and neither can take the other's issue ([ADR 0015](decisions/0015-one-run-at-a-time.md)).
- **A dispatched turn outlives the process that started it.** Closing the terminal does not re-open the claim: an agent is working it, and reaping is what settles it. What the reap needs is written to the audit log at dispatch.
- **A turn milhouse can no longer find a lane for is re-opened** the next time anything runs, because an issue that is `in_progress` with no live lane has nobody working it.

A turn that outlives `[agent] turn_timeout_ms` is collected anyway and classified `timeout`, exactly as `step` would. Nothing is waiting on it, so the deadline is measured from the time recorded when it started.

## `milhouse status`

What is in scope, what lanes are open, what is claimed, and this repository's iteration history. Reads beads, herdr, and git; starts nothing and changes nothing.

```
milhouse status [--repo PATH]
```

```console
$ milhouse status
repo    /home/agent/code/github.com/kris-steinhoff/greet
scope   every ready issue in the repository
branch  main
herdr   workspace wY (labelled milhouse:greet)

  [x] dogfood-6i2.1  Add goodbye(name) to src/greet/__init__.py and document it in README.md  (closed)
  [ ] dogfood-6i2.2  Make the src-layout greet package importable when running python -m pytest  (open)
  [ ] dogfood-6i2.3  Add tests/test_greet.py covering hello and goodbye  (open)

lanes (2)
  dogfood-6i2.1  milhouse/dogfood-6i2.1  /home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-1
  dogfood-6i2.2  milhouse/dogfood-6i2.2  /home/you/.herdr/worktrees/greet/milhouse-dogfood-6i2-2

iterations (2)
    1  success   dogfood-6i2.1  dogfood-6i2.1 closed in beads
    2  stalled   dogfood-6i2.2  dogfood-6i2.2 is still open and nothing was committed
```

It also flags any claim or lock left behind by an unfinished run. The history spans every invocation in this repository, not just the last one, because it is read back out of the beads audit log — one append-only trail that `bd`'s own entries share ([ADR 0021](decisions/0021-iteration-history-goes-in-the-beads-audit-log.md)).

`bd audit` has no query, so `milhouse status` is the readable view of `.beads/interactions.jsonl`. Reading the file directly works too, and shows bd's entries interleaved with milhouse's:

```sh
jq -c 'select(.kind == "iteration") | {issue_id, extra}' .beads/interactions.jsonl
```

## End-to-end check

The manual check that the loop really works. It needs eyes on it, because the thing being verified — that the context is fresh every iteration — is only visible in the pane.

```sh
milhouse doctor            # all required rows green
bd ready                   # there is something to work
milhouse step --dry-run    # the prompt looks right
milhouse step --attach     # one iteration, watched
milhouse step --attach     # and another
```

Watch for:

1. A workspace named `milhouse:<repo>` appears.
2. The pane shows the agent starting and working.
3. **The pane returns to a shell prompt when the step ends.** This is the one that matters: it is what proves the next iteration gets a fresh context window.
4. The issue closes in `bd`, and `milhouse status` shows both iterations.

Then trigger a permission prompt deliberately and confirm herdr reports `blocked`, and that milhouse stops with the workspace to attach to rather than sitting there.

Then check the concurrent path, which is the one with the most ways to be subtly wrong:

```sh
milhouse dispatch -n 2     # two lanes, two agents, back immediately
milhouse status            # both lanes listed, both claims in flight
milhouse reap              # nothing settled yet — exits 9
milhouse reap              # again once they finish
```

Watch for:

1. Two workspaces appear, labelled with the two issue ids, in separate worktrees.
2. `milhouse dispatch` returns while both agents are still working.
3. Killing the dispatching terminal does **not** re-open either claim.
4. `milhouse reap` classifies each turn once, and reaping again finds nothing.

Then a concurrent run, against a scratch repository with a real `[verify] command` and at least two independent ready issues:

```sh
milhouse run <target> --count 2 --dry-run   # the waves, and what the count is worth
milhouse run <target> --count 2 --attach
```

Watch for:

1. An integration lane on `milhouse/<target>`, and a worker lane per issue on `milhouse/<target>--<issue>`.
2. Progress lines labelled with their issue, because two turns are interleaving them.
3. A `merging … into …` line per successful turn, and `fast-forwarded` on the first one.
4. `milhouse status` grouping the worker lanes under the integration lane.
5. Every closed issue's commits reachable from `milhouse/<target>`, or named in the report's `not merged` block. Nothing should be in neither.

Finally, run `milhouse step` twice at once in two terminals against the **same** issue and confirm the second refuses with exit code `10`. Against different issues both should run.

## Exit codes

Stable, and safe to branch on in a script.

| Code  | Error                    | Means                                                                  |
| ----- | ------------------------ | ---------------------------------------------------------------------- |
| `0`   | —                        | Success.                                                               |
| `1`   | `MilhouseError`          | An expected failure with no more specific category.                    |
| `2`   | `ConfigError`            | `.milhouse/config.toml`, an env var, or a flag is invalid.             |
| `4`   | `TrackerError`           | `bd` failed, or the beads database is missing.                         |
| `5`   | `HerdrError`             | `herdr` failed, or the server is unreachable.                          |
| `6`   | `AgentError`             | An agent could not be started, prompted, or exited.                    |
| `7`   | `MissingDependencyError` | A required tool is not on `PATH`. Also `doctor`'s failure code.        |
| `8`   | `ProcessError`           | A subprocess failed in a way no caller translated.                     |
| `9`   | (no exception)           | A step did not finish its issue, or a run stopped short of its target. |
| `10`  | `RunLockedError`         | Another milhouse process is already working this lane.                 |
| `130` | `UserAbortError`         | Interrupted, or a confirmation was declined.                           |

`3` is retired. It was `SourceError`, raised when a task definition could not be resolved, and there are no task definitions ([ADR 0018](decisions/0018-no-task-milhouse-works-the-ready-queue.md)). The codes do not renumber, because scripts branch on them.

Every error prints one line on stderr, plus a remedy line when there is something specific to try. `9` is the exception: it is an outcome rather than a failure, so it is reported on stdout like any other result.
