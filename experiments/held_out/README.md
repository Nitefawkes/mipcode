# Held-out validation plan

This plan was written and committed before any held-out repository was analyzed. It fixes the engine, the repositories, the measures, and the thresholds for two offline studies. Neither study calls a model.

The [first history replay](../../results/history_replay/README.md) was used to change the detectors, so its second and third runs are a fit to those five repositories. This plan tests whether the result holds on repositories nobody consulted, and adds a recall measurement that the replay cannot give.

## What is frozen

- **Engine:** `src/mipdelta` at commit `c3ccbb624fed`, tree `a0983ac2a393`. No detector or resolver change is made between this commit and the reported results.
- **Detector set:** the default set. `parameter_truthiness_review` stays off.
- **Replay script:** `experiments/history_replay/replay_history.py` as of the same commit, with `--horizon 25 --limit 800`.

A change made in response to these results needs a new held-out set before it can be reported as an improvement.

## Repositories

| Repository | Head | Python files |
| --- | --- | ---: |
| [attrs](https://github.com/python-attrs/attrs) | `9d9f94a043de` | 56 |
| [httpx](https://github.com/encode/httpx) | `b5addb64f016` | 60 |
| [marshmallow](https://github.com/marshmallow-code/marshmallow) | `a38b7f684204` | 38 |
| [rich](https://github.com/Textualize/rich) | `3a109ccdffe5` | 212 |
| [mkdocs](https://github.com/mkdocs/mkdocs) | `2862536793b3` | 65 |

Selection rules, applied before cloning:

- Not used in any earlier MipCode study or tuning. That excludes click, packaging, platformdirs, tomlkit, werkzeug, docutils, and rdflib.
- Not maintained by the same group as an excluded repository. That excludes jinja2 and the other Pallets projects.
- At least one application-style codebase (mkdocs) and one larger package (rich).

Each is a shallow clone of the default branch. The replay covers the most recent 800 non-merge commits of each. Only the commit count and file count of each clone were read before this plan was committed.

## Study 1: silence on complete changes

The method is the [history replay protocol](../history_replay/README.md), unchanged.

**Primary measure:** the pooled share of contract-changing commits with no reported production site.

**Secondary measures:** reported production sites per 100 contract-changing commits, silence per repository, and sites per detector.

| Outcome | Rule |
| --- | --- |
| Generalizes | Pooled silence is at least 93.0%, and every repository with 30 or more contract-changing commits is at least 88.0% |
| Does not generalize | Pooled silence is below 90.0% |
| Partial | Anything else. The report names the detectors that account for the gap |

For reference, the tuned repositories gave 85.6% before the detector changes and 97.8% with the current default set.

**Adjudication.** Silence does not say whether the reports that remain are useful. Every reported production site is read, or a random sample of 60 stratified by detector (seed `20261112`) if there are more than 60. Two readers classify each site independently from the commit diff and the surrounding source:

- `real_omission`: the commit left this site wrong, shown by a later fix or by the code itself.
- `review_worthy`: the code is legal, and a reviewer would reasonably want to look at this site because of the change.
- `false_report`: the site has no real relation to the change, or the reported relation is wrong.

The readers are model subagents that do not see each other's labels. A third read resolves disagreements. The report gives the agreement rate and the raw labels.

## Study 2: recall on constructed omissions

The method is the [stale-file mutation protocol](../recall_mutation/README.md). It takes an upstream commit that changes a function's parameters and also edits a call to that function in another file, then restores the other file to its earlier version. The restored file is a known omission. The ground truth uses the standard library `ast` module only and does not depend on `mip-delta`.

The harness is developed and debugged on the five tuned repositories only. It is then run once on the held-out repositories with the frozen engine.

**Primary measure:** site-level recall in the stratum where the earlier call cannot bind to the new signature and the function name is defined once in the tree.

**Secondary measures:** recall in the other strata, mutation-level recall, and reports outside the restored file.

| Outcome | Reading |
| --- | --- |
| At least 70% | The check resolves most constructed omissions of the kind it models |
| 40% to 70% | Resolution coverage is a material limit. The report lists the unresolved call forms |
| Below 40% | Resolution coverage is the main limit, and a quiet result carries little information |

**Type-checker baseline.** A type checker is run on the same mutated trees, and on the complete trees for comparison. Each ground-truth site is then one of: reported by both, by `mip-delta` only, by the type checker only, or by neither. This shows what the check adds to a tool that projects may already run. The baseline was not built when this plan was committed, so the choice of type checker and its configuration will be recorded with the results.

## Limits known in advance

- The constructed omissions are signature changes and removals. Recall on them is an upper bound for omissions in general. Return-shape and exception changes are not covered.
- Five repositories are a small sample, and all are libraries or tools with test suites.
- The adjudication labels come from model readers. They are a consistent reading, not ground truth.
- A silent commit says nothing about omissions the check cannot see.
