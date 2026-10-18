# Semantic LOD (`.lod`) v0.4 — Multi-Resolution Cognitive Containers & Pre-Edit Guardrails for AI Coding Agents

> **Architectural Mipmaps + Stateful Pre-Edit Guardrails:** Compile any polyglot repository (`Rust`, `Python`, `TypeScript/JS`) into a self-contained `.lod.json` container with incremental SQLite caching, Stateful "Fog-of-War" Resolution Gating, Production-First Blast-Radius Analysis, and a Zero-Tax Compact Markdown Wire Protocol.

---

## Why Semantic LOD?

When coding agents modify complex repositories, two failure modes dominate:
1. **Context Starvation (Flat RepoMaps / Naive Lexical Search):** Signature dumps and raw grep strip out project-level decisions (`DEC-01`), doc-comment invariants (`//! INVARIANT`), inline module constants, and cross-language coupling. The agent edits a signature or constant and triggers cascading regressions across uninspected callers.
2. **Context Overload (Verbose JSON / Raw File Dumps):** Dumping entire files or repeating verbose JSON schemas and 64-character SHA-256 hashes per line exhausts tight agent token and byte budgets before the agent can inspect all affected callers.

**Semantic LOD v0.4** solves both by combining **Level of Detail (LOD) mipmapping**, **Stateful Fog-of-War resolution gating**, and **Hybrid Pre-Edit Guardrails**:

| Layer | Resolution Name | Contents | Access & Guardrail Rule |
| :--- | :--- | :--- | :--- |
| **L0** | `L0_WORLD_MAP` | Active mission, decisions, global rules, and top modules ranked by coupling centrality within a strict token budget + exact `l0_loss` ledger. | Mounted first (`lod_map`). |
| **L1** | `L1_BLUEPRINT` | Public contracts, **3-tier epistemic rules** (`EXTRACTED_FACT`, `DECLARED_RULE`, `HEURISTIC_HAZARD`), and cross-language coupling edges. | Required before `L3` source edits (`lod_verify_edit_eligibility`). |
| **L2** | `L2_MECHANICS` | Compact symbol signatures, line spans, outgoing calls, and **symbol-filtered inline module constants**. | Compact diet by default (`-65.6%` JSON byte reduction). |
| **L3** | `L3_RAW_SOURCE` | Exact 1-based line-numbered source slices + auto-inlined referenced constants unpacked from embedded `zlib+base64` blobs and verified via live `SHA-256` freshness checks. | **Gated by Fog-of-War:** Jumping straight to `L3` or editing without `L1`/blast-radius hydration triggers an automatic guardrail interception. |

---

## Key Capabilities in v0.4

1. **Hybrid Pre-Edit Guardrail (`lod_pre_edit_guardrail` & `lod_verify_edit_eligibility`):**
   - Intercepts unhydrated file or signature edits (`exit_code=2` / `exit_code=3`) via Claude Code `PreToolUse` or Git `pre-commit` hooks.
   - Delivers a single-call **Pre-Edit Guardrail Packet** combining target contracts, symbol-filtered inline constants, active `DECLARED_RULE` / `HEURISTIC_HAZARD` warnings, and production-first callers.
2. **Production-First Blast Radius + Executable Call vs. Prose Separation (`lod_blast_radius`):**
   - Resolves cross-file and conditional/function-local imports across the repository.
   - Partitioned and sorted so **`production` callers always rank ahead of `test` callers**.
   - AST-grounded line classification separates genuine **Executable Callers** from **Non-Call References** (`docstring_or_comment` prose mentions and `import_statement` lines).
3. **Zero-Tax Compact Markdown Wire Protocol (`render_compact_markdown`):**
   - Retains full cryptographic SHA-256 provenance in the internal container while rendering agent-facing responses in dense Markdown (**`79%–86%` fewer UTF-8 bytes** than structured JSON).
4. **Hybrid Tree-Sitter AST + Incremental SQLite Cache (`lod_module_cache`):**
   - Uses native `tree-sitter` grammars (`rust`, `python`, `typescript`/`tsx`) with automatic fallback to stateful lexers and Python `ast`.
   - Caches parsed module ASTs keyed by `SHA-256` in SQLite for **sub-10ms warm incremental rebuilds** (`4.8x–10x` speedup on multi-hundred-module repositories) and automatic stale session receipt invalidation.

---

## Quick Start

### 1. Install
```bash
pip install -e .
# Optional: enable native Tree-Sitter AST backend and MCP SDK
pip install -e ".[ast,mcp]"
```

### 2. Compile a Repository (Full or Incremental)
```bash
python -m semantic_lod.compiler demo_engine 500 --incremental
```

### 3. Run the Pre-Edit Guardrail Audit on Any Repository
```bash
python -m semantic_lod.real_repo_deploy demo_engine sym://src/projection.rs#OwnedProposal --markdown
```

### 4. Run the Full Multi-Condition Benchmark Suite
```bash
python -m semantic_lod.benchmark demo_engine --live-agent
```

---

## License

Apache License 2.0 — Copyright 2026 Brian Gansz. See [LICENSE](LICENSE).
