# Sherlock Recall Set Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a frozen, pre-registered study that measures how much of Sherlock's paid High/Medium Solidity findings fall in mipcode's class, which capabilities would catch the misses, and the snapshot recall of `mip-delta --audit-solidity`.

**Architecture:** The study is a set of focused scripts under `experiments/sherlock_recall/`. Pure modules (report parsing, contest selection, hit matching, consensus, scoring) are unit-tested offline against fixtures. Thin I/O shells handle the network and model work: `gh`/`git` subprocess calls, the `mip-delta` CLI subprocess, and an Anthropic SDK wrapper with a cost ledger. Output goes as JSON to `results/sherlock_recall/`. The frozen plan is `experiments/sherlock_recall/README.md`, committed before the measurement run.

**Tech Stack:** Python ≥3.10, standard library, `gh` and `git` CLIs, the `mip-delta` CLI (frozen engine), the `anthropic` Python SDK (experiment-only), pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-sherlock-recall-set-design.md`

## Global Constraints

- **Branch:** all work happens on `feat/sherlock-recall`, cut from `main`. Never commit to `main`.
- **Frozen engine:** no edits under `src/mipdelta/` on this branch. The engine commit the study names is `git rev-parse --short HEAD -- src/mipdelta` at the start (currently `3ebdabd`).
- **Dependencies:** standard library only, except the `anthropic` SDK, which is imported only in `experiments/sherlock_recall/llm.py`. Never add it to `pyproject.toml`'s runtime dependencies. Record the installed SDK version in the plan README.
- **Untrusted downloads:** clones and API responses are cached under `.mipdelta/sherlock_cache/` (already gitignored). Never execute, build or install anything inside a clone: no `forge`, `npm`, `make`, or Python with that directory as the working directory.
- **No network in tests.** Tests use fixtures under `tests/fixtures/sherlock/` and injected fakes.
- **Results are force-added:** `results/` is gitignored, so commit results with `git add -f`, as earlier studies did.
- **Seed** `20261118`. **Draw** 20 contests; the first 4 drawn are the dev set and the other 16 are the measurement set. **Contest window:** 2024-01 to 2025-12, inclusive, by the `YYYY-MM` repo prefix.
- **Models:** reader A `claude-sonnet-5-5` at effort `low`; reader B `claude-haiku-5-5` at effort `low`; tie-breaker `claude-opus-5-5` at effort `medium`; same-defect reader `claude-sonnet-5-5` at effort `medium`. No `temperature`/`top_p`/`top_k`. JSON comes through `output_config.format`, never forced `tool_choice`. Sonnet and Opus calls send `fallbacks: "default"` with beta `server-side-fallback-2026-07-01`.
- **Transport amendment (2026-11-19, Ruling R19):** labeling runs through `claude -p` on the user's subscription, using the flags in spec §3 "Amendment 2026-11-19". There are no server-side fallbacks, so `fallback_ran` is always false. The $15 cap is notional and applies to the CLI's `total_cost_usd`. A usage limit or a transport error halts the run without caching that call. Preflight makes one tiny call per model and checks `modelUsage`. The SDK transport stays as an option but is unused. Task 8b adds this transport.
- **Cost cap:** $15.00 total for all model calls. Pricing per million tokens (input/output): Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50, Opus 5.5 $4/$20.
- **Production `.sol` file:** ends in `.sol`, not `.t.sol` or `.s.sol`, and no path segment (case-insensitive) is `test`, `tests`, `script`, `scripts`, `mock`, `mocks`, `lib` or `node_modules`.
- **Hit closeness:** `strict` means within ±3 lines of a site range; `function` means the same function or modifier body (from `mipdelta.solidity.parse_solidity_file`); `file` means anywhere else in a cited file or the obligation file.
- **Taxonomies** (copy exactly):
  - `class` ∈ {`in_class`, `adjacent`, `out_of_class`};
  - `pattern` ∈ {`param_not_forwarded`, `param_unread`, `sibling_missing_guard`, `override_mismatch`, `dead_state`, `paired_op_mismatch`, `branch_zero_value`, `hardcoded_safety_param`, `other`};
  - `would_catch_with` ⊆ {`sibling_guard_asymmetry`, `safety_param_weighting`, `cross_file_or_dependency_visibility`, `fork_upstream_diff`, `existing_detector_gap`, `none_of_these`};
  - same-defect verdict ∈ {`same_defect`, `related`, `unrelated`}.
- **Excluded protocols** (excluded only when it's the protocol's own code): twyne, veda, lombard, strata, origin, termmax, dhedge, openzeppelin, aqua, uniswap, morpho, permit2, solmate, 1inch, sablier. `2024-10-morpho-x-index` is **eligible**.

## Review Focus

1. **Markdown-wrapped permalinks.** Links in `[text](url)`, `<url>`, or followed by `)`, `.`, `,` or a backtick must parse to a clean URL. Also accept `#L10-L20`, `#L10`, no anchor, and URL-encoded paths. Test owner: Task 1.
2. **One issue with links in several sections.** Only Code Snippet or Root Cause links become sites. A Root Cause that only links to an external repo yields zero companion sites, and the finding is still kept for class coverage. Test owner: Task 1.
3. **Missing, private or renamed companion repo, or a judging README with no H/M.** The contest is recorded as ineligible with a reason, and the build continues. Test owner: Task 2.
4. **Analyzer paths that differ from site paths.** These include backslashes, `./` prefixes, or the clone root nested one directory deep. They must normalize to the same POSIX path relative to the repo root before matching. Test owner: Task 3.
5. **Model call failures.** A refusal, malformed JSON, or `max_tokens` stop is recorded (`refused`/`invalid`) and never crashes the run, and its tokens still count toward the cap. Hitting the cap mid-run stops cleanly. A rerun resumes from cached responses without paying for them again. Test owner: Task 5.

---

### Task 1: Report parser

**Files:**
- Create: `experiments/sherlock_recall/sherlock_report.py`
- Create: `tests/fixtures/sherlock/old_format.md`, `tests/fixtures/sherlock/new_format.md`, `tests/fixtures/sherlock/mixed_format.md`
- Test: `tests/test_sherlock_report.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Site: repo: str; ref: str; path: str; line_start: int | None; line_end: int | None; external: bool`. `repo` is `owner/name`, `ref` is a SHA or branch name, and `path` is a POSIX path relative to the repo root.
  - `@dataclass(frozen=True) class JudgedFinding: finding_id: str; severity: str; number: int; title: str; source_url: str; fmt: str; sections: dict[str, str]; sites: tuple[Site, ...]`. `finding_id` is `"<contest>:H-1"`; `severity` is `"H"` or `"M"`; `fmt` is `"old"` or `"new"`.
  - `parse_permalink(url: str, companion_repo: str) -> Site | None`, which returns `None` for links that aren't GitHub blob links.
  - `parse_report(markdown: str, contest: str, companion_repo: str) -> list[JudgedFinding]`.
  - `reader_sections(f: JudgedFinding) -> dict[str, str]`, which returns only the sections readers may see (spec §3).

Fixtures are short hand-written excerpts modeled on the formats in the spec's Data source section: 2 issues each, with permalinks in every section type.

- [ ] **Step 1: Create the branch**

Run: `git switch -c feat/sherlock-recall`
Expected: `Switched to a new branch 'feat/sherlock-recall'`

- [ ] **Step 2: Write the failing tests**

```python
def test_parse_permalink_variants():
    s = parse_permalink("https://github.com/sherlock-audit/2024-08-x/blob/db23bf6/src/A.sol#L10-L20", "sherlock-audit/2024-08-x")
    assert (s.path, s.line_start, s.line_end, s.ref, s.external) == ("src/A.sol", 10, 20, "db23bf6", False)
    assert parse_permalink("https://github.com/sherlock-audit/2024-08-x/blob/main/src/A.sol#L7", "sherlock-audit/2024-08-x").line_end == 7
    assert parse_permalink("https://github.com/sherlock-audit/2024-08-x/blob/main/src/A.sol", "sherlock-audit/2024-08-x").line_start is None
    assert parse_permalink("https://github.com/Uniswap/v3-core/blob/main/x.sol#L1", "sherlock-audit/2024-08-x").external is True
    assert parse_permalink("https://example.com/foo", "sherlock-audit/2024-08-x") is None

def test_markdown_wrapped_links_are_cleaned():  # Review Focus 1
    md = "see [here](https://github.com/sherlock-audit/2024-08-x/blob/abc/src/My%20A.sol#L3)."
    ...  # parse_report on a one-issue doc containing md in Root Cause
    assert sites[0].path == "src/My A.sol" and sites[0].line_start == 3

def test_old_format_sites_from_code_snippet_only():
    fs = parse_report(OLD, "2024-08-x", "sherlock-audit/2024-08-x")
    assert [f.finding_id for f in fs] == ["2024-08-x:H-1", "2024-08-x:M-1"]
    assert all(s.path.endswith(".sol") for s in fs[0].sites)
    assert not any("test/" in s.path for s in fs[0].sites)   # PoC/Recommendation links excluded

def test_new_format_sites_from_root_cause_only():
    fs = parse_report(NEW, "2025-07-y", "sherlock-audit/2025-07-y")
    assert fs[0].fmt == "new" and len(fs[0].sites) == 2      # Mitigation + Discussion links excluded

def test_old_format_without_code_snippet_falls_back_to_vulnerability_detail(): ...
def test_external_only_root_cause_keeps_finding_with_no_companion_sites():  # Review Focus 2
    assert fs[1].sites and all(s.external for s in fs[1].sites)

def test_reader_sections_exclude_poc_and_discussion():
    keys = set(reader_sections(fs[0]))
    assert keys == {"Summary", "Root Cause", "Attack Path", "Impact", "Mitigation"}
    # old format → {"Summary","Vulnerability Detail","Impact","Recommendation"}
```

- [ ] **Step 3: Run the tests and confirm they fail**

Run: `python -m pytest tests/test_sherlock_report.py -v`
Expected: FAIL (`ModuleNotFoundError: sherlock_report`)

- [ ] **Step 4: Implement `sherlock_report.py`**

Split on `^# Issue (H|M)-(\d+): (.*)$`. Detect the format by whether `### Root Cause` is present in the issue body. Split sections on `^#{2,3} ` headings. The site section is `Code Snippet` (old, falling back to `Vulnerability Detail`) or `Root Cause` (new). Find URLs with `https://github\.com/[^\s)\]>`]+`, strip trailing `.,;:`, then `urllib.parse.unquote` the path. Import the module in tests the way `tests/test_recall_mutation.py` does, with `sys.path.insert(0, ROOT/"experiments"/"sherlock_recall")` and `importlib.import_module`.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `python -m pytest tests/test_sherlock_report.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-11-18-sherlock-recall-set-design.md docs/superpowers/plans/2026-11-18-sherlock-recall-set.md experiments/sherlock_recall/sherlock_report.py tests/test_sherlock_report.py tests/fixtures/sherlock/
git commit -m "feat(sherlock-recall): parse Sherlock final reports into findings and sites"
```

---

### Task 2: Contest selection, eligibility, draw, and ref resolution

**Files:**
- Create: `experiments/sherlock_recall/build_set.py`
- Test: `tests/test_sherlock_build_set.py`

**Interfaces:**
- Consumes: `parse_report`, `JudgedFinding`, `Site` (Task 1).
- Produces:
  - `class GhSource(Protocol)`, with methods:
    - `list_judging_repos() -> list[str]`
    - `readme(repo: str) -> str | None`
    - `repo_exists(repo: str) -> bool`
    - `commit_before(repo: str, iso_date: str) -> str | None`
    - `first_issue_date(repo: str) -> str | None`
  - `class CliGhSource` implements it with `gh api` and caches JSON under `.mipdelta/sherlock_cache/gh/`.
  - `@dataclass class Contest: name: str; judging_repo: str; companion_repo: str; eligible: bool; reason: str; sol_share: float; n_findings: int; resolved_sha: str | None`
  - `contest_month(name: str) -> str | None` (`"2024-08"` from `"2024-08-cork-protocol"`)
  - `is_excluded(name: str) -> bool` (the protocol's own code only; see Global Constraints)
  - `assess(name: str, src: GhSource) -> tuple[Contest, list[JudgedFinding]]`
  - `draw(eligible_names: list[str], seed: int = 20261118, k: int = 20) -> tuple[list[str], list[str]]`, returning `(dev, measurement)`
  - `main(argv) -> int` with `--out results/sherlock_recall/` and `--dry-list` (writes the candidate list and eligibility only, no draw).
- Writes: `contests.json` (every candidate, its eligibility and reason, the draw order, and dev/measurement membership) and `recall_set.json` (findings and sites for the 20 drawn contests, with each `main` ref replaced by `resolved_sha`).

- [ ] **Step 1: Write the failing tests**, using a `FakeGh(GhSource)` dict-backed fake:

```python
def test_window_and_suffix(): assert contest_month("2024-08-cork-protocol") == "2024-08"
def test_exclusion_is_own_code_only():
    assert is_excluded("2024-03-morpho-blue") and is_excluded("2025-01-uniswap-v4-hooks")
    assert not is_excluded("2024-10-morpho-x-index")
def test_ineligible_reasons():  # Review Focus 3
    assert assess("2024-05-gone", fake_missing_companion)[0].reason == "companion_repo_missing"
    assert assess("2024-05-empty", fake_no_hm)[0].reason == "no_high_or_medium"
    assert assess("2024-05-rust", fake_rust)[0].reason == "non_solidity"   # sol_share < 0.80
def test_draw_is_deterministic_and_sorted_input():
    names = [f"2024-{i:02d}-p{i}" for i in range(1, 13)] + [f"2025-{i:02d}-q{i}" for i in range(1, 13)]
    dev, meas = draw(list(reversed(names)))
    assert (dev, meas) == draw(names) and len(dev) == 4 and len(meas) == 16 and not set(dev) & set(meas)
def test_main_ref_resolves_to_commit_before_first_issue():
    c, fs = assess("2024-08-x", fake_with_main_links)
    assert c.resolved_sha == "abc123" and all(s.ref == "abc123" for f in fs for s in f.sites if not s.external)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python -m pytest tests/test_sherlock_build_set.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `build_set.py`**

- **Draw:** sort the eligible names, then take `random.Random(seed).sample(names, k)`. The first 4 drawn are dev.
- **Ref resolution:** if every companion site is pinned to the same SHA, use it. Otherwise use `commit_before(companion, first_issue_date(judging))`, falling back to the default-branch HEAD with `reason += "|ref_fallback_head"`.
- **Candidate list:** `gh api -X GET search/repositories -f q='org:sherlock-audit judging in:name' --paginate`, then filter names by the `-judging` suffix and the month window.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python -m pytest tests/test_sherlock_build_set.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add experiments/sherlock_recall/build_set.py tests/test_sherlock_build_set.py
git commit -m "feat(sherlock-recall): select eligible Sherlock contests and draw a seeded sample"
```

---

### Task 3: Hit matching

**Files:**
- Create: `experiments/sherlock_recall/match_hits.py`
- Test: `tests/test_sherlock_match_hits.py`

**Interfaces:**
- Consumes: `Site` (Task 1); analyzer finding dicts shaped like `mip-delta --json` `findings[]` (`detector`, `symbol`, `path`, `start_line`, `end_line`, `evidence`); `mipdelta.solidity.parse_solidity_file(path, source) -> SolFileFacts` (`contracts[].functions[]` with `start_line`/`end_line`).
- Produces:
  - `norm_path(p: str) -> str` (POSIX, strips `./`, makes paths relative to the repo root)
  - `is_production_sol(path: str) -> bool`
  - `@dataclass(frozen=True) class Candidate: finding_id: str; analyzer: dict; site: Site | None; closeness: str` (`closeness` ∈ {`strict`, `function`, `file`})
  - `candidates_for(finding_id: str, sites: list[Site], obligation_path: str | None, analyzer_findings: list[dict], sources: dict[str, str]) -> list[Candidate]`, with one Candidate per analyzer finding at its closest closeness. It ignores external sites and non-production files.

- [ ] **Step 1: Write the failing tests**

```python
def test_strict_within_three_lines(): ...      # site L50-52, analyzer start 55 -> strict; 56 -> function (if same fn)
def test_declaration_line_counts_as_strict(): ...  # state_sink_review at L12, site L12 -> strict (not inside any fn)
def test_function_hit_uses_parsed_bodies(): ...
def test_file_level_in_obligation_file(): ...   # analyzer finding in obligation_path only -> closeness "file"
def test_paths_normalize_before_matching():     # Review Focus 4
    assert norm_path(".\\src\\A.sol") == "src/A.sol" == norm_path("./src/A.sol")
def test_non_production_and_external_ignored():
    assert not is_production_sol("test/A.t.sol") and not is_production_sol("lib/oz/ERC20.sol")
def test_unanchored_site_yields_only_file_closeness(): ...
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python -m pytest tests/test_sherlock_match_hits.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `match_hits.py`**

Closeness priority is strict > function > file. Use `start_line..end_line` of the analyzer finding against `[line_start-3, line_end+3]`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python -m pytest tests/test_sherlock_match_hits.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add experiments/sherlock_recall/match_hits.py tests/test_sherlock_match_hits.py
git commit -m "feat(sherlock-recall): match analyzer findings to judged sites by closeness"
```

---

### Task 4: Analyzer runner

**Files:**
- Create: `experiments/sherlock_recall/run_recall.py`
- Test: `tests/test_sherlock_run_recall.py`

**Interfaces:**
- Consumes:
  - `recall_set.json` (Task 2);
  - `mip-delta --audit-solidity --root <clone> --json` (stdout JSON with a `findings` key).
- Produces:
  - `clone_at(repo: str, sha: str, cache: Path) -> Path`. It does a shallow fetch of that SHA into `cache/<owner>__<name>@<sha>` and reuses an existing clone.
  - `run_audit(root: Path) -> dict`, the parsed CLI JSON plus `wall_ms`.
  - `main(argv) -> int` with `--set {dev,measurement}`, `--out`, and `--engine-commit` (required; written into the output).
- Writes `hits_<set>.json`: per contest, the raw analyzer findings (paths normalized with `norm_path`), production file and line counts, wall time and the resolved SHA. Candidates are **not** computed here: they need each finding's final `obligation_site`, which comes from labeling (Task 6).

- [ ] **Step 1: Write the failing test**

It builds a throwaway local git repo in `tmp_path` with `src/Vault.sol` holding an unread `private constant` (the same shape as the existing dead-state case in `tests/test_solidity_audit.py`) and `test/Vault.t.sol`. It then calls `run_audit` on it and `candidates_for` with a site on the constant's line.

```python
def test_runner_end_to_end_on_local_repo(tmp_path):
    out = run_audit(repo)
    assert any(f["detector"] == "state_sink_review" for f in out["findings"])
    assert all(not f["path"].startswith(("test/", "/")) and ":" not in f["path"] for f in out["findings"])
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python -m pytest tests/test_sherlock_run_recall.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `run_recall.py`**

- Call the CLI through `subprocess.run([sys.executable, "-m", "mipdelta.cli", "--audit-solidity", "--root", str(clone), "--json"], cwd=ROOT)` with the working directory set to the mipcode repo, never the clone. `cli.py` has an `if __name__ == "__main__"` entry.
- Clone with `git init` + `git fetch --depth 1 origin <sha>` + `git checkout FETCH_HEAD`.
- Sanitize output so no local absolute paths are written. Reuse the `_assert_no_local_paths` idea from `experiments/solidity_bounty/run_contest_corpus.py`.

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python -m pytest tests/test_sherlock_run_recall.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add experiments/sherlock_recall/run_recall.py tests/test_sherlock_run_recall.py
git commit -m "feat(sherlock-recall): clone audited commits and run the frozen analyzer"
```

---

### Task 5: Model client with preflight, cost ledger, and refusal handling

**Files:**
- Create: `experiments/sherlock_recall/llm.py`
- Test: `tests/test_sherlock_llm.py`

**Interfaces:**
- Produces:
  - `PRICES: dict[str, tuple[float, float]]` (USD per million input/output tokens; the values are in Global Constraints)
  - `class CostLedger: cap_usd: float; spent_usd: float; def charge(model: str, in_tok: int, out_tok: int) -> None; def would_exceed(est_usd: float) -> bool`. It persists to `.mipdelta/sherlock_cache/ledger.json`.
  - `class BudgetExceeded(Exception)`
  - `@dataclass class CallResult: status: str; data: dict | None; served_model: str; fallback_ran: bool; refusal_category: str | None; in_tok: int; out_tok: int`, where `status` ∈ {`ok`, `refused`, `invalid`}
  - `class Reader: def __init__(self, client, ledger: CostLedger, cache_dir: Path)`
  - `Reader.call(model: str, effort: str, system: str, user: str, schema: dict, key: str) -> CallResult`. The `key` is a stable cache key, so a rerun returns the cached response for free.
  - `preflight(client, model_ids: list[str]) -> dict[str, str]`, mapping each ID to its `display_name`. It raises if any ID fails `client.models.retrieve`.
  - `main(argv)` with `--preflight`, printing the JSON mapping.

`client` is an `anthropic.Anthropic()` in production and a fake in tests. Write the call against the SDK's documented surface:
- `client.beta.messages.create(model=..., max_tokens=2048, system=..., messages=[...], output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}}, betas=[...], fallbacks="default")` for Sonnet and Opus;
- a plain `client.messages.create` without the fallback fields for Haiku.

Confirm the parameter names against the installed SDK version (upgrade `anthropic` first if `output_config` isn't accepted), and record the version.

- [ ] **Step 1: Write the failing tests**, using a `FakeClient` that returns scripted responses:

```python
def test_ok_response_parsed_and_charged(): ...            # status ok, ledger.spent_usd == expected from PRICES
def test_refusal_recorded_not_raised():                    # Review Focus 5
    r = reader.call(...)   # fake stop_reason "refusal", stop_details.category "cyber"
    assert (r.status, r.refusal_category) == ("refused", "cyber") and ledger.spent_usd > 0
def test_malformed_json_and_max_tokens_are_invalid(): ...
def test_fallback_served_model_recorded(): ...             # response.model differs from requested -> fallback_ran True
def test_cache_hit_costs_nothing(): ...                     # second call same key -> client not called, spent unchanged
def test_cap_raises_before_call(): ...                      # ledger near cap -> BudgetExceeded, client not called
def test_no_sampling_params_and_no_forced_tool_choice():
    kwargs = fake.last_kwargs
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(kwargs)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python -m pytest tests/test_sherlock_llm.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `llm.py`**

- **Pre-call cost estimate:** the input-token estimate (`len(system + user) // 3`) at the input price, plus `max_tokens` at the output price. If `would_exceed`, raise `BudgetExceeded`.
- After each call, charge the actual `usage.input_tokens` and `output_tokens`.
- Check `stop_reason` before reading `content`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python -m pytest tests/test_sherlock_llm.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add experiments/sherlock_recall/llm.py tests/test_sherlock_llm.py
git commit -m "feat(sherlock-recall): model client with preflight, cost cap and refusal handling"
```

---

### Task 6: Blind class labeling and same-defect checks

**Files:**
- Create: `experiments/sherlock_recall/label.py`
- Create: `experiments/sherlock_recall/prompts/class_reader.md`, `experiments/sherlock_recall/prompts/same_defect.md`
- Test: `tests/test_sherlock_label.py`

**Interfaces:**
- Consumes: `JudgedFinding`, `reader_sections` (Task 1); `candidates_for`, `Candidate` (Task 3); `clone_at` (Task 4); `Reader`, `CallResult`, `BudgetExceeded` (Task 5).
- Produces:
  - `CLASS_SCHEMA: dict`. Its fields are `class`, `pattern` (nullable), `would_catch_with` (array, may be empty for `out_of_class`), `obligation_site` (nullable string `path#Lnn`), and `rationale` (≤ 300 characters). The enums are copied from Global Constraints.
  - `SAME_DEFECT_SCHEMA: dict`, with fields `verdict` and `rationale`.
  - `build_class_input(f: JudgedFinding, sources: dict[str, str]) -> str`. It renders `reader_sections(f)` plus each companion site with ±20 lines, and never includes analyzer output.
  - `consensus(labels: list[dict | None]) -> dict`. It takes the majority of `class` and of `pattern`; a `would_catch_with` tag is kept if at least 2 readers chose it (or both, when there are 2); `None` entries are refused readers. It returns `{..., "needs_tiebreak": bool}`, where `needs_tiebreak` is true when two readers differ on `class`, `pattern`, or the `would_catch_with` set, or one of them is `None`.
  - `label_findings(findings, sources, reader) -> list[dict]`, which runs A and B, then Opus only when `needs_tiebreak`. Each row stores every raw `CallResult`.
  - `check_candidates(cands, findings, sources, reader) -> list[dict]`. Candidates are built here, after consensus, with `candidates_for(finding_id, sites, obligation_path, analyzer_findings, sources)`. `obligation_path` is the path part of the final `obligation_site`. `sources` are read from the cached clone (`clone_at`), production `.sol` files only.
  - `main(argv)` with `--set {dev,measurement}` and `--out`. It writes `labels_<set>.json`, `candidates_<set>.json` and `same_defect_<set>.json`. On `BudgetExceeded` it writes partial output with `"partial": true` and exits 2.

`class_reader.md` must contain the spec §3 rubric (the class definitions, the pattern list, the `would_catch_with` definitions) and the Mitigation instruction **verbatim**:

> Classify the root-cause defect described in the Summary and Root Cause (or Vulnerability Detail). Use the Mitigation or Recommendation section only to locate a peer or base site that already handles this correctly. Ignore extra hardening suggestions there that are unrelated to the root cause; they do not change the class, pattern, or capabilities.

- [ ] **Step 1: Write the failing tests**

```python
def test_class_input_has_no_poc_discussion_or_analyzer(): ...
def test_class_input_context_is_plus_minus_20_lines(): ...
def test_prompt_contains_rubric_and_mitigation_line_verbatim():
    text = (PROMPTS / "class_reader.md").read_text(encoding="utf-8")
    assert MITIGATION_LINE in text and all(t in text for t in PATTERNS + CAPABILITIES)
def test_consensus_majority_and_multiselect():
    a = {"class": "in_class", "pattern": "param_unread", "would_catch_with": ["safety_param_weighting"]}
    b = {"class": "in_class", "pattern": "param_unread", "would_catch_with": ["safety_param_weighting", "fork_upstream_diff"]}
    assert consensus([a, b])["needs_tiebreak"] is True
    c = {"class": "in_class", "pattern": "param_unread", "would_catch_with": ["fork_upstream_diff"]}
    assert consensus([a, b, c])["would_catch_with"] == ["fork_upstream_diff", "safety_param_weighting"]
def test_refused_reader_forces_tiebreak(): assert consensus([a, None])["needs_tiebreak"]
def test_tiebreak_only_called_on_disagreement(): ...   # FakeReader call log: 2 calls when agree, 3 when not
def test_candidates_use_final_obligation_site(): ...    # analyzer finding only in obligation file -> one 'file' candidate checked
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python -m pytest tests/test_sherlock_label.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `label.py` and the two prompts**

- The same-defect prompt shows the finding's reader sections, the analyzer finding (detector, symbol, evidence), and both code locations, and asks for one verdict.
- Cache keys are `f"{model}:{finding_id}:{prompt_sha}"`, so editing a prompt invalidates its cache.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python -m pytest tests/test_sherlock_label.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add experiments/sherlock_recall/label.py experiments/sherlock_recall/prompts/ tests/test_sherlock_label.py
git commit -m "feat(sherlock-recall): blind class labeling with tie-break and same-defect checks"
```

---

### Task 7: Scoring

**Files:**
- Create: `experiments/sherlock_recall/score.py`
- Test: `tests/test_sherlock_score.py`

**Interfaces:**
- Consumes: `labels_<set>.json`, `candidates_<set>.json`, `same_defect_<set>.json`, `hits_<set>.json`, and the optional `spot_check_<set>.json`, a human-written list of `{finding_id, candidate_index, verdict: "agree"|"disagree", reason}`.
- Produces:
  - `wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]`
  - `class_coverage(labels) -> dict`, giving shares by class, overall and by severity
  - `miss_buckets(labels, hits) -> dict`, giving per-tag H/M counts and solo counts for in-class and adjacent misses
  - `next_build(buckets) -> dict`, the decision rule: the top tag excluding `none_of_these`; if the top two differ by fewer than 3, it returns `{"tie": True, "candidates": [...]}` plus the no-new-parser preference
  - `recall(labels, hits, same_defect, spot_check, cls="in_class") -> dict` with `strict`, `full`, `full_human`, `ci`, and `n`
  - `outcome(full_ci) -> str`, one of `meaningful` (≥40%), `partial` (15–40%), `low` (<15%), or `inconclusive:<a>|<b>` when the interval straddles a boundary
  - `agreement(labels) -> dict`, giving raw agreement, Cohen's κ for class and for pattern, and mean Jaccard for `would_catch_with`
  - `main(argv)` with `--set`, which writes `summary_<set>.json`

- [ ] **Step 1: Write the failing tests**

```python
def test_wilson_known_value(): assert wilson(5, 20) == pytest.approx((0.112, 0.469), abs=1e-3)
def test_outcome_inconclusive_when_ci_straddles(): assert outcome((0.10, 0.30)) == "inconclusive:low|partial"
def test_outcome_bands(): assert outcome((0.42, 0.70)) == "meaningful" and outcome((0.16, 0.39)) == "partial"
def test_recall_counts_only_same_defect_and_production_sites(): ...
def test_human_disagree_removes_hit_and_never_adds():
    r = recall(labels, hits, same_defect, spot_check=[{"finding_id": "c:H-1", "candidate_index": 0, "verdict": "disagree", "reason": "x"}])
    assert r["full_human"]["k"] == r["full"]["k"] - 1
def test_next_build_tie_rule(): assert next_build({"sibling_guard_asymmetry": 9, "fork_upstream_diff": 7})["tie"] is True
def test_kappa_perfect_agreement_is_one(): ...
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python -m pytest tests/test_sherlock_score.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `score.py`**

- The recall denominator is in-class findings with at least one non-external production site.
- A finding counts as hit if any of its candidates has the verdict `same_defect`, and for `full_human` it must also not be marked `disagree`.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS, including the pre-existing tests

- [ ] **Step 5: Commit**

```bash
git add experiments/sherlock_recall/score.py tests/test_sherlock_score.py
git commit -m "feat(sherlock-recall): class coverage, miss buckets, decision rule and recall scoring"
```

---

### Task 8: Dev-set dry run, preflight, and the frozen plan

**Files:**
- Create: `experiments/sherlock_recall/README.md` (the frozen plan)
- Create (force-added): `results/sherlock_recall/contests.json`, `recall_set.json`, and the dev-set outputs

**Interfaces:**
- Consumes: every module above.
- Produces: a committed plan whose git timestamp comes before any measurement-set analyzer run.

The operator must supply credentials, either an `ANTHROPIC_API_KEY` or an `ant auth login` profile. If neither is available, stop here and ask. *Amended (R19): the CLI transport uses the logged-in Claude Code subscription instead, so no API key is needed.*

- [ ] **Step 1: Build the candidate list and the draw**

Run: `python experiments/sherlock_recall/build_set.py --out results/sherlock_recall/`
Expected:
- `contests.json` lists every candidate with its eligibility, and 20 drawn names split 4/16;
- the non-Solidity count is recorded;
- `recall_set.json` contains the 20 contests.

- [ ] **Step 2: Run the model preflight**

Run: `python experiments/sherlock_recall/llm.py --preflight`
Expected: JSON mapping `claude-sonnet-5-5`, `claude-haiku-5-5` and `claude-opus-5-5` to their display names. On failure, stop and report the error; don't substitute other IDs.

- [ ] **Step 3: Run the dev set end to end**

Run, in order:
- `python experiments/sherlock_recall/run_recall.py --set dev --engine-commit 3ebdabd --out results/sherlock_recall/`
- `python experiments/sherlock_recall/label.py --set dev --out results/sherlock_recall/`
- `python experiments/sherlock_recall/score.py --set dev`

Expected: all three exit 0. Record the measured cost per finding from the ledger. Fix harness bugs only, never detectors. A report-format variant seen here gets a fixture and a parser test in Task 1's test file. Any added `pattern` tag must be added now, before freezing.

- [ ] **Step 4: Write `experiments/sherlock_recall/README.md` as the frozen plan**

Follow the house style of `experiments/held_out/README.md`. Fix the following:
- the engine commit and tree;
- the SDK version;
- the preflight output;
- the candidate count, eligibility counts and the draw (dev and measurement names);
- the rubric and prompt SHA-256s;
- the hit rule and the spot-check rule;
- every measure, threshold and the decision rule, from spec §5;
- the projected measurement cost from dev data against the $15 cap, including the Haiku-swap clause if it applies;
- the known limits.

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS

- [ ] **Step 6: Commit the frozen plan before any measurement run**

```bash
git add experiments/sherlock_recall/README.md
git add -f results/sherlock_recall/contests.json results/sherlock_recall/recall_set.json results/sherlock_recall/*_dev.json
git commit -m "docs(sherlock-recall): freeze measurement plan, contest draw and dev-set results"
```

---

### Task 9: Measurement run, human spot-check, and results

**Files:**
- Create: `results/sherlock_recall/README.md`
- Create (force-added): `results/sherlock_recall/*_measurement.json`, `spot_check_measurement.json`

**Interfaces:**
- Consumes: the frozen plan (Task 8). Nothing in `experiments/sherlock_recall/` or `src/mipdelta/` changes from here on. A needed harness fix is recorded as an amendment in the plan README, as the held-out study did.

- [ ] **Step 1: Run the analyzer once on the measurement set**

Run: `python experiments/sherlock_recall/run_recall.py --set measurement --engine-commit 3ebdabd --out results/sherlock_recall/`
Expected: `hits_measurement.json` for 16 contests.

- [ ] **Step 2: Label and check**

Run: `python experiments/sherlock_recall/label.py --set measurement --out results/sherlock_recall/`
Expected: exit 0, or exit 2 with `"partial": true` if the cap is reached. On exit 2, report it and stop; don't raise the cap.

- [ ] **Step 3: Human spot-check**

The operator reads every candidate with the verdict `same_defect` and writes `spot_check_measurement.json`. Do not do this step for the user. List the hits and wait.

- [ ] **Step 4: Score**

Run: `python experiments/sherlock_recall/score.py --set measurement`
Expected: `summary_measurement.json` with coverage, buckets, `next_build`, recall (strict, full, full_human, with CI), the outcome, agreement, refusal counts by model and category, fallback counts, and total cost.

- [ ] **Step 5: Write `results/sherlock_recall/README.md`**

Use the house style of `results/held_out/README.md`. Lead with class coverage and the miss-bucket ranking, then the decision-rule result, then recall with its interval and outcome, then agreement, refusals, cost and limits. Dev numbers go in their own section, never pooled.

- [ ] **Step 6: Commit**

```bash
git add -f results/sherlock_recall/
git commit -m "docs(sherlock-recall): measurement results, class coverage and next-build ranking"
```
