# MipCode (`.mip` / `.mip.json`) v0.4 — Architectural Mipmaps & Stateful Pre-Edit Guardrails for AI Coding Agents

> **Architectural Mipmaps + Stateful Pre-Edit Guardrails:** Compile any polyglot repository (`Rust`, `Python`, `TypeScript/JS`) into a self-contained `.mip.json` (`.mip`) container with incremental SQLite caching, Stateful "Fog-of-War" Resolution Gating, Production-First Blast-Radius Analysis, and a Zero-Tax Compact Markdown Wire Protocol.

---

## Why MipCode?

When coding agents modify complex repositories, two failure modes dominate:
1. **Context Starvation (Flat RepoMaps / Naive Lexical Search):** Signature dumps and raw grep strip out project-level decisions (`DEC-01`), doc-comment invariants (`//! INVARIANT`), inline module constants, and cross-language coupling. The agent edits a signature or constant and triggers cascading regressions across uninspected callers.
2. **Context Overload (Verbose JSON / Raw File Dumps):** Dumping entire files or repeating verbose JSON schemas and 64-character SHA-256 hashes per line exhausts tight agent token and byte budgets before the agent can inspect all affected callers.

**MipCode v0.4** solves both by combining **computer-graphics mipmapping (`L0`–`L3`)**, **Stateful Fog-of-War resolution gating**, and **Active Pre-Edit Guardrails**:

| Mip Level | Resolution Name | Contents | Access & Guardrail Rule |
| :--- | :--- | :--- | :--- |
| **L0** | `L0_WORLD_MAP` | Active mission, decisions, global rules, and top modules ranked by coupling centrality within a strict token budget + exact `l0_loss` ledger. | Mounted first via `mip.map()` (`mip_map`). |
| **L1** | `L1_BLUEPRINT` | Public contracts, **3-tier epistemic rules** (`EXTRACTED_FACT`, `DECLARED_RULE`, `HEURISTIC_HAZARD`), and cross-language coupling edges. | Required before `L3` source edits (`mip.verify_edit()` / `mip_verify_edit`). |
| **L2** | `L2_MECHANICS` | Compact symbol signatures, line spans, outgoing calls, and **symbol-filtered inline module constants**. | Compact diet by default (`-65.6%` JSON byte reduction) via `mip.zoom(target, "L2")`. |
| **L3** | `L3_RAW_SOURCE` | Exact 1-based line-numbered source slices + auto-inlined referenced constants unpacked from embedded `zlib+base64` blobs and verified via live `SHA-256` freshness checks. | **Gated by Fog-of-War:** Jumping straight to `L3` or editing without `L1`/blast-radius hydration triggers an automatic guardrail interception. |

---

## MCP Tool Namespace (`mip.*`)

| Tool (`mip.*` / MCP Name) | Purpose |
| :--- | :--- |
| `mip.map()` (`mip_map`) | Mount budgeted `L0_WORLD_MAP` with top modules by centrality, project rules, and explicit `l0_loss` ledger. |
| `mip.symbols()` (`mip_symbols`) | Query the cross-language symbol & call-reference index across all mipmap levels. |
| `mip.zoom()` (`mip_zoom`) | Progressive `L1` / `L2` / `L3` mipmap zoom with Stateful Fog-of-War gating and live `SHA-256` freshness checks. |
| `mip.blast()` (`mip_blast`) | Transitive BFS blast-radius analysis ranking **production** callers ahead of **test** callers and separating executable calls from docstrings/prose/imports. |
| `mip.guardrail()` (`mip_guardrail`) | Single-call Pre-Edit Guardrail packet rendered in **Compact Markdown** (`79%–86%` smaller than JSON). |
| `mip.verify_edit()` (`mip_verify_edit`) | Host Pre-Edit Interception Hook (`Claude Code PreToolUse` / `git pre-commit`) enforcing `L1` & blast-radius hydration before file writes. |
| `mip.propose_delta()` (`mip_propose_delta`) | Governed working-context writeback backed by cryptographic hydration receipts (`rcpt_...`). |

*(Legacy `lod_*` tool aliases and `.lod.json` containers remain supported for backward compatibility.)*

---

## Origins: Why Passive Multi-Resolution Summaries Weren't Enough

**MipCode** began as an empirical investigation into hierarchical context compression (often discussed conceptually as "Semantic LOD" or multi-resolution repo summaries). Across five heldout benchmark iterations (`v0.1` $\rightarrow$ `v0.4`), we discovered that **passive hierarchical text summaries alone fail as a replacement for `rg` + file reads**:
- Under equal byte budgets, verbose JSON multi-resolution schemas lose to fast lexical grep (`rg`) on pure navigation tasks.
- Coding agents routinely ignore passive `expand_when` advisory hints when rushing to patch a file.

What actually moved first-attempt cross-module refactoring pass rates from **`0%` to `100%`** was replacing passive summaries with **MipCode's active guardrail architecture**:
1. **Stateful "Fog-of-War" Session Gating & Pre-Edit Hooks (`mip.verify_edit` / `mip.guardrail`)** that actively intercept blind edits (`exit_code=2` / `exit_code=3`) and auto-hydrate missing contracts and blast-radius callers.
2. **Three-Tier Epistemic Typing (`EXTRACTED_FACT`, `DECLARED_RULE`, `HEURISTIC_HAZARD`)** + explicit omission accounting (`l0_loss`, `omitted_at_l1`).
3. **Production-First Blast Radius (`mip.blast`)** separating genuine executable call sites from docstring/comment prose mentions and import statements.
4. **Zero-Tax Compact Markdown Wire Protocol (`render_compact_markdown`)** cutting agent wire overhead by **`79%–86%`** while preserving internal `SHA-256` provenance.

---

## Quick Start

### 1. Install
```bash
pip install -e .
# Optional: enable native Tree-Sitter AST backend and MCP SDK
pip install -e ".[ast,mcp]"
```

### 2. Compile a `.mip.json` Container (Full or Incremental)
```bash
python -m mipcode.compiler demo_engine 500 --mip --incremental
```

### 3. Run the Pre-Edit Guardrail Audit on Any Repository
```bash
python -m mipcode.real_repo_deploy demo_engine sym://src/projection.rs#OwnedProposal --markdown
```

### 4. Run the Full Multi-Condition Benchmark Suite
```bash
python -m mipcode.benchmark demo_engine --live-agent
```

---

## License

Apache License 2.0 — Copyright 2026 Brian Gansz. See [LICENSE](LICENSE).
