# Sherlock snapshot-recall plan

This plan was written and committed before the analyzer ran on any measurement-set contest. It fixes the engine, the contests, the reader setup, the measures and the thresholds for one study. The study asks how much of what Sherlock judges paid for falls inside mipcode's consistency class, and which missing capability accounts for most of the misses.

This is **snapshot recall**. The analyzer reads each contest's code as it stood at audit time, and no diff is involved. `--audit-solidity` reports the obligations it finds in that snapshot. The study does not test the change-review mode that mipcode is built around. A low recall number is expected. The two headline measures are class coverage and the miss-bucket ranking. Recall is secondary.

The design is [`docs/superpowers/specs/2026-11-18-sherlock-recall-set-design.md`](../../docs/superpowers/specs/2026-11-18-sherlock-recall-set-design.md), including its 2026-11-19 transport amendment.

## What is frozen

- **Engine:** `src/mipdelta` at commit `3ebdabd1ecda` (v0.6.4), tree `39b06e1f405a`, with the default detector set. `src/mipdelta` is byte-identical between that commit and this plan's commit. No detector or resolver change is made between this commit and the reported results.
- **Harness:** the scripts in this directory as of this commit. These are `sherlock_report.py`, `build_set.py`, `match_hits.py`, `run_recall.py`, `llm.py`, `label.py` and `score.py`.
- **Prompts:**
  - `prompts/class_reader.md`: file SHA-256 `37c91d2410170b352328a40d3cf765c45d110fad70b33ab8929705b9dfebebc6`; prompt+schema key `be144c156c6cdefb4c37e7af37325b878256a65a1756743cdb6c0876c65a4107`.
  - `prompts/same_defect.md`: file SHA-256 `b2ed054c019ca570f2d6e7e416c0f4f32c826e8b634acbb3deddefe72616d324`; prompt+schema key `907a0349707030c0f219a7de1187c4df7efff5d9b7fb08a04e3567421dd7fac3`.

  The full prompts are the committed files. They include two lines that rulings added (ledger R13 and R14):
  - The class reader uses `none_of_these` only when no other tag applies.
  - The same-defect reader counts a peer, base or declaration site as the same defect when fixing the root cause would also resolve what the analyzer reports.
- **Pattern list:** unchanged from the spec. No tags were added on the dev set.
- **Runtime:** Python 3.12.6 on Windows 11, `gh` and `git` through `subprocess`.

A change made in response to these results needs a new draw before it can be reported as an improvement.

## Contests

**Candidates.** These are the `sherlock-audit/*-judging` repositories whose contest month falls from 2024-01 to 2025-12. Of 229 judging repositories, 118 are in that window.

| Eligibility | Count |
| --- | ---: |
| Eligible | 89 |
| Not Solidity (under 80% of site links point to `.sol` files) | 14 |
| No High or Medium issue | 13 |
| Companion repo missing | 2 |
| Excluded protocol (own code in an existing mipcode corpus) | 0 |
| Assessment error | 0 |

Four eligible contests have site links pinned to more than one commit (`|mixed_pins`). For those, the companion ref is the last commit before the judging repo's first issue. A permalink on `main` is resolved the same way, with a fallback to the default-branch HEAD that is flagged `|ref_fallback_head`. `2024-10-morpho-x-index` is eligible by override, because it is Index Coop code that integrates Morpho.

**Draw.** The eligible list is sorted by name and sampled with `random.Random(20261118)`. The first four are the dev set; the other 16 are the measurement set.

| Set | Contest | H/M findings | Commit |
| --- | --- | ---: | --- |
| dev | 2025-06-superfluid-locker-system | 8 | `d8beaeed47` |
| dev | 2024-04-alchemix | 1 | `32e4902f77` |
| dev | 2024-06-new-scope | 26 | `c8300e73f4` |
| dev | 2024-02-perennial-v2-3 | 13 | `86ea166a20` |
| measurement | 2024-02-jala-swap | 3 | `030d3ed542` |
| measurement | 2024-01-olympus-on-chain-governance | 4 | `6171681cfe` |
| measurement | 2024-04-interest-rate-model | 18 | `8f6ef1b086` |
| measurement | 2024-07-kwenta-staking-contracts | 1 | `0527fb7425` |
| measurement | 2024-06-leveraged-vaults | 19 | `14d3eaf044` |
| measurement | 2024-11-ethos-network-ii | 7 | `57c02df7c5` |
| measurement | 2024-08-saffron-finance | 5 | `38dd9c8436` |
| measurement | 2024-03-nouns-dao-2 | 3 | `8f6879efaf` |
| measurement | 2024-04-arcadia-pricing-module | 3 | `54832626b8` |
| measurement | 2025-07-malda | 17 | `798d00b879` |
| measurement | 2024-01-looksrare | 4 | `7d76b96a58` |
| measurement | 2024-04-teller-finance | 29 | `defe55469a` |
| measurement | 2024-12-numa-audit | 16 | `ae1d7781ef` |
| measurement | 2024-02-leverage-contracts | 3 | `c9ac221d2a` |
| measurement | 2024-07-exactly-stacking-contracts | 13 | `3eb87e3edf` |
| measurement | 2024-05-napier-update | 11 | `c31af59c63` |

The dev set has 48 findings, 17 of them High. The measurement set has 156 findings, 43 of them High. 121 of the measurement findings have a companion `.sol` site. No measurement-set contest has been cloned or analyzed before this commit; only its judging report has been parsed. The full candidate list is `results/sherlock_recall/contests.json`, and the findings and sites are in `recall_set.json`.

## Ground truth

- **Unit.** One `# Issue H-n` or `M-n` section of the final judging report counts as one finding.
- **Sites.** A site is a permalink into the companion repo that points at a `.sol` file. It must appear in `## Code Snippet` (old format), `### Root Cause` (new format), or Vulnerability Detail when an old-format issue has no snippet.
  - Links in PoC, Mitigation, Recommendation and Discussion are not sites.
  - Links into other repositories are marked `external` and don't count toward recall.
- **Production files.** Recall counts only sites in production files. A production file is not under `test/`, `tests/`, `script/`, `scripts/`, `mock/`, `mocks/`, `lib/` or `node_modules/`, and is not named `*.t.sol` or `*.s.sol`.

## Readers

Readers see the finding's title and its reader sections, plus each cited site's lines with ±20 lines of context. They never see PoC code, Discussion, or analyzer output. The class prompt carries the spec's Mitigation-scoping instruction verbatim.

| Role | Model | Effort |
| --- | --- | --- |
| Reader A | `claude-sonnet-5-5` | `low` |
| Reader B | `claude-haiku-5-5` | `low` |
| Tie-breaker (when A and B differ on class, pattern or tag set; blind to both) | `claude-opus-5-5` | `medium` |
| Same-defect reader | `claude-sonnet-5-5` | `medium` |

**Consensus** follows ledger rulings R12, R16 and R17. Labels are conditioned on the final class:
- **Class and pattern:** majority vote.
- **Tags:** a tag is kept when at least two class-matching readers chose it. When exactly one reader's class matches the final class, that reader's tags are used.
- **Disagreement:** when two or more class-matching readers share no tag, the finding gets no tags and is flagged `capability_split`.
- **Single reader:** a finding with only one valid reader carries that reader's label, flagged `low_confidence`.
- **Same-defect checks:** these run only for findings whose final class is `in_class` or `adjacent`.

**Transport (amended 2026-11-19, R19).** Labeling runs through Claude Code CLI `2.1.322` in print mode on the operator's Claude subscription. The `anthropic` SDK path (0.87.0 installed) is present in `llm.py` but unused.
- **Invocation.** Every call runs from a fresh empty temporary directory, with the user text on stdin:

  ```
  claude -p --model <id> --effort <level> --system-prompt-file <prompt> --json-schema <schema>
    --output-format json --tools "" --no-session-persistence --strict-mcp-config
    --setting-sources "" --disable-slash-commands --exclude-dynamic-system-prompt-sections
  ```

- **Answers and served model.** The answer is `structured_output`. The served model is the set of `modelUsage` keys. If those keys differ from the requested ID, the call is flagged `fallback_ran` and counted.
- **No server-side fallbacks.** A refusal is recorded as `refused`, and the tie-breaker reads instead.
- **Halting.** A usage limit or transport error halts the run without caching that call. Every answered call is cached, keyed by model, finding, prompt key and a hash of the input. A re-run therefore resumes and never re-asks.

**Preflight (2026-11-19).** One schema-constrained call went to each model. Each `modelUsage` named exactly the requested ID:

```json
{"cli_version": "2.1.322 (Claude Code)",
 "models": {"claude-sonnet-5-5": "claude-sonnet-5-5",
            "claude-haiku-5-5": "claude-haiku-5-5",
            "claude-opus-5-5": "claude-opus-5-5"}}
```

**Reader context (disclosed).** Probes showed that `--setting-sources ""` stops the user's hooks, memory files, plugins, skills and MCP servers from loading. Context tokens dropped from 3,441 to 1,395, and hook time dropped from 16.5 s to 2 ms. What remains is the following:
- the CLI's fixed agent-SDK preamble;
- the `StructuredOutput` tool;
- environment information (temporary working directory, OS, scratchpad path, a rule for untrusted downloads, model ID, date);
- the account email.

None of it mentions mipcode, Sherlock, the analyzer or any contest. Both readers receive the same context.

## Hits

- **Candidates.** An analyzer finding in a production file is a candidate for a judged finding when it lands in one of two places: a file that finding cites as a site, or the file holding its final `obligation_site`. Closeness is tagged in this order:
  - `strict`: the reported line or end line is within ±3 lines of a site;
  - `function`: inside the same function or modifier body;
  - `file`: anywhere else in that file.
- **Same-defect check.** Every candidate goes to the same-defect reader, which answers `same_defect`, `related` or `unrelated`. Only `same_defect` is a hit.
  - **Strict recall:** strict candidates confirmed `same_defect`.
  - **Full recall:** candidates of any closeness confirmed `same_defect`.
- **Human spot-check.** After the model check, the operator reads every `same_defect` hit against the finding and the source. They record `agree` or `disagree` with a one-line reason.
  - A hit they disagree with leaves headline recall (`full_human`).
  - The operator never adds a hit the model rejected.
  - Model-confirmed and human-confirmed recall are both reported, with every disagreement.

## Measures and thresholds

- **Headline 1: class coverage.** The shares of `in_class`, `adjacent` and `out_of_class` among all H/M findings in the measurement set, split by severity. This measure is descriptive and has no threshold.
- **Headline 2: miss-bucket ranking.** Among in-class and adjacent findings with no `same_defect` hit, the count of each `would_catch_with` tag, split High/Medium.
  - A finding with several tags counts once under each of them.
  - The count where the tag is the finding's only tag is also given.
- **Decision rule.** The next build is the top bucket by High+Medium count, excluding `none_of_these`.
  - If the top two buckets differ by fewer than 3 findings, the report says so and recommends whichever needs no new parser.
  - Before the run, sibling-guard asymmetry and fork-upstream diffing are expected to lead. A different leader is a finding in its own right.
- **Secondary: in-class snapshot recall.** The number of in-class findings with at least one `same_defect` hit, divided by the number of in-class findings with at least one production site. It is reported as full recall (headline basis: human-confirmed) with a Wilson 95% interval, alongside strict recall.

| In-class recall | Reading |
| --- | --- |
| ≥ 40% | The snapshot analyzer reaches a meaningful share of paid in-class bugs. Expansion should add patterns. |
| 15–40% | Partial. The report names the missed patterns that account for the gap. |
| < 15% | The snapshot analyzer doesn't reach paid in-class bugs. A quiet result on contest code carries little information. |

When the interval straddles a boundary, the outcome is reported as inconclusive between the two readings, with the point estimate. There is no top-up draw.

These thresholds apply only to the secondary measure, which is read together with class coverage.

- **Other secondary measures:**
  - recall for `adjacent`;
  - recall by pattern;
  - analyzer findings per contest;
  - reader agreement (raw agreement and Cohen's κ on class and pattern, mean Jaccard on tags);
  - refusal, invalid and `fallback_ran` counts by model;
  - counts of `low_confidence`, `capability_split` and unlabeled findings;
  - wall time.

  Wall time is the analyzer subprocess time only.
- **Separation of sets.** Dev numbers are reported separately and never pooled.

## Dev-set run (harness check, not a result)

All three stages exited 0:
- **Analyzer.** It reported production-file findings per contest: superfluid 33 (60 in all), alchemix 9, new-scope 3, perennial 5.
- **Superfluid clone.** It first failed on Windows path length. The harness fix (`core.longpaths`, no credential prompts, subprocess timeouts with a process-tree kill) changed the clone step only.
- **Calls.** 120 reader calls, with no refusals, no invalid outputs and no fallbacks.
- **Class split.** 10 in-class, 7 adjacent and 31 out-of-class.
- **Agreement.** Class κ was 0.53 (raw 0.77), pattern κ 0.93, and mean tag Jaccard 0.75.
- **Hits.** 10 candidates went to the same-defect check, and all were judged `unrelated`. In-class recall was 0/10, and adjacent recall 0/6.
- **Obligation sites.** Two could not be resolved, because they point at Morpho code outside the clone. They are recorded as label errors.

## Cost projection

The dev set used a notional $1.806 of CLI-reported `total_cost_usd` for 48 findings, about $0.038 per finding. At list prices on recorded tokens, the same run comes to $1.40. Scaled to 156 measurement findings, the projection is about $5.90 notional, against the $15 cap. Same-defect checks scale with candidates, not findings, so they add to this. The projection is under the cap, so the Haiku-swap clause does not apply.

On the subscription, this cost is drawn from usage limits, not billed. The cap stays as a brake. If the run reaches it, it stops and reports partial results.

## Running it

From the repo root, with `PYTHONPATH="src;experiments/sherlock_recall"`:

```
python experiments/sherlock_recall/run_recall.py --set measurement --engine-commit 3ebdabd --out results/sherlock_recall/
python experiments/sherlock_recall/label.py --set measurement --out results/sherlock_recall/
# operator spot-check of every same_defect hit
python experiments/sherlock_recall/score.py --set measurement
```

## Known limits

- **Snapshot only.** The analyzer never sees the change that introduced a bug, so this measures what the audit mode reaches, not what change review would catch.
- **Model labels.** Class, pattern and capability tags come from model readers. Agreement is reported, but no human labeled the class rubric. Readers share the CLI's generic preamble.
- **Lenient same-defect criterion.** Peer-side hits count, which can inflate recall. The human spot-check bounds that inflation.
- **Regex parser.** The engine's Solidity parser is regex-based. Findings that need inheritance, libraries under `lib/`, or other repositories are expected misses, and the `cross_file_or_dependency_visibility` bucket counts them.
- **Small in-class sample.** The recall interval will be wide.
- **Sherlock only.** The draw uses Sherlock-judged findings from 2024–2025. Other platforms' judging standards may differ.
- **Windows stdin.** On Windows the CLI transport writes stdin in text mode, so readers see CRLF line endings. Every call is affected the same way.
