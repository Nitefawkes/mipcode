# Live pilot results

The pilot followed the [plan](../../experiments/pilot/README.md) committed in `4cb2864` before any episode ran. It stopped after Stage 1, as the plan requires: the unaided model did not make any of the omissions the tasks are built on, so the arms cannot be told apart on these tasks.

## Stage 1

Six episodes, two per task, with no hooks. The model was `claude-opus-5-5`, with read, search, and edit tools and no shell.

| Task | Omission the human author made | Episode 1 | Episode 2 |
| --- | --- | --- | --- |
| `rich-locals-depth` | `Traceback.__init__` does not pass the new option to `extract` | Covered | Covered |
| `httpx-http1` | The async client does not pass `http1` to its proxy transport | Covered | Covered |
| `httpx-redirects` (control) | The public client constructors do not accept the new option | Covered | Covered |

Two graders labeled each patch against the task's rubric without seeing the other's label. They agreed on all six, each with high confidence, and both judged every patch a complete implementation of its ticket.

Every task has an episode graded `omitted` in zero of two plain episodes, so no task went on to Stage 2. The reminder and ledger arms did not run.

| Measure | Value |
| --- | --- |
| Cost per episode | $0.25 to $0.57 |
| Turns per episode | 14 to 33 |
| Wall time per episode | 36 to 84 seconds |
| Total pilot spending, including two probe sessions | $2.31 of the $50 limit |

## What this shows

On three tasks where a human author left a place inconsistent, a current frontier model given the same request covered that place every time, without a reminder or a checker. For these tasks there was nothing for the ledger to catch.

That is a result about the check's room to help, and it fits what the probe session showed: before changing a function, the model searched for its callers on its own.

## What it does not show

- **Six episodes on three tasks.** A model that makes such an omission one time in ten would usually pass this stage.
- **The tasks are small.** Each is one option threaded through two or three files, finished in about a minute. An omission may be more likely in a long session, a large change, or a codebase where the affected place is far from the edit.
- **The tasks came from the check's own reports on human commits.** They are the omissions that are easiest for a static check to see, and they may also be the ones a careful reader sees most easily.
- **The model may remember the repositories.** Later httpx releases contain the fix for both httpx tasks. For rich the gap is still present upstream, and the model covered it anyway.
- **One model.** A smaller or cheaper model may behave differently.
- **Nothing was executed.** Grading is by model readers from the patches.

## Observations from the patches

These were not part of the graded question.

- **rich.** Both patches forward the new option at the site, and both leave the four older options unforwarded there, as they were in the parent tree. The agents fixed what the ticket asked for and did not repair the neighboring gap. One patch also added a missing option to the exception-group call inside `extract`.
- **httpx `http1`.** Both patches also pass `http1` to httpcore's proxy classes, which the upstream commit did not do. Whether those classes accept it at the pinned httpcore version was not checked. If they do not, building a proxy transport would fail.
- **Tool use.** Four of the six sessions attempted a tool that was not allowed, between one and four times, and continued without it.

## Hook mechanics, from the probe sessions

Two short probe sessions ran before the pilot, at a cost of $0.14.

- A headless session loads the episode directory's hooks and no user-level settings or plugins.
- The post-edit hook's added context reaches the model.
- The Stop hook blocks once, the model receives the list of open sites and continues, and the event log records the opened sites, the block, and each run's duration.

So the ledger arm was ready to run. It was not needed.

## Data

- [stage1.json](stage1.json) holds each episode's cost, turns, changed files, and both graders' labels with the lines they relied on.
- [patches/](patches/) holds each episode's final patch.
