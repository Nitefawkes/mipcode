# MipCode (`.mip` / `.mip.json`) v0.5 — Architectural Mipmaps & Stateful Pre-Edit Guardrails for AI Coding Agents

> **Architectural Mipmaps + Stateful Pre-Edit Guardrails:** Compile any polyglot repository (`Rust`, `Python`, `TypeScript/JS`) into a self-contained `.mip.json` (`.mip`) container with incremental SQLite caching, Stateful "Fog-of-War" Resolution Gating, Production-First Blast-Radius Analysis, and a Zero-Tax Compact Markdown Wire Protocol.

> *"Semantic LOD v0.5 treats a codebase not as a flat file tree, but as a 3D surface geometry rendered under dynamic task lighting. By borrowing anisotropic filtering, BRDF shading, and ray-casting from computer graphics, .lod solves the multi-file byte-budget bottleneck—delivering blazing-fast cross-cutting execution traces while maintaining strict architectural governance through its stateful MMU."*

---

## The Dual Problem: Starvation vs. Overload

When coding agents modify complex repositories, two failure modes dominate:
1. **Context Starvation (Flat RepoMaps / Naive Lexical Search):** Signature dumps and raw grep strip out project-level decisions (`DEC-01`), doc-comment invariants (`//! INVARIANT`), inline module constants, and cross-language coupling. The agent edits a signature or constant and triggers cascading regressions across uninspected callers.
2. **Context Overload (Verbose JSON / Raw File Dumps):** Dumping entire files or repeating verbose JSON schemas and 64-character SHA-256 hashes per line exhausts tight agent token and byte budgets before the agent can inspect all affected callers.

**MipCode v0.5** solves both by combining **computer-graphics mipmapping (`L0`–`L3`)**, **Stateful Fog-of-War resolution gating**, **Active Pre-Edit Guardrails**, and **anisotropic ray-slicing**:

| Mip Level | Resolution Name | Contents | Access & Guardrail Rule |
| :--- | :--- | :--- | :--- |
| **L0** | `L0_WORLD_MAP` | Top modules by PageRank centrality, project decisions (`DEC-01`), and explicit `l0_loss` omission ledger fitting within token budget. | Always accessible without pre-conditions. |
| **L1** | `L1_BLUEPRINT` | Extracted module facts (`EXTRACTED_FACT`), contract invariants (`DECLARED_RULE`), heuristic warnings (`HEURISTIC_HAZARD`), and coupled modules. | Required before requesting `L3` raw source or modifying a coupled module. |
| **L2** | `L2_MECHANICS` | Compact module symbol index with non-zero child counts or full function/class signatures + docstrings + auto-inlined module constants. | Accessible once `L1` is hydrated or directly for symbol discovery. |
| **L3** | `L3_RAW_SOURCE` | Exact 1-based line-numbered source slices + auto-inlined referenced constants unpacked from embedded `zlib+base64` blobs and verified via live `SHA-256` freshness checks. | **Gated by Fog-of-War:** Jumping straight to `L3` or editing without `L1`/blast-radius hydration triggers an automatic guardrail interception. |

---

## MCP Tool Namespace (`mip.*` / Dual-Layer Tool Surface)

MipCode v0.5 establishes a **Dual-Layer Architecture**: the public MCP interface uses intuitive developer verbs so agents never burn reasoning tokens translating graphics jargon, while the under-the-hood engine executes the physical simulation model (BRDF shading, foveated raycasting, ambient occlusion).

| Upgraded Public MCP Tool | What It Does | Engine Alias |
| :--- | :--- | :--- |
| `mip.map()` (`mip_map` / `lod_map`) | Mount budgeted `L0_WORLD_MAP` with top modules by centrality, project rules, and explicit `l0_loss` ledger. | `lod_map` |
| `mip.symbols()` (`mip_symbols` / `lod_symbols`) | Query the cross-language symbol & call-reference index across all mipmap levels. | `lod_symbols` |
| `mip.zoom(target, level, focus="")` (`mip_zoom` / `lod_zoom`) | If `focus` is omitted, performs standard isotropic L1/L2/L3 zoom with Fog-of-War gating. If `focus="..."` (e.g., `"persist cache"`), it automatically executes the **Anisotropic BRDF Shader**, ranking relevant symbols (including deep private `_helpers`) by relevance score while culling off-axis noise under the byte cap. | `lod_anisotropic_zoom` / `mip_anisotropic_zoom` |
| `mip.trace(origin, direction, max_hops, focus="")` (`mip_trace` / `lod_trace`) | Cross-file foveated call-graph tracer. Hop 0/1 render foveated L3 source; hop 2+ stays at L2 signatures. Host-module `DECLARED_RULE` / `HEURISTIC_HAZARD` invariants are bundled and recorded via `_record_session_l1_hydration`, keeping Fog-of-War gates intact for untouched modules. | `lod_raycast` / `mip_raycast` |
| `mip.blast()` (`mip_blast` / `lod_blast_radius`) | Transitive BFS blast-radius analysis ranking **production** callers ahead of **test** callers and separating executable calls from docstrings/prose/imports. | `lod_blast_radius` |
| `mip.guardrail()` (`mip_guardrail` / `lod_pre_edit_guardrail`) | Single-call Pre-Edit Guardrail packet rendered in **Compact Markdown** (`79%–86%` smaller than JSON). | `lod_pre_edit_guardrail` |
| `mip.verify_edit()` (`mip_verify_edit` / `lod_verify_edit_eligibility`) | Host Pre-Edit Interception Hook (`Claude Code PreToolUse` / `git pre-commit`) enforcing `L1` & blast-radius hydration before file writes. | `lod_verify_edit_eligibility` |
| `mip.propose_delta()` (`mip_propose_delta` / `lod_propose_context_delta`) | Governed working-context writeback backed by cryptographic hydration receipts (`rcpt_...`). | `lod_propose_context_delta` |

*(All legacy `lod_*` tool aliases and `.lod.json` containers remain fully supported for 100% backward compatibility. Detailed anisotropic insights and mathematical foundations are documented in [ANISOTROPIC_INSIGHTS.md](ANISOTROPIC_INSIGHTS.md) and [docs/NAMING_AND_TOOL_ALIGNMENT_V05.md](docs/NAMING_AND_TOOL_ALIGNMENT_V05.md).)*

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
