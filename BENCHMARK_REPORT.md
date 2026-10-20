# Semantic LOD v0.5 — Multi-Condition Benchmark & Architectural Report

> *"Semantic LOD v0.5 treats a codebase not as a flat file tree, but as a 3D surface geometry rendered under dynamic task lighting. By borrowing anisotropic filtering, BRDF shading, and ray-casting from computer graphics, .lod solves the multi-file byte-budget bottleneck—delivering blazing-fast cross-cutting execution traces while maintaining strict architectural governance through its stateful MMU."*

- **Target Repository:** `demo_engine` (`10` modules, `2528 tokens` full source)
- **Container Artifact:** `demo_project.lod.json` (`Self-Contained: True`, `AST Backend: tree_sitter`, `Incremental Rebuild: 13.53ms`)

---

## Executive Scorecard Across All 4 Tasks

| Metric / Capability | Condition A: Baseline Raw Files (`read_file` + `grep`) | Condition B: Flat Signature RepoMap (Aider-style) | Condition C: Semantic LOD v0.4/v0.5 (`.lod`/`.mip` + Fog-of-War MCP) |
| :--- | :--- | :--- | :--- |
| **Initial Orientation Tokens** | `2528 tokens` (full scan) or blind | `343 tokens` (scales linearly with repo) | **`498 tokens`** (capped by budget; `9` modules in loss ledger) |
| **T1: Async Lifetime Hazard Task Tokens** | `2528 tokens` (full scan) | `541 tokens` | **`1984 tokens`** (L0 + Auto-Fault L1 + 9-line L3 slice) |
| **T1: Surfaces Working-Context Decision (`DEC-01`)** | No | No | **Yes (`DECLARED_WORKING_CONTEXT`)** |
| **T1: Separates `DECLARED_RULE` vs `HEURISTIC_HAZARD`** | No (raw comments only) | No (stripped by signature dump) | **Yes (3-tier epistemic typing)** |
| **T1: Enforces L1 Blueprint Before L3 Source Edit** | No | No | **Yes (`SEMANTIC_PAGE_FAULT_TRIGGERED`)** |
| **T2: Detects Cross-Language Python Dependent (`audit_worker.py`)** | No (if grepping Rust `use`) | No (manual inference only) | **Yes (`STATIC_AST_AND_SYMBOL_REFERENCE_GRAPH`)** |
| **T2: Blocks Premature Signature Edit on Coupled Module** | No | No | **Yes (`COUPLED_CONTRACT_PAGE_FAULT`)** |
| **T2: States Static-Analysis Epistemic Boundaries** | No | No | **Yes (Explicit dynamic-dispatch/macro disclaimer)** |
| **T3: Detects Out-of-Band Disk Drift (`SHA-256`)** | No | No | **Yes (`STALE_DISK_DRIFT_DETECTED`)** |
| **T3: Works 100% Offline Without Local Checkout** | No | No | **Yes (`zlib+base64` embedded source blobs)** |
| **T4: Rejects Uninspected Architectural Decisions** | No | No | **Yes (`DELTA_REJECTED_UNINSPECTED_MODULES`)** |
| **T4: Attaches Cryptographic Hydration Receipt to Delta** | No | No | **Yes (`3` receipts recorded)** |
| **v0.4 Task 1: Hybrid Tree-Sitter AST + Lexer Fallback** | N/A | N/A | **Yes (`tree_sitter` + fallback verified)** |
| **v0.4 Task 2: Incremental SQLite Rebuild (`<100ms`) & Receipt Invalidation** | N/A | N/A | **Yes (`13.53ms`, stale receipts invalidated)** |
| **v0.4 Task 3: Host File-Edit Hook (`lod_verify_edit_eligibility`)** | No | No | **Yes (Blocks unhydrated edit with exit `2`/`3` + auto-L1)** |
| **Codex Synthesis 1: Cross-File `lod_symbols` + Call-Ref Lookup** | No (text grep only) | No | **Yes (`2` hits; decl + call-ref verified)** |
| **Codex Synthesis 2: Compact Module `L2` Diet Byte Reduction** | N/A | N/A | **`-59.7%` (`36314B` -> `14640B`)** |
| **Codex Synthesis 3 (`RDS-02`): Symbol-Level Reverse Callers (`lod_blast_radius`)** | No (misses enclosing method) | No | **Yes (`4` callers, `4` production ranked first)** |
| **Codex Synthesis 4 (`RDS-03`): Auto-Inlined Module Constants on `L2`/`L3` Slices** | No (misses top-of-file consts) | No | **Yes (`True` — auto-attaches `defined_in` + literal)** |
| **Codex Synthesis 5 (`v4` Fix): Zero-Tax Compact Markdown Response Format** | N/A | N/A | **`-87.8%` (`9331B` -> `1143B`)** |

---

## Detailed Task Breakdown

### 1. `T1_ASYNC_LIFETIME_HAZARD`
- **Scenario:** Make RouteHandler::handle async without violating zero-copy RingBuffer<'buf> lifetimes
- **Why Condition C Wins:** When the agent attempts to jump straight from `L0` to `L3` on `sym://src/router.rs#RouteHandler`, the **Fog-of-War Gate** blocks the raw read and auto-hydrates the `L1` Blueprint first. The agent immediately receives both the `DECLARED_RULE` (*"CONTRACT: Must not hold RawFrame<'buf> across async suspension points"*) and the `HEURISTIC_HAZARD` warning before writing a single line of broken Rust code.

### 2. `T2_CROSS_LANGUAGE_BLAST_RADIUS`
- **Scenario:** Modify OwnedProposal in src/projection.rs and detect all Rust + Python upstream dependents
- **Why Condition C Wins:** Standard Rust `use` grepping misses that `tools/audit_worker.py` (Python) also depends on `OwnedProposal`. Furthermore, when the agent calls `lod_zoom("sym://src/projection.rs#OwnedProposal", level="L3", intent="modify_signature")`, Gate 2 triggers `COUPLED_CONTRACT_PAGE_FAULT` until `lod_blast_radius` is run.

### 3. `T3_STALE_DISK_DRIFT_DETECTION`
- **Scenario:** Detect when local source file has drifted from compiled architectural map
- **Why Condition C Wins:** Recomputes the live SHA-256 digest of `src/ingress.rs` against the container's `b68007e3...` digest, flagging `STALE_DISK_DRIFT_DETECTED` before the agent applies stale line-number patches.

### 4. `T4_HANDOFF_AND_GOVERNED_WRITEBACK`
- **Scenario:** Cross-agent handoff + governed architectural decision writeback with hydration proofs
- **Why Condition C Wins:** When Agent B tries to propose a decision affecting `mod://src/router.rs` and `mod://src/ingress.rs` without inspecting their `L1` blueprints, `lod_propose_context_delta` returns `DELTA_REJECTED_UNINSPECTED_MODULES`. Once inspected, the accepted delta permanently embeds the `hydration_proof` receipt ledger.
