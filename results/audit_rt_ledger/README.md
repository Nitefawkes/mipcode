# Experiment 2: Keep-or-Kill Empirical Audit of `monitoring.py` (`[rt-obs]`) & `ledger.py` (`ObligationLedger`)

> **Two-phase audit at `$0` LLM API cost:** Phase 1 audited `src/mipdelta/` completely unmodified at [`bf0e3c8`](../../) to get an honest baseline keep-or-kill verdict on existing code. Phase 2 landed the targeted fixes revealed by Phase 1 in [`491bdda`](../../) and re-verified the entire Part A stress matrix, 30-mutation Part A recall benchmark, Part B 20-PR corpus, and Experiment 1 regression checks. Full machine-readable data is stored in [`summary.json`](summary.json), [`part_a_stress.json`](part_a_stress.json), [`part_a_recall.json`](part_a_recall.json), [`part_b_raw_180.json`](part_b_raw_180.json), and [`part_b_raw.json`](part_b_raw.json).

---

## 1. Pre-Registered Decision Matrix vs. Observed Verdict

| Component | Pre-Registered **Promote** Threshold | Pre-Registered **Prune** Trigger | Observed Result (`bf0e3c8` -> `491bdda`) | Final Verdict |
| --- | --- | --- | --- | --- |
| **Part A: `src/mipdelta/monitoring.py` (`--run-pytest`, PEP 669 `[rt-obs]`)** | `< 20%` median `pytest` overhead, `0` async/cov/`xdist`/`filterwarnings` crashes, AND `>= 25%` rescue of misses static AST misses. | `> 30%` overhead, runner/tool-ID crashes, OR `< 10%` net lift over static AST + installed `mypy`. | **As-shipped at `bf0e3c8`:** Failed due to 5 localized defects (`Mock`/`_Call` crash in `rich`, `src_root` shadowing in `mipcode`, missing MRO/`callee_qualname`/`<locals>`/`caller_line` lookup in `resolve.py`), yielding `12/30 (40.0%)` (`Arm 2b` diag `23/30 = 76.7%`).<br>**After `491bdda` fixes + `max_events_per_site=1` CLI default:** `0` tool-ID or `Mock` crashes (`rc=0` on `marshmallow`, `rich`, `mipcode`; `httpx` matches baseline failures cleanly), **`+5.26%` cold / `+14.17%` warm median overhead** (`<20%`), **`1209` unique prod edges on `mipcode`**, and **`27/30 (90.0%)` as-shipped recall (`Arm 2a`)** (`14/15 = 93.3%` optional wrappers where `mypy` is `0.0%`; **`+56.7%` net lift over static AST + installed `mypy`**). | **PROMOTE STRICTLY AS OPT-IN SUPPLEMENT (`--run-pytest`, `cap=1` default).** Default `mip-delta` stays zero-dependency ~5 ms pure AST; `--run-pytest` rescues dynamic calls on demand. |
| **Part B: `src/mipdelta/ledger.py` (`ObligationLedger`)** | Discharges `>= 25%` of WIP flags before `merge_head`, suppresses mid-PR re-alerts, AND `<= 10%` spurious ID churn across line shifts/renames. | `< 5%` (~0) WIP discharges across 20 PRs, OR brittle anchor churn making stateless `--base-ref P_0` strictly cleaner. | Across `20` multi-commit PRs (`804` scanned, `8` repos, `74` commits at `bf0e3c8`): **`9.09%` (`7/77`) discharge rate**, **no merge-head precision lift** (`47.8% -> 47.1%` TP), **`66.23%` (`151/228`) push re-alert suppression**, **`0` (`0.0%`) anchor churn across `19` line shifts**.<br>**After `491bdda` vendored + `@task(ctx)` exclusions:** `24/24` targeted FPs eliminated with `0/33` TP loss (`70 -> 46` merge-head open sites, `47.1% -> 71.7%` on the 20-PR set; `13.21%` `7/53` discharge rate `<25%`; `52.68%` re-alert suppression; `19` shifts / `0` churn). | **SCOPE-RESTRICT TO HOOK / SESSION / MULTI-PUSH DEDUP.** Stateless `--base-ref` gives identical single-shot CI PR gate output; never claim WIP PR precision lift from `ObligationLedger`. Keep `ledger.py` strictly for hook/session deduplication (`0` anchor churn, `52.7–66.2%` repeat push spam cut). |

---

## 2. Part A — `monitoring.py` (`--run-pytest`, PEP 669 `[rt-obs]`)

### 2.1 Five Defects Discovered at `bf0e3c8` & Fixed in `491bdda`

1. **Unhashable `unittest.mock` proxies (`monitoring.py`):** `rich` hard-crashed every single-process run (`TypeError: unhashable type: '_Call'`) when `sys.monitoring` observed `MagicMock`/`_Call` callables or receivers -> added zero-import `_is_mock_object` / `_safe_str_attr` guards while still applying `DISABLE` budgets.
2. **Self-monitoring `PYTHONPATH` shadowing (`cli.py`):** `_run_monitored_pytest` prepended the invoking checkout's `src_root` ahead of the inspected repository's `src/` -> reordered `PYTHONPATH` to prioritize `<baseline>/src` / `<root>/src` ahead of tool `src_root`.
3. **Missing `callee_qualname` / MRO inheritance lookup (`resolve.py`):** `resolve_runtime_edges` matched only `fn.class_qualname == receiver_type` short name and failed on inherited calls (`Child.method` executing `Base.method`) and unindexed `<locals>` test subclasses (`marshmallow` `B09`/`O11`) -> added `callee_qualname` exact check + MRO ancestor traversal (`_ancestor_class_names`).
4. **Conflated multi-call sites (`model.py`, `monitoring.py`, `resolve.py`):** Discarding line/offset fused distinct calls inside one function (`Status.update` calling both `Live.update` and `Spinner.update` in `B07`) -> serialized `caller_line` via `code.co_positions()` and disambiguated per AST call span (with single-site fallback).
5. **`<locals>` qualname mismatch + heavy default cap (`resolve.py`, `cli.py`):** Runtime `co_qualname` includes `.<locals>.` whereas static `model.py` omits it (`B13`); normalized `.replace(".<locals>.", ".")` and lowered CLI `--run-pytest` default `max_events_per_site` from `8` to `1`.

### 2.2 Track A1: Post-Fix (`491bdda`) Operational Overhead & Runner Compatibility

Tested in isolated `uv` virtual environments (`Python 3.12`) across `marshmallow` (`1192` tests), `rich` (`984 passed, 23 skipped`), `httpx` (`4 failed, 1413 passed, 1 skipped` baseline), and synced `mipcode` (`253` tests):

| Repo | Baseline Cold / Warm | `cap=1` Cold (`rc`) | `cap=1` Warm (`rc`) | `cap=8` Warm (`rc`) | Peak Job RAM (`Base -> cap=1`) | Shards / Prod Edges (`cap=1`) | `pytest --cov` (`mon`) | `pytest -n 2` (`mon`) |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| `marshmallow` | `4859.66 / 3858.06 ms` | `5405.54 ms` (`+11.23%`, `rc=0`) | `5876.80 ms` (`+52.33%`, `rc=0`) | `5293.26 ms` (`+37.20%`) | `53.92 -> 81.50 MB` | `1` shard / `624` prod (`2201.18 KB`) | `rc=0` (`+34.53%`, `624` prod) | `rc=0` (`+21.30%`, `3` sh / `667` prod) |
| `rich` | `9366.19 / 7977.74 ms` | `10123.01 ms` (`+8.08%`, `rc=0`) | `9429.90 ms` (`+18.20%`, `rc=0`) | `9941.77 ms` (`+24.62%`) | `68.01 -> 110.46 MB` | `1` shard / `2619` prod (`2871.69 KB`) | `rc=0` (`+8.77%`, `2619` prod) | `rc=0` (`-17.15%`, `3` sh / `2637` prod) |
| `httpx` | `121151.78 / 121796.55 ms` | `124112.71 ms` (`+2.44%`, `rc=1`¹) | `124353.19 ms` (`+2.10%`, `rc=1`¹) | `121461.07 ms` (`-0.28%`) | `270.20 -> 193.61 MB` | `1` shard / `1241` prod (`1560.82 KB`) | **`rc=1` both** (`+8.17%`)² | `rc=1`¹ (`-9.62%`, `3` sh / `1263` prod) |
| `mipcode` | `107932.64 / 102382.05 ms` | `105547.75 ms` (`-2.21%`, `rc=0`) | `112761.96 ms` (`+10.14%`, `rc=0`) | `113792.19 ms` (`+11.14%`) | `110.32 -> 130.22 MB` | `7` shards / **`1209` prod** (`1577.79 KB`) | `rc=0` (`-0.74%`, `1209` prod) | `rc=0` (`+15.12%`, `9` sh / `1210` prod) |

- **Median overhead (`4` repos):** **`+5.26%` cold**, **`+14.17%` warm**; **`0` tool/hook warnings** (`any_tool_or_hook_warnings = false`). Notice `marshmallow` is a 3.8s suite sensitive to fixed Python startup/JSON-flush variance (`+1.5s–2.0s` = `+52.3%`), whereas the larger suites run `+2.1%–+18.2%`.
- **Compare `bf0e3c8` baseline:** Prior to `491bdda`, `rich` crashed (`rc=1`, `0` shards) across every single-process run and `mipcode` collected `0` prod edges due to `src_root` shadowing.
- Notes: ¹ `httpx` baseline fails the same 4 `test_write_timeout` tests (`0` additional `anyio`/`asyncio`/`trio` crashes). ² `httpx` `pytest --cov` aborts in both baseline (`2489.85 ms`) and monitored (`2693.16 ms`) because `pyproject.toml` sets `filterwarnings = ["error"]`, rejecting `pytest-cov`'s `CoverageWarning`.

### 2.3 Track A2: Repo-Wide Coverage & 30-Mutation 4-Arm Recall (`B01..B15`, `O01..O15`)

Repo-wide unresolved workspace-method attribute calls rescued by green-baseline `cap=1` (`default_warm`) vs `cap=8` (`cap8_warm`) shards (`static_attr_unresolved` -> `arm2a` resolved / `arm2b` harness / `raw_edge_present`):

- `httpx` (`266` unresolved): `cap=1` (`4622` edges) `Arm 2a` `118 (44.4%)` / `Arm 2b` `126 (47.4%)` / raw `258 (97.0%)`; `cap=8` (`4686` edges) `Arm 2a` `120 (45.1%)`.
- `rich` (`817` unresolved): `cap=1` (`8770` edges) `Arm 2a` `317 (38.8%)` / `Arm 2b` `331 (40.5%)` / raw `518 (63.4%)`; `cap=8` (`8877` edges) `Arm 2a` `319 (39.0%)`.
- `marshmallow` (`100` unresolved): `cap=1` (`6035` edges) `Arm 2a` `24 (24.0%)` / `Arm 2b` `29 (29.0%)` / raw `99 (99.0%)`; `cap=8` (`6356` edges) `Arm 2a` `26 (26.0%)`. (`Arm 2b` doesn't enforce `caller_line` disambiguation, matching extra call lines in multi-branch functions.)
- `mipcode` (`136` unresolved): `cap=1` (`3950` edges) `Arm 2a` `34 (25.0%)` / `Arm 2b` `34 (25.0%)` / raw `92 (67.6%)`; `cap=8` (`3976` edges) identical (`34 / 25.0%`).

30-mutation 4-arm recall benchmark (`15` breaking `B01..B15` + `15` optional delegating wrappers `O01..O15`; installed `mypy` baselines: `httpx=0`, `rich=0`, `marshmallow=0`, `mipcode=17` existing errors excluded via diffing):

| Group | `Arm 1` (`static` AST) | `Arm 2a` (`[rt-obs]` shipped `491bdda`, `cap=1`) | `Arm 2b` (Harness MRO, `cap=8`) | `Arm 3` (Installed `mypy`) | Net Lift `Arm 2a` over `Arm 1 + mypy` |
| --- | ---: | ---: | ---: | ---: | ---: |
| **Breaking (`15`: `B01..B15`)** | `0/15 (0.0%)` | **`13/15 (86.7%)`** | `13/15 (86.7%)` | `11/15 (73.3%)` | **`+3/15 (+20.0%)`** (`B09`, `B10`, `B11`) |
| **Optional Wrapper (`15`: `O01..O15`)** | `0/15 (0.0%)` | **`14/15 (93.3%)`** | `14/15 (93.3%)` | `0/15 (0.0%)` | **`+14/15 (+93.3%)`** |
| **Overall (`30`)** | `0/30 (0.0%)` | **`27/30 (90.0%)`** *(was `12/30 = 40.0%` at `bf0e3c8`)* | `27/30 (90.0%)` | `11/30 (36.7%)` | **`+17/30 (+56.7%)`** *(was `+7/30 = +23.3%` at `bf0e3c8`)* |

- Targeted `pytest` on breaking mutations (`B01..B15`): `14/15` failed as expected; `B15` (`DeltaSession.waive`) passed targeted tests yet was caught statically + dynamically (`Arm 2a`) using full-suite baseline shards.
- **Only `3/30` remaining misses (`B03`, `B14`, `O13`) — all test-execution coverage gaps:** `B03` (`httpx` `Client._send_single_request -> HTTPTransport.handle_request` is mocked out with `MockTransport` in tests) and `B14` / `O13` (`mipcode` `_run_hook -> ObligationLedger.{unreported_at_stop, mark_reported_at_stop}` has no direct monitored caller invocations in green `pytest` shards). Notice `cap=1` (`Arm 2a`) achieves the exact same `27/30 (90.0%)` recall as `cap=8` (`Arm 2b`).

---

## 3. Part B — `ledger.py` (`ObligationLedger`) Multi-Commit PR Audit

### 3.1 Selection Funnel & Progression Metrics (`bf0e3c8` Audit + `491bdda` Recheck)

Scanning first-parent merges sequentially across 9 held-out clones (`2 <= k <= 12` linear non-merge commits changing production `.py` files, `<= 3` PRs per repo, `$0` cost):
- Initial `180`-merge scan produced `7` flagged PRs across `3` repos (`httpx 3`, `marshmallow 2`, `mkdocs 2`; `3/35 = 8.57%` discharged).
- Widened scan (`804` linear prod PRs scanned across `4,111` first-parent merges) yielded **`20` contract-flagged PRs across `8` repos** (`httpx 3`, `marshmallow 3`, `mkdocs 3`, `rich 1`, `pytest 3`, `requests 3`, `scrapy 3`, `urllib3 1`; `paramiko 0/15`), totaling **`74` commits**.

| Metric | `bf0e3c8` (Baseline) | `491bdda` (Vendored + `@task` Exclusions) | Verdict / Meaning |
| --- | ---: | ---: | --- |
| `total_per_commit_findings` (`sum c_{i-1} -> c_i`) | `69` (`33/69 TP = 47.8%`) | **`45`** (`33/45 TP = 73.3%`) | Commit-level churn (`24` FPs removed, `0` TPs lost). |
| `total_stateless_base_alerts` (`sum P_0 -> c_i`) | `228` | **`112`** | Repeat push alerts if running `--base-ref P_0` on every commit without ledger state. |
| `total_ledger_opened` (`unique opened`) | `77` (`38/77 TP = 49.4%`) | **`53`** (`38/53 TP = 71.7%`) | Unique obligations opened across the PR progression. |
| `total_ledger_discharged` (`opened -> resolved`) | `7` (`5 TP + 2 FP`, **`9.09%`**) | **`7`** (`5 TP + 2 FP`, **`13.21%`**) | Below `>= 25%` Promote target; above `< 5%` Prune floor. |
| `total_open_at_merge_head` (`P_0 -> c_k`) | `70` (`33/70 TP = 47.1%`) | **`46`** (`33/46 TP = 71.7%`) | Identical to stateless `--base-ref P_0` at merge head (`0` WIP precision lift over stateless `--base-ref`). |
| `realert_suppression_ratio` (`1 - opened/stateless`) | **`66.23%`** (`151/228`) | **`52.68%`** (`59/112`) | Suppresses repeat notifications across multi-push sessions (`requests PR#12` dropped completely to `0` under `491bdda`). |
| `line_shifts_survived_count` vs `anchor_churn` | **`19` shifts, `0` churn** | **`19` shifts, `0` churn** | `enclosing_function` anchors survive `-1` to `+80` line edits with `0%` spurious reopen churn. |
| Diff-scoped installed `mypy` (`new_errors` at head) | `6/20` PRs (`43` errors) | — | **Zero overlap** on `22` genuine wrapper/sink omissions in `httpx PR#01` (`7x Client.<verb>`), `httpx PR#04` (`14x SyncClient.<verb>`), `mkdocs PR#03`, and `rich PR#09` (`Traceback.__init__`). |

### 3.2 Per-PR Breakdown & Effect of `491bdda` Path/Pattern Filters

- **Why `ObligationLedger` does NOT lift PR-gate precision over stateless `--base-ref`:** At `merge_head`, `ObligationLedger.open()` and stateless `mip-delta --check-git --base-ref P_0` emit the exact same set (`70` at `bf0e3c8`, `46` at `491bdda`). Real WIP mistakes resolved in intermediate commits (`5` ledger-discharged TPs across `marshmallow#1503`, `httpx#42`, `urllib3#763`, plus `2` commit-only TPs in `mkdocs#396`, `scrapy#4900`), while persistent FPs remained open unless filtered statically (`bf0e3c8`: `47.8% -> 47.1%`; `491bdda`: `73.3% -> 71.7%`).
- **Impact of `is_vendored_path` & Invoke `@task(ctx)` filters in `491bdda`:** Eliminated **`24/24` targeted persistent merge-head FPs** (`12` `requests/packages/chardet/*` in `PR#11`, `7` `requests/packages/idna/codec.py` in `PR#12`, `5` `marshmallow/tasks.py` in `PR#17` — dropping all 3 PRs cleanly to `0` findings) with **`0/33` TP loss** on Part B and **`13/13` `real_omission` + `5/5` `review_worthy` (`0/30` silent controls)** preserved on the Experiment 1 (`N=67`) dataset. Remaining `13` FPs at merge head isolate cleanly to `pytest/capture.py` (`4` moved `_assert_state`, `1` TextIO `seek` stub), `scrapy` (`3` asyncgen/hook API params), and `5` cross-class/intentional shims.

---

## 4. Final Architectural Scope Summary

1. **Default CLI / CI PR Gate (`mip-delta --check-git`):** Zero-dependency static AST (~5 ms), automatically ignoring vendored subtrees (`is_vendored_path`) and Invoke `@task` context parameters, with optional Stage-2 LLM micro-triage (`--review`, `92.3%` Precision in Experiment 1). For single-shot CI PR checks, prefer stateless `--base-ref <merge-base>`.
2. **Opt-In Dynamic Supplement (`--run-pytest`, `monitoring.py`):** Promoted strictly as an opt-in `--run-pytest` dynamic supplement (`max_events_per_site=1` default: `+5.26%` cold / `+14.17%` warm median overhead, `0` crashes/collisions, `27/30 = 90.0%` recall, `14/15 = 93.3%` on optional delegating wrappers invisible to `mypy`).
3. **Hook / Session / Multi-Push Deduplication (`ledger.py`):** Explicitly scoped to deduplicating repeated agent-hook (`--hook`) and multi-push (`--ledger`) runs (`0` anchor churn across `19` line shifts; `52.7–66.2%` repeat alert suppression). Never claim `ObligationLedger` improves single-shot merge-head PR precision over stateless `--base-ref`.
