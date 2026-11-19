# Sherlock snapshot-recall results

These results follow the [frozen plan](../../experiments/sherlock_recall/README.md), committed as `20841a1` before the analyzer ran on any measurement-set contest. The engine is `src/mipdelta` at `3ebdabd`, tree `39b06e1f405a`, and it was unchanged throughout. The measurement set is 16 Sherlock contests from 2024–2025, holding 156 judged High and Medium findings, 43 of them High.

This is **snapshot recall**. `mip-delta --audit-solidity` read each contest's code as it stood at audit time, with no diff. The results say nothing about change review.

## Summary

- **About a third of paid findings have omission or consistency structure.**
  - 17.3% are in-class: one site meets an obligation and a related site doesn't.
  - Another 14.7% are adjacent: an omission that needs semantics mipcode doesn't model.
  - 67.9% are out of class: logic, math, economics, oracles or integrations.
  - Among Highs, 16.3% are in-class.
- **The snapshot analyzer reached none of them.**
  - In-class recall is 0 of 23, with a Wilson 95% interval of 0–14.3%. Adjacent recall is 0 of 16.
  - The interval lies wholly below 15%, so the pre-registered outcome is **low**. The snapshot analyzer doesn't reach paid in-class bugs, and a quiet result on contest code carries little information.
- **The next build is sibling-guard asymmetry, by the frozen decision rule.**
  - It leads the miss buckets with 18 findings (4 High, 14 Medium). The next bucket is safety-parameter weighting with 10, a margin of 8, so the rule's tie clause doesn't apply.
  - It needs no new parser.
  - Before the run, sibling-guard asymmetry and fork-upstream diffing were expected to lead. Sibling guards did; fork-upstream diffing did not, with 1 finding.

## Class coverage (headline 1)

| Severity | n | in_class | adjacent | out_of_class |
| --- | ---: | ---: | ---: | ---: |
| High | 43 | 7 (16.3%) | 2 (4.7%) | 34 (79.1%) |
| Medium | 113 | 20 (17.7%) | 21 (18.6%) | 72 (63.7%) |
| **All** | **156** | **27 (17.3%)** | **23 (14.7%)** | **106 (67.9%)** |

All 156 findings were labeled; none is unlabeled or low-confidence.

The final patterns for in-class and adjacent findings were:

| Pattern | Findings |
| --- | ---: |
| `other` | 19 |
| `sibling_missing_guard` | 11 |
| `paired_op_mismatch` | 7 |
| `param_unread` | 4 |
| `hardcoded_safety_param` | 4 |
| `override_mismatch` | 2 |
| `param_not_forwarded` | 2 |
| `dead_state` | 1 |

The fixed pattern list leaves 19 of 50 as `other`. That is a hint the taxonomy is missing shapes.

## Miss buckets (headline 2)

All 50 in-class and adjacent findings are misses. Each is counted once under each of its blind `would_catch_with` tags.

| Capability | High | Medium | Total | As the only tag |
| --- | ---: | ---: | ---: | ---: |
| `sibling_guard_asymmetry` | 4 | 14 | **18** | 15 |
| `safety_param_weighting` | 3 | 7 | 10 | 8 |
| `cross_file_or_dependency_visibility` | 0 | 8 | 8 | 4 |
| `existing_detector_gap` | 1 | 1 | 2 | 2 |
| `fork_upstream_diff` | 0 | 1 | 1 | 1 |
| `none_of_these` (excluded from the rule) | 0 | 12 | 12 | 11 |

Four findings (1 High, 3 Medium) carry no tag, because their class-matching readers chose disjoint tag sets (`capability_split`).

Only 2 findings are tagged `existing_detector_gap`. So the misses are mostly shapes today's detectors don't target, not detectors that failed to fire.

## Recall (secondary)

| Class | Findings with a production site | Strict | Full (model) | Full (human) | Wilson 95% |
| --- | ---: | ---: | ---: | ---: | --- |
| in_class | 23 of 27 | 0 | 0 | 0 | 0–14.3% |
| adjacent | 16 of 23 | 0 | 0 | 0 | 0–19.4% |

**What the analyzer reported.** 155 findings across 16 contests, of which 24 are in production files. That is 0.16 per 1,000 production lines over 882 files and 149,415 lines. Analyzer wall time totaled 20.5 s.

**Candidates.** Only 4 analyzer findings landed in a file a judged finding cites or names as its obligation site. All 4 are one `parameter_sink_review` report in teller-finance, about an unused `_collateralRecipient` parameter. The same-defect reader judged it:
- `related` to H-1 (strict closeness: the same function, but a different root cause);
- `unrelated` to H-9, M-1 and M-10 (file closeness).

**Human spot-check.** It applies only to `same_defect` hits, and there were none, so the operator had nothing to check. Human-confirmed recall equals model-confirmed recall by construction (ledger R24).

Read together with class coverage, as the plan requires: 0% recall on a class that is 17% of paid findings, plus 15% adjacent, means the present snapshot detectors are not where the value lies on contest code. The miss buckets say what to build instead.

## Readers

**Calls.** 360 reader calls, all answered: Sonnet 156, Haiku 156, Opus tie-breaker 48. There were no refusals, no invalid outputs, and no served-model mismatches (`fallback_ran` 0). The same-defect reader added 4 calls.

**Agreement between Sonnet and Haiku:**

| Label | Raw agreement | Cohen's κ |
| --- | ---: | ---: |
| class | 80.1% | 0.60 |
| pattern (n=125) | 92.0% | 0.82 |

Mean Jaccard on the tag sets is 0.74. The raw labels are in `labels_measurement.json`.

**Cost.** CLI-reported notional cost for the measurement set was about $4.72 (cumulative ledger $6.52, including the dev set and preflight), against the $15 cap. Recomputed at list prices from recorded tokens, it is $3.99. It was paid from the operator's Claude subscription, not billed (ledger R19).

## Deviations and disclosures

- **Analyzer re-run (R21).** The first measurement run recorded harness errors for 4 contests:
  - nouns-dao-2, malda and teller-finance: Foundry writes `broadcast/*.s.sol/` directories, which `rglob("*.sol")` matched, and reading them failed;
  - leveraged-vaults: a file named with `:` can't be checked out on NTFS.

  The fixes (`4494caa`..`123e9d8`) touch only `run_recall.py`. Directories are skipped. For leveraged-vaults, a plumbing checkout writes only safe regular blobs. It dropped 22 entries: 19 colon-named JSON files and 3 submodule links, and no `.sol` file. No git security setting was changed.

  The whole measurement set was then re-run once. The 12 contests that had succeeded reproduced identically, apart from `wall_ms`. Only per-contest counts were seen between the two runs. No detector, prompt or threshold changed.
- **Transport (R19).** Labeling used Claude Code CLI 2.1.322 in print mode on the subscription, with no server-side fallbacks. The frozen plan discloses the reader context.
- **Obligation sites.** One obligation site couldn't be resolved: teller M-11 names a function rather than a file. That finding still has its cited-file candidates.
- **Dev set.** Dev numbers are in `summary_dev.json` and are not pooled here.

## Limits

- **Model labels.** Class, pattern and capability labels come from model readers; no human relabeled them. Class κ of 0.60 is moderate, and class carries more uncertainty than pattern.
- **Small in-class sample.** With n=23, recall can't be told apart from a few percent. The outcome reading rests on the interval's upper bound of 14.3%.
- **Snapshot mode.** The analyzer saw no diffs. A change-review measurement needs the fix commits, not the audited snapshot.
- **Narrow scope.** The draw covers Sherlock's EVM contests only. Fourteen non-Solidity contests were excluded at selection.

## Files

- **Inputs:** `contests.json` and `recall_set.json` hold the candidates, the draw, the findings and the sites.
- **Pipeline outputs:**
  - `hits_measurement.json`: analyzer output per contest;
  - `labels_measurement.json`: raw reader labels and consensus;
  - `candidates_measurement.json` and `same_defect_measurement.json`;
  - `summary_measurement.json`: every measure above.
- **Dev set:** the same files with the `_dev` suffix.
