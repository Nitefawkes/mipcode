# Experiment 2: Runtime Observation (`monitoring.py`) & Obligation Ledger (`ledger.py`) Keep-or-Kill Audit

## Question

Two stateful/runtime subsystems currently live inside `src/mipdelta/` with synthetic/fixture coverage (`tests/test_v2_session_stubs_monitoring.py`, `tests/test_obligation_ledger.py`) and agent pilot traces, while `mip-delta`'s headline evidence rests on the 10-repository static git replay (`results/held_out/`) and Experiment 1 (`results/two_stage_sieve/`):

1. **Part A — `src/mipdelta/monitoring.py` (`--run-pytest`, PEP 669 `[rt-obs]`):** Does runtime receiver collection (`max_events_per_site=8`) rescue real dynamic attribute, delegating-wrapper, and override omissions across `httpx`, `rich`, `marshmallow`, and `mipcode` that both static AST and installed `mypy` miss—without slowing down `pytest` or colliding with `coverage.py` / `pytest-cov`, `anyio` / `trio` / `pytest-asyncio`, `filterwarnings = ["error"]`, or `pytest-xdist`?
2. **Part B — `src/mipdelta/ledger.py` (`ObligationLedger`):** Across real multi-commit upstream PRs (`P_0 -> c_1 -> ... -> c_k = merge_head`), does `ObligationLedger` materially discharge intermediate WIP commit flags before merge, suppress mid-PR re-alert spam cleanly against a stateless `git diff P_0..HEAD` check, and maintain stable `enclosing_function` anchors across line shifts—or is it dead weight with near-zero WIP discharge or brittle anchor churn?

**Protocol invariant:** Phase 1 audited `src/mipdelta/` completely unmodified (`bf0e3c8`) at `$0` LLM API cost. Phase 2 re-verified the same stress, recall, and 20-PR suites after landing the `491bdda` runtime/lookup fixes, `max_events_per_site=1` default, and vendored/`@task` exclusions (documented in `results/audit_rt_ledger/`).

---

## Code-Grounded Protocol Augmentations (Logged Pre-Run)

Deep inspection of `src/mipdelta/monitoring.py`, `src/mipdelta/resolve.py`, `src/mipdelta/ledger.py`, and `src/mipdelta/cli.py` identified four confounds that MUST be measured explicitly rather than hidden:

1. **Key-Shape & Inheritance Confound (`resolve.py:312`):**
   - `monitoring.py` keys edge shards by `(caller_path, caller_qualname, instruction_offset, callee_qualname, callee_name, receiver_type)` and `resolve.py` joins on `(call.owner_function, call.callee_name)`.
   - Crucially, `monitoring.py` records BOTH `callee_qualname = "Base.method"` AND `receiver_type = "Child"`, yet `resolve.py:312` checks only `fn.class_qualname.split(".")[-1] == edge.receiver_type.split(".")[-1]`. Any inherited method call (e.g., `self.make_error` in `marshmallow.fields.Nested` or `self.normalize_path` in `mipdelta.cli._CheckingCache`) fails `[rt-obs]` lookup in as-shipped `src/mipdelta`.
   - **Decision:** Part A scores **Arm 2a (`static + rt-obs` as shipped)** AND **Arm 2b (`static + rt-obs` + inheritance diagnostic)** separately inside the experiment harness so fundamental PEP 669 limits are cleanly separated from a localized `resolve.py:312` lookup defect.
2. **Breaking-Edit Execution Semantics & Green-Baseline Shard Reuse:**
   - Running `--run-pytest` *after* introducing a covered breaking signature mutation causes `pytest` itself to fail with `TypeError` on the mutated call (and uncovered calls produce `0` edges anyway).
   - **Decision:** Part A runs `--run-pytest` on clean `HEAD` once per repo to generate `.mipdelta/runtime/edges-*.json` (modeling IDE / hook `PostToolUse` cached shards), measures repo-wide dynamic-call edge coverage on all unresolved workspace-method attribute calls in `src/`, AND records whether `pytest` itself catches the 15 breaking mutations via test failure.
3. **Stateless `--base-ref P_0` Baseline vs. `ObligationLedger` (Part B):**
   - At `merge_head` (`c_k`), stateless `mip-delta --check-git --base-ref P_0` produces the exact same unwaived open findings as `ObligationLedger`. Therefore `ledger.py` must earn its keep across all three progression metrics:
     1. **WIP Noise Discharge:** `sum_i findings(c_{i-1} -> c_i)` (per-commit churn) and `len(opened)` vs `open_at_merge_head` (`P_0 -> c_k`).
     2. **Mid-PR Re-Alert Suppression:** `sum_i findings(P_0 -> c_i)` (repeated stateless base-ref notifications across pushes) vs unique `len(opened)` events in `ObligationLedger`.
     3. **Anchor Stability / Churn Under Line Shifts:** Persisting `open -> open` findings whose `start_line` shifted (testing whether `enclosing_function` absorbs edits above a site) vs spurious `discharged + reopened` ID churn caused by module-level empty anchors (`anchor == ""`), ordinal shifts when duplicate sites appear in one function, or function/file renames.
4. **Zero Cherry-Picking Across Repositories:**
   - **Part A (30 mutations):** Derived directly from unresolved workspace-method attribute calls at current `HEAD` across `httpx`, `rich`, `marshmallow`, and `mipcode`, grounded in historical held-out missed patterns (e.g., `URL.join`, `Client.request`, `BaseTransport.handle_request`, `Syntax.stylize_range`, `Schema.dump` / `Field._serialize`, `FactCache.normalize_path`): **15 breaking signature mutations** + **15 optional-parameter delegating-wrapper / override omissions**.
   - **Part B (20 multi-commit PRs):** Selected sequentially in reverse-chronological `--merges --first-parent` order (`2 <= k <= 12` clean linear non-merge commits `P_0 -> c_1 -> ... -> c_k`, `>= 1` production `mip-delta` finding across the progression, capped at `<= 3` PRs per repo across our 9 clones: `httpx`, `marshmallow`, `mkdocs`, `rich`, `pytest`, `requests`, `scrapy`, `urllib3`, `paramiko`; `attrs` excluded as having only `1` recent multi-commit PR).

---

## Experimental Design

### Part A — `monitoring.py` (`--run-pytest`)

1. **Track A1 (Operational & Runner Compatibility Stress Matrix):**
   - Four isolated `uv` virtualenvs (`C:\tmp\exp2-venvs\{mipcode,httpx,rich,marshmallow}`) matching each repo's real `pyproject.toml` dependencies plus `pytest`, `pytest-cov`, `pytest-xdist`, and `mypy`.
   - Record **cold & warm** wall-clock `ms`, overhead `%`, peak RSS `MB`, shard count, JSON bytes written (total + production-only vs test-only split), and `max_events_per_site` cap-hit rate (`8` vs `1` comparison).
   - Run explicit collision checks:
     - `pytest --cov` (`coverage.py 7.x` `sys.monitoring` `COVERAGE_ID=1` vs `monitoring.py` `available[0]` `DEBUGGER_ID=0`)
     - `httpx` async suite (`anyio`, `asyncio`, `trio`, `filterwarnings = ["error"]`)
     - `pytest -n 2` (`pytest-xdist` multi-process worker shard writing)
2. **Track A2 (30-Mutation 4-Arm Recall + Dynamic Edge Coverage):**
   - **Arm 1 (`static`):** Default `mip-delta` static AST resolution.
   - **Arm 2a (`static + rt-obs`, as-shipped):** `analyze_delta(..., runtime_edges=...)` using `HEAD` `--run-pytest` shards.
   - **Arm 2b (`static + rt-obs` + inheritance diagnostic):** Harness diagnostic allowing `edge.callee_qualname` / base-class method resolution.
   - **Arm 3 (Installed `mypy`):** Running `mypy` inside each repo's real `uv` virtualenv (`strict = true` where configured by repo).
   - Also measure **dynamic edge coverage** across all unresolved workspace-method attribute calls in `src/` and check baseline vs mutant `pytest` pass/fail on the 15 breaking mutations.

### Part B — `ledger.py` (`ObligationLedger`)

- Replay all 20 sequential contract-flagged multi-commit PRs via Git plumbing (`P_0 -> c_1 -> ... -> c_k`) tracking:
  - Per-commit incremental checks (`c_{i-1} -> c_i`)
  - Cumulative stateless base-ref checks (`P_0 -> c_i`, with final step `i = k` equal to merge-gate `open_at_merge_head`)
  - Stateful `ObligationLedger` progression across `c_1 .. c_k`
  - Diff-scoped installed `mypy` errors introduced per step vs `P_0` on touched production files
- Inspect and adjudicate every unique opened / discharged / merge-open site to measure **per-commit precision vs PR-merge-boundary precision** alongside natural WIP discharge rate, re-alert suppression ratio, and anchor churn.

---

## Pre-Registered Keep / Kill Decision Matrix

| Component | Promote & Productize | Prune & Delete (`git rm`) |
| --- | --- | --- |
| **Part A: `monitoring.py` (`--run-pytest`)** | `< 20%` median `pytest` overhead, `0` async/coverage/`xdist`/`filterwarnings` runner crashes, AND `>= 25%` rescue of dynamic attribute/wrapper misses that static AST misses. | `> 30%` overhead, coverage/`anyio`/async/`xdist` tool-ID collisions or crashes, OR `< 10%` net recall lift over static AST + installed `mypy` (`Arm 2a` & `Arm 2b` reported explicitly to separate look-up bugs from fundamental `sys.monitoring` limits). |
| **Part B: `ledger.py` (`ObligationLedger`)** | Discharges `>= 25%` of intermediate WIP commit flags before `merge_head`, suppresses mid-PR re-alerts, AND tracks state cleanly (`<= 10%` spurious ID churn across line shifts/edits/renames). | `< 5%` (~0) intermediate WIP discharges across 20 real multi-commit PRs, OR brittle anchor churn/drift across ordinary intermediate edits (making stateless `mip-delta --base-ref P_0` strictly cleaner). |
