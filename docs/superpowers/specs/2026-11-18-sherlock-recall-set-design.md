# Sherlock recall set for the Solidity analyzer: design

Date: 2026-11-18. Status: draft for approval.

## Why

The Solidity analyzer (`src/mipdelta/solidity.py`, `mip-delta --audit-solidity`) has only been shown to be *quiet*: 7 of 8 holdout repositories produced no findings. Quiet doesn't mean it catches anything. No one has measured whether it catches the bugs bounty programs actually pay for. On the wall of fame (coherion-web `mipcode-ledger.json`), 8 of the 10 Solidity entries are `state_sink_review`: unused constants or state variables, which programs usually class as informational.

This study answers three questions that decide where Solidity work goes next:

1. **Class coverage.** What share of paid High and Medium findings are the kind of bug mipcode looks for: a contract or change obligation that one site meets and a related site doesn't?
2. **In-class snapshot recall.** For those findings, does the frozen analyzer, run on the audited commit, report the judged site?
3. **Misses by bucket.** For each in-class or adjacent finding it misses, what capability would have caught it? This ranked list, not intuition, picks the single next build.

**Expectations, stated before the run.** Most paid Sherlock High and Medium findings are expected to be economic, math, rounding, oracle or multi-step state-machine bugs. No structural checker should claim those. Overall recall is therefore expected to be low, and that alone is not a failure. **The headline results are class coverage (question 1) and the miss-bucket ranking (question 3).** In-class recall (question 2) is reported with thresholds but is secondary.

**This measures snapshot recall only.** The analyzer runs on the static audited tree. That is the only sensible Solidity setup: diff mode (`analyze_solidity_delta`, `solidity.py:1120`) has no detectors of its own. It runs the same snapshot audit on the before and after trees and keeps only new findings. So a pass that replays the fix in reverse can't report anything the snapshot run misses, and is deliberately left out. Its `added_guards` delta is computed but never turned into a finding, which matters for the sibling-guard bucket below.

It follows the Python held-out protocol (`experiments/held_out/README.md`): freeze the plan, then run once and report what comes out.

## Goals

- A versioned, reproducible recall set built from Sherlock's public judging repos. It covers EVM/Solidity contests only, with every valid High and Medium in the sampled contests.
- A plan committed before any measurement. It fixes the engine commit, contest list, sampling seed, labeling rubric, hit rule and outcome thresholds.
- Blind class labels for every finding, from two independent model readers plus a third to settle disagreements, with agreement rates and raw labels published.
- Class coverage and a ranked table of misses by capability bucket (the headline), plus strict and full in-class recall (secondary).
- A results README in the house style of `results/held_out/` and `results/solidity_bounty/`.

## Non-goals

- Changing any detector during the study. A change prompted by these results needs a new held-out contest set before it can be called an improvement.
- Testing the diff mode (`--before/--after`, `--check-git`). This study tests the snapshot path (`--audit-solidity`) only, and the reason is given under Why. Fork-vs-upstream diffs are a separate later study, and the `fork_upstream_diff` bucket measures how much they would matter.
- Code4rena, Cantina, Immunefi, or exploit postmortems. Code4rena is a possible second round.
- Precision. Sherlock reports list only High and Medium, so an analyzer finding that matches no H/M site is not evidence of a false report. Total finding counts are reported as noise context only.
- Publishing to coherion-web. A new real defect found on the way goes through the normal verdict process. It is not added automatically.

## Anti-goals (the study fails even if it runs)

- Looking at analyzer output on the measurement contests before the plan is committed, or adjusting the rubric, hit rule or thresholds after seeing it.
- Counting a finding as a hit only because it lands in the same function as a judged site, when it describes an unrelated defect.
- Readers labeling class membership while they can see the analyzer's output.

## Approaches considered

| Approach | Trade-off | Decision |
| --- | --- | --- |
| **A. Sherlock judged findings** | Several hundred paid, severity-judged bugs, each pinned to a file, line and commit, in a uniform markdown format. Tests snapshot mode only. Labeling costs model calls. | **Chosen** by the user. |
| B. Curated famous exploits (about 25) | High stakes and good for storytelling, but hand-picked, many have no source to scan, and the sample is too small for a recall percentage. | Rejected as the measurement. Use the best cases as illustrations afterward. |
| C. Sherlock and Code4rena combined | About twice the sample, but Code4rena findings are free-form issue text with heavy duplication, so the ground truth is noisier. | Deferred to a possible round 2. |

## Data source (verified 2026-11-18)

- Judging repos are `sherlock-audit/<YYYY-MM>-<name>-judging`. Their `README.md` is the final report, with sections `# Issue H-<n>: <title>` and `# Issue M-<n>: <title>`, each with a `Source:` issue URL. The body uses one of two formats, and some contests mix them (builder check, confirmed on `2024-08-cork-protocol`, `2024-11-oku`, `2025-07-cap`):
  - **old:** `## Summary`, `## Vulnerability Detail`, `## Impact`, `## Code Snippet`, `## Tool used`, `## Recommendation`;
  - **new:** `### Summary`, `### Root Cause`, `### Internal Pre-conditions`, `### External Pre-conditions`, `### Attack Path`, `### Impact`, `### PoC`, `### Mitigation`, then `## Discussion`. Permalinks appear inline in Root Cause.
- Permalinks look like `https://github.com/sherlock-audit/<YYYY-MM>-<name>/blob/<sha-or-main>/<path>#L<a>[-L<b>]`. Some use `main` instead of a commit SHA.
- The audited source is the companion repo `sherlock-audit/<YYYY-MM>-<name>` (for example `2024-08-cork-protocol`, default branch `main`).
- The machine already has an authenticated `gh` CLI. The core API rate limit is 5000/h.

## Design

### 1. Contest selection (frozen in the plan)

- **Candidates:** every `sherlock-audit/*-judging` repo whose contest month is from 2024-01 to 2025-12 *(builder: set the window to whatever has complete reports; record it)*, whose `README.md` contains at least one `# Issue H-` or `# Issue M-`, and whose companion source repo can be cloned.
- **EVM/Solidity only:** at least 80% of the *site links* (defined in §2) point at `.sol` files. Rust, Move, Cairo and Go contests are excluded and their count is recorded, because that count is itself evidence for the "other languages" question.
- **Exclusions:** a contest is excluded when the code in scope is the *own code* of a protocol already in a mipcode corpus: the 12-target contest corpus, the 8-repo holdout, and the curated suite (`experiments/solidity_bounty/*.py`). That covers Twyne, Veda, Lombard, Strata, Origin, TermMax, dHEDGE, OpenZeppelin, Aqua, Uniswap, Morpho, Permit2, Solmate, 1inch and Sablier. Integrating with, vendoring or forking one of them does not exclude a contest. *(My call.)* So `2024-10-morpho-x-index`, which is Index Coop code integrating Morpho, is **eligible**. The fork case is exactly what the `fork_upstream_diff` bucket measures.
- **Sample:** list the eligible candidates sorted by repo name, then draw 20 with `random.Random(20261118)`. The first 4 drawn are the **dev set**. The harness (parsing, cloning, hit matching, reader prompts) is debugged on them, and their analyzer output may be looked at. The other 16 are the **measurement set** and are run once, after the plan is committed.
- The candidate list, eligibility results and the draw go into the plan before anything is cloned for the measurement set.

### 2. Ground truth: findings and sites

- **Unit = one finding:** one `# Issue H-n` / `M-n` section of the final report. Duplicates are already merged by Sherlock.
- **Sites:** each permalink into the companion repo that points at a `.sol` file **and appears in the section that locates the bug** becomes `(path, line_start, line_end)`. That section is `## Code Snippet` (old format) or `### Root Cause` (new format). If an old-format issue has no Code Snippet, its Vulnerability Detail is used. Links in PoC, Mitigation, Recommendation and Discussion are **not** sites, because they point at tests, fixes and other protocols. *(My call.)* A link with no `#L` anchor is kept as a file-level site, which can only produce candidate hits (see §4). A permalink on `main` resolves to the companion repo's commit at contest close *(builder: decide how, e.g. the last commit before the judging repo's first issue; record the rule)*. Snippet links to external repos (dependencies, upstream protocols) are kept but marked `external` and don't count toward recall.
- A finding with no resolvable `.sol` site is kept for class coverage but excluded from recall. Its count is reported.
- **Production files:** recall counts only sites in production files. A production file is not under `test/`, `tests/`, `script/`, `scripts/`, `mock/`, `mocks/`, `lib/` or `node_modules/`, and is not named `*.t.sol` or `*.s.sol`.

### 3. Class labels (blind)

Readers see the finding's title, plus Summary, Vulnerability Detail, Impact and Recommendation (old format) or Summary, Root Cause, Attack Path, Impact and Mitigation (new format). They also see the cited code: each site's lines with ±20 lines of context. They never see PoC code, Discussion, or analyzer output. *(My call: Mitigation stays in because it often names the peer site that already does it right, which is the `obligation_site`.)* The reader prompt carries this instruction verbatim:

> Classify the root-cause defect described in the Summary and Root Cause (or Vulnerability Detail). Use the Mitigation or Recommendation section only to locate a peer or base site that already handles this correctly. Ignore extra hardening suggestions there that are unrelated to the root cause; they do not change the class, pattern, or capabilities.

Each reader assigns:

- `class`:
  - `in_class`: the defect is a consistency obligation that one site meets and a related site doesn't. Examples: a parameter accepted but not forwarded or not read; a sibling entry point missing a guard, modifier or check that its peers apply; an override or interface implementation whose signature, modifiers or behavior no longer matches its base; state written but never read where a read was intended; paired operations that disagree (round down on mint but not up on redeem, units scaled on one path only); a value populated on one branch but zero on another before use.
  - `adjacent`: a consistency or omission defect, but one that needs semantics mipcode doesn't model, such as a hardcoded `0` minimum output or `block.timestamp` as the deadline, or a missing check that has no sibling to compare against.
  - `out_of_class`: a logic, math, economic, oracle, access-design, or external-integration defect with no omission or consistency structure.
- `pattern`: one tag from a fixed list, for `in_class` and `adjacent` only: `param_not_forwarded`, `param_unread`, `sibling_missing_guard`, `override_mismatch`, `dead_state`, `paired_op_mismatch`, `branch_zero_value`, `hardcoded_safety_param`, `other` *(builder: may add tags on the dev set only, then freeze)*.
- `obligation_site`: for `in_class`, the site that already meets the obligation (the peer or base), if the finding names one.
- `would_catch_with`: for `in_class` and `adjacent` only. The capabilities that would let a structural checker report this finding, multi-select from a fixed list:
  - `sibling_guard_asymmetry`: compare guards, modifiers and checks across sibling entry points in the same contract or family;
  - `safety_param_weighting`: recognize safety-critical parameters (`minAmountOut`, `deadline`, `recipient`, `nonce`, `chainId`, slippage bounds) as dropped, unread, hardcoded, or set to `0` / `block.timestamp`;
  - `cross_file_or_dependency_visibility`: needs code under `lib/` or another repo (an inherited base, a library, an external protocol interface);
  - `fork_upstream_diff`: the finding text names an upstream the code was forked or adapted from, and the defect is a divergence from it;
  - `existing_detector_gap`: one of today's detectors (`parameter_sink_review`, `delegating_wrapper_review`, `override_modifier_review`, `state_sink_review`, `try_catch_sink_review`) covers this shape but failed to fire;
  - `none_of_these`.

  Readers tag this blind, before analyzer output exists. A miss is then credited to its tags afterward.

**Readers and independence** *(my call)*. Two readers per finding, using **different models** so that agreement means something: reader A is `claude-sonnet-5-5` and reader B is `claude-haiku-5-5`. The prompt and rubric are identical. A third, blind reader (`claude-opus-5-5`) is invoked whenever A and B disagree on `class`, `pattern`, or the `would_catch_with` set. It doesn't see their labels.

**Model IDs and request shape** (checked 2026-11-18 against Anthropic's current model table, cached 2026-11-03):
- The exact IDs are `claude-sonnet-5-5`, `claude-haiku-5-5` and `claude-opus-5-5`, with no date suffix.
- **Live preflight gate:** before the plan is committed, `label.py --preflight` calls the Models API (`models.retrieve`) for each ID and records the returned `id` and `display_name` in the plan. If any ID doesn't resolve, the plan can't be frozen. There was no API key on the build machine at design time, so this hasn't run yet.
- **No sampling parameters.** The current API rejects `temperature`, `top_p` and `top_k`, so independence comes only from using different models.
- **JSON output through structured outputs** (`output_config.format` with a JSON schema), not a forced tool call. Forced `tool_choice` returns 400 on Sonnet 5.5 and Opus 5.5.
- **Explicit effort.** Thinking can't be turned off on these models. Readers A and B run at effort `low`; the tie-breaker and same-defect reader run at `medium`. Thinking tokens are billed and are included in the cost estimate.
- **Refusals.** Vulnerability write-ups may trip the `cyber` safety classifier.
  - Sonnet and Opus calls send server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). Every label records the model that actually answered (`response.model`) and whether a fallback ran. Labels a fallback answered are counted and reported, since they weaken reader independence.
  - Haiku 5.5 has no server-side fallback.
  - A refusal nothing rescues is recorded as `refused`. That reader's label is then missing, and the tie-breaker reads instead. Refusal counts by model and category are reported.
- **Final labels:** `class` and `pattern` take the majority of the three. A `would_catch_with` tag is kept if at least two readers chose it (both, when only two read).
- **Reporting:** raw agreement and Cohen's κ for `class` and `pattern`, plus Jaccard overlap for `would_catch_with`, with all raw labels.
- **Transport** *(my call)*: `label.py` uses the official `anthropic` Python SDK as an **experiment-only** dependency. It is installed in the dev environment and **not** added to the package's runtime dependencies, so `mip-delta` stays zero-dependency. It doesn't reuse `review.py`, which is frozen with the engine and hardwired to the Stage-2 prompt, schema and `max_tokens`. The SDK installed now (0.87.0) may be older than `output_config` and `fallbacks`, so the builder upgrades it and records the version used. Credentials come from `ANTHROPIC_API_KEY` or an `ant auth login` profile. The `claude -p` CLI fallback is not used, because it can't pin the model.
- **Amendment 2026-11-19 (pre-freeze; user decision, ledger Ruling R19). This supersedes the Transport, Refusals and preflight lines above wherever they conflict.**
  - **Transport.** The default transport is the Claude Code CLI in print mode, billed against the user's Claude subscription, so no API key is needed. Upgrading the SDK to 2.10.0 broke other tools in the global Python, and it was reverted. The SDK transport stays in the code as an option but is not used for the measurement.
  - **Invocation.** Each call runs `claude -p` from an empty temporary directory with these flags:
    - `--model <id>` and `--effort <level>`
    - `--system-prompt-file <prompt>`
    - `--json-schema <schema>`
    - `--output-format json`
    - `--tools ""`
    - `--no-session-persistence`
    - `--strict-mcp-config`
    - `--setting-sources ""`
    - `--disable-slash-commands`
    - `--exclude-dynamic-system-prompt-sections`

    The user text goes on stdin. The answer is read from `structured_output`. The served model is read from the `modelUsage` keys, and the CLI binary's version is recorded.
  - **Model pinning.** `--model` pins the model, so the earlier objection no longer holds. A probe on 2026-11-19 returned `modelUsage` keyed by exactly the requested ID.
  - **Reader context (disclosed).** `--bare` accepts only an API key, so subscription calls can't use it. Probes on 2026-11-19 checked what a reader receives with `--setting-sources ""`. No hooks, memory files, plugins, skills or MCP servers load. The context still holds:
    - the CLI's fixed agent-SDK preamble (independent tool calls, deferred tools);
    - the `StructuredOutput` tool that the schema adds;
    - environment information (working directory, OS, scratchpad path, a rule for untrusted downloads, model ID, date);
    - the account email.

    None of this mentions mipcode, the analyzer, or any contest. The frozen README records the probe output.
  - **Fallbacks.** The CLI has no server-side `fallbacks`. A refusal is recorded as `refused`, and the tie-breaker reads instead, as the Haiku rule above already provides. `fallback_ran` is always false, and refusal counts are still reported.
  - **Cost and limits.** Calls are paid from subscription usage limits, not billed dollars. The $15 cap stays as a notional brake on the CLI's reported `total_cost_usd`. Reaching a usage limit, or any CLI transport error, halts the run cleanly without caching that call. Because every answered call is cached, re-running resumes where the run stopped.
  - **Preflight.** `llm.py --preflight` makes one tiny schema-constrained call per model and checks that `modelUsage` names exactly that ID. This replaces `models.retrieve`.

**Cost cap:** $15 for all labeling and same-defect checks, frozen in the plan.
- **Rough estimate** at current list prices (per million tokens, input/output: Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50, Opus 5.5 $4/$20), assuming about 300 findings, about 6k input tokens per read, and about 1.3k output tokens including thinking at `low`:

| Item | Estimate |
| --- | ---: |
| Reader A (Sonnet) | ~$7.50 |
| Reader B (Haiku) | ~$0.40 |
| Tie-breaker (Opus) on about 30% of findings | ~$3 |
| Same-defect checks | under $1 |
| **Total** | **about $12** |

- *(Builder: replace this with a measured estimate from the dev set, counting tokens with `count_tokens` and actual output tokens, and record it before the plan is committed. If the projection exceeds the cap, swap reader A to a second Haiku call with a reordered rubric, and record the swap before the plan is committed.)*
- The run stops and reports partial results if the cap is reached.

### 4. Running the analyzer

- **Engine:** `src/mipdelta` frozen at the commit the plan names (currently `3ebdabd`, v0.6.4). Default detector set.
- **Per contest:** shallow clone of the companion repo at the resolved commit, then `mip-delta --audit-solidity --root <clone> --json`. Store the raw JSON per contest.
- **Candidate hits** *(my call)*. Historically the analyzer reports only a handful of findings per repository, so every analyzer finding in a production file is a candidate for a finding when it lands in:
  - any file that finding cites as a site, or
  - the file holding its final `obligation_site`.

  mipcode often reports the peer, base or declaration side of a mismatch, not the line the auditor cited, and this catches that. Each candidate is also tagged with how close it is:
  - **strict:** the reported line or `end_line` is within ±3 lines of a site's line range (this includes declaration-line findings such as `state_sink_review`);
  - **function:** the reported line is inside the same function or modifier body as a site (bodies come from `solidity.parse_solidity_file`);
  - **file:** in a cited or obligation file, but neither strict nor function.
- **Same-defect check:** every candidate, whatever its closeness, goes to one blind model reader (`claude-sonnet-5-5`). The reader sees the finding text, the analyzer message, and both code locations, and answers `same_defect` / `related` / `unrelated`. Only `same_defect` counts as a hit.
- **Human spot-check of every confirmed hit.** Candidate hits are expected to be few: the analyzer historically fires only a handful of times per repository. So after the model check, the operator reads **every** `same_defect` hit against the finding and the source, and records `agree` or `disagree` with a one-line reason.
  - A hit the operator disagrees with is removed from headline recall.
  - Both numbers are reported: model-confirmed and human-confirmed, along with every disagreement.
  - The operator never adds a hit the model rejected, so the check can only make recall stricter.
- **The two named recall strictnesses:**
  - **strict recall:** strict candidates confirmed `same_defect`;
  - **full recall:** candidates of any closeness confirmed `same_defect`. This is the secondary measure's main number.

  Counts of `related` and `unrelated` are reported at each closeness level.

### 5. Measures and thresholds (frozen in the plan)

- **Headline 1, class coverage:** shares of `in_class`, `adjacent` and `out_of_class` among all valid H/M on the measurement set, split by severity. No threshold; descriptive.
- **Headline 2, miss-bucket ranking:** for in-class and adjacent findings with no `same_defect` hit, counts per `would_catch_with` tag, split High/Medium. A finding with several tags counts once in each. The report also gives the count under each tag alone, with no other tag.
- **Decision rule (frozen):** the next build is the top `would_catch_with` bucket by High+Medium count, excluding `none_of_these`. If the top two differ by fewer than 3 findings, the report says so and recommends whichever needs no new parser. Before the run, sibling-guard asymmetry and fork-upstream diffing are expected to lead. A different leader is a finding in its own right.
- **Secondary, in-class snapshot recall:** in-class findings with at least one `same_defect` hit ÷ in-class findings with at least one production site. Reported as full recall with a Wilson 95% interval, plus strict recall.
- **Small samples** *(my call)*. 16 contests may give only about 15–40 in-class findings, so the interval will be wide. There is no top-up draw. If the interval straddles an outcome boundary, the outcome is reported as **inconclusive between the two readings**, and the point estimate is still given. The headline measures use all H/M findings (expected to be several hundred), so they aren't affected.
- **Other secondary measures:**
  - recall for `adjacent`;
  - recall by pattern;
  - analyzer findings per contest and per 1,000 production lines;
  - wall time.
- **Outcomes:**

| In-class recall | Reading |
| --- | --- |
| ≥ 40% | The snapshot analyzer reaches a meaningful share of paid in-class bugs. Expansion should add patterns. |
| 15–40% | Partial. The report names the missed patterns that account for the gap. |
| < 15% | The snapshot analyzer doesn't reach paid in-class bugs. A quiet result on contest code carries little information. |

  These thresholds apply to the secondary recall measure only. A low result is expected and is read together with class coverage. For example, 10% recall on a class that is 5% of paid findings says little, while 10% recall on a class that is 25% of paid findings is a strong signal to build.

- Dev-set numbers are reported separately and never pooled into the headline.

### 6. Deliverables and layout

```
experiments/sherlock_recall/
  README.md          # the frozen plan (committed before the measurement run)
  build_set.py       # list + filter + draw contests; parse reports; resolve sites
  run_recall.py      # clone, run mip-delta, match hits
  label.py           # blind class labels + same-defect checks, with cost cap
  prompts/           # reader prompts, frozen
results/sherlock_recall/
  README.md          # results, house style
  contests.json      # candidate list, eligibility, draw
  recall_set.json    # findings, sites, resolved commits
  labels.json        # raw reader labels + resolutions
  hits.json          # analyzer output per contest + matches
  summary.json       # measures
```

All scripts are standard-library only, matching the repo's zero-dependency stance. They call `gh` and `git` through `subprocess`, and cache clones and API responses under a gitignored directory. Unit tests for report parsing, permalink resolution and hit matching go in `tests/` using recorded fixtures, with no network.

## Constraints

- Stdlib-only Python 3.10+, matching the repo.
- Network access is only through `gh`/`git`. Any downloaded source is untrusted data: clone it into its own cache directory, never execute it, and never run build tools inside it (no `forge build`).
- The plan commit comes before any measurement-set analyzer run. Git history is the proof.

## Left to the builder

The items marked *(builder: …)* above, plus: how API responses and clones are cached, the exact reader prompt wording (it must carry the rubric in §3 verbatim), how the results README is laid out, and the handling of rare report-format variants seen on the dev set (record each one).
