# Live pilot plan

This plan was committed before any pilot episode ran. It describes a small, staged study of whether the obligation ledger changes what a coding agent finishes. It is a pilot: the numbers will be too small to support a general claim, and the plan says in advance what each outcome would and would not show.

## Question

When an agent implements a feature and leaves one place inconsistent, does the ledger's completion check get that place fixed more often than a generic reminder to look again?

The comparison that matters is the ledger against the reminder. A completion check gives the agent a second look, and a second look alone may be what helps.

## Tasks

Each task comes from an upstream commit in which the human author made an omission that the [held-out replays](../../results/held_out/README.md) reported and readers confirmed. The agent starts from the commit's parent tree, as a single commit with no later history, and receives a ticket that describes the feature without naming the place upstream missed. Tickets, hidden omitted sites, and grading rubrics are in [tasks/](tasks/).

| Task | Omission upstream made | Role |
| --- | --- | --- |
| `rich-locals-depth` | `Traceback.__init__` does not pass the new option to `extract` | Measure |
| `httpx-http1` | The async client does not pass `http1` to its proxy transport | Measure |
| `httpx-redirects` | The public client constructors do not accept the new option | Control. The ticket cannot avoid naming the constructors |
| `paramiko-agent-sha2` | `AgentKey.sign_ssh_data` ignores the new `algorithm` | Not run. The feature is about 280 lines across 15 files, more than one capped episode |

Seven candidates were considered. Three were rejected while the tasks were written: one duplicated the rich site, and two could not be stated as a request without either naming the site or dictating the implementation.

## Arms

All arms use the same model, prompt, and tools (read, search, edit; no shell). Sessions load only the episode directory's settings, so no user-level hooks or plugins take part.

- **plain:** no hooks.
- **reminder:** a Stop hook that blocks once with a fixed, generic request to re-check callers, wrappers, overrides, and sibling implementations. It runs no analysis.
- **ledger:** the `mip-delta` post-edit and stop hooks with the default detector set, from the engine at commit `21c9341`.

## Stages

**Stage 1.** Two plain episodes for each of the three tasks.

A task goes on to Stage 2 only if at least one of its plain episodes is graded `omitted`. If the unaided model makes none of these omissions, the pilot cannot tell the arms apart, and it stops there. That outcome is reported as a result.

**Stage 2.** For each task that goes on: two more plain episodes, four reminder episodes, and four ledger episodes.

## Grading

Two graders read each episode's final patch against the task's rubric and label it `covered`, `omitted`, or `not_attempted`. They see the patch and the rubric only: not the arm, the transcript, or each other's label. A third read resolves disagreements. The graders are model subagents.

## Measures

- **Primary:** episodes graded `covered`, per task and arm.
- **Secondary, ledger arm:** whether the hook reported the omitted site, from the event log, and whether the site was then fixed.
- **Cost:** dollars, turns, and stop-hook blocks per episode.

## Reading the result

| Outcome on a task | Reading |
| --- | --- |
| Ledger covers at least two more episodes of four than the reminder | Weak evidence that the ledger's content helps beyond a second look. Worth a larger study |
| Ledger and reminder are within one episode of each other, both above plain | The second look helps. No evidence the ledger adds to it |
| All three arms are within one episode of each other | No effect seen at this size |
| The hook did not report the omitted site in ledger episodes graded `omitted` | The agent's version of the omission differs from upstream's in a way the check misses. Reported with the patch |

Four episodes per arm cannot show a small effect, and they cannot rule one out.

## Budget

Each episode is capped at $1.50 by the command line. The runner refuses to start an episode that could take total spending past $45, and a run that ends without reporting its cost is charged its full cap. The approved limit is $50, of which two probe sessions used $0.14.

## Limits known in advance

- **The tasks favor the check.** Every task is an omission the check reported on the human's commit. The pilot does not sample omissions the check would miss.
- **The model may remember the repositories.** Later upstream code contains the fix for the two httpx tasks. For the rich task the gap is still present upstream, so memory works the other way.
- **Two or three tasks, one model, one prompt.**
- **No shell.** The agent cannot run code or tests, which removes one way it would otherwise notice an omission.
- **Grading is by model readers from the patch.** Nothing is executed.

## Run it

```sh
python experiments/pilot/run_episode.py \
  --task experiments/pilot/tasks/rich-locals-depth.json --arm plain --rep 1 \
  --clone ../clones/rich --work ../pilot/episodes --out ../pilot/results \
  --hook-dir ../pilot-hooks/bin --spend-ledger ../pilot/spend.jsonl --total-cap-usd 45
```

`--hook-dir` holds copies of the scripts in [hooks/](hooks/), with an engine snapshot beside it as `../engine/src/mipdelta`. Keep that directory where only the owning account can write, because the session executes those files.
