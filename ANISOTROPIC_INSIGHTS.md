
# Semantic LOD v0.5: The Anisotropic Ray-Slicer

## The Origin Story: Why v0.4 Needed an Upgrade
In v0.4, Semantic LOD implemented polyglot parsing, an incremental SQLite cache, and an active MMU (Memory Management Unit) that forced agents to hydrate architectural invariants before editing code. However, empirical testing (by the Codex agent track) on large 700+ file repositories under strict 24 KB context limits revealed a critical bottleneck:

1.  **The "Small-Scope Tax" & JSON Framing:** The structured JSON envelope of the LOD payloads introduced massive overhead. Reading L0 + L1 + L2 for a multi-hop execution trace frequently triggered `response_too_large` refusals.
2.  **The Cross-File Ray Problem:** When an agent needed to trace a deeply nested call sequence (e.g., `A -> B -> C -> D`), standard `lod_zoom` forced isotropic (uniform) resolution loading at every hop, wasting payload space on irrelevant sibling functions within the intersected modules.

## The Solution: Anisotropic Ray-Slicing (`lod_raycast`)
Inspired by 3D graphics rendering (Anisotropic Filtering and Raycasting), v0.5 introduces `lod_raycast` to fundamentally shift `.lod` from a per-file zoom lens into a cross-cutting execution tracer.

### How It Works Mechanically
`lod_raycast` shoots a ray from an origin symbol, traversing the pre-compiled AST `outgoing_calls` and `referenced_identifiers` across file boundaries. It dynamically adjusts resolution based on distance from the ray:

1.  **Foveated Resolution (Hop 0 and 1):** The origin symbol and its immediate neighbors (the "Fovea") are rendered at maximum `L3` resolution, embedding the exact raw source lines directly into the payload.
2.  **Peripheral Resolution (Hop 2+):** Further down the call chain, the ray drops to `L2` resolution (signatures, line ranges, and immediate outgoing calls).
3.  **Off-Axis Culling:** Sibling functions inside the touched modules that do not lie on the direct execution path are aggressively culled into a single integer count (e.g., `"culled_off_axis_symbols": 47`).

### Bridging the Gap: Finding Private Helpers
This approach directly solves the "H03 Private Helper Blind Spot." If a public entry point calls a deeply nested private helper (e.g., `_persist_research_result` at ordinal 48), the ray marches directly into it, elevating it to `L3` visibility while culling the preceding 47 unrelated symbols.

### Fusing with the Polyglot MMU
Crucially, `lod_raycast` maintains architectural governance. It bundles the `L1` architectural invariants (`DECLARED_RULE`, `HEURISTIC_HAZARD`) of *every* host module the ray touches. By registering these modules as hydrated in the agent's session state, it satisfies the `SEMANTIC_PAGE_FAULT_TRIGGERED` state gate, granting surgical `L3` access without bypassing the architecture's negative constraints.
