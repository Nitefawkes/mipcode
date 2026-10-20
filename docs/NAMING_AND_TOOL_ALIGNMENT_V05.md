# Semantic LOD v0.5: Dual-Layer Naming & Tool Consolidation Spec

## 1. The Core Principle: Engine vs. Interface

To make Semantic LOD accessible to standard backend/full-stack developers and LLM agent harnesses while preserving the powerful mathematical breakthroughs of computer graphics (Anisotropic Filtering, BRDF Shading, Raycasting, Ambient Occlusion), we adopt a **Dual-Layer Architecture**:

* **Under-the-Hood Engine & Whitepaper Brand:** The mathematical mechanics (e.g., BRDF dot-products, surface normals $\mathbf{N}$, light vectors $\mathbf{L}$, anisotropic rays, and ambient occlusion crevice weights) remain fully documented as the physical simulation model powering code comprehension.
* **Agent-Facing MCP Tool Interface:** The public MCP tools and parameters use intuitive developer verbs (`zoom`, `trace`, `focus`, `relevance_score`) so LLMs never burn reasoning tokens translating graphics jargon.

---

## 2. Consolidating the MCP Tool Surface (v0.4 $\rightarrow$ v0.5)

To prevent tool-selection friction in LLM agents, we fold the new capabilities into the existing intuitive primitives rather than inflating the tool count:

| Legacy v0.5 Graphics Name | Upgraded Public MCP Tool / Parameter | What It Does |
| --- | --- | --- |
| `lod_map()` | **`lod_map()`** / **`mip_map()`** | L0 World Map & global invariant index. |
| `lod_symbols(...)` | **`lod_symbols(...)`** / **`mip_symbols(...)`** | Cross-file declaration and call-reference search. |
| `lod_zoom(...)` + `lod_anisotropic_zoom` | **`lod_zoom(target, level, focus="")`** / **`mip_zoom(target, level, focus="")`** | If `focus` is omitted, performs standard isotropic L1/L2/L3 zoom. If `focus="..."` (e.g., `"persist cache"`), it automatically executes the **Anisotropic BRDF Shader**, ranking relevant symbols (including private `_helpers` at ordinal 48+) by relevance score while culling off-axis noise. |
| `lod_raycast(...)` | **`lod_trace(origin, direction, max_hops, focus="")`** / **`mip_trace(...)`** | Cross-file foveated call-graph slicer (Engine Alias: `lod_raycast` / `mip_raycast`). |
| `lod_blast_radius(...)` | **`lod_blast_radius(...)`** / **`mip_blast(...)`** | Upstream/downstream coupling & invariant impact. |

---

## 3. Drop-In Python Refactoring for `mcp_server.py`

Update `src/semantic_lod/mcp_server.py` to support both the clean public names and backwards-compatible aliases:

```python
# =====================================================================
# SEMANTIC LOD v0.5 PUBLIC MCP TOOL SURFACE (DUAL-LAYER ALIASING)
# =====================================================================

def lod_zoom(
    target: str,
    level: str = "L1",
    focus: Optional[str] = None,
    max_on_axis_symbols: int = 12,
    hydrate_top_k_l3: int = 2,
    byte_budget: int = 12000,
    session_id: str = "default",
    intent: str = "inspect",
    token_budget: int = 1500,
    workspace_dir: Optional[str] = None,
    force_bypass_gate: bool = False,
    compact_l2: bool = True,
    name_contains: str = "",
    offset: int = 0,
    max_lines: Optional[int] = None,
    line_offset: int = 0,
    deduplicate_session_l3: bool = True,
    max_l3_chars_per_symbol: int = 2400,
) -> Dict[str, Any]:
    """
    Progressive Hydration & Anisotropic Task Shading:
      - level='L1'/'L2'/'L3': Standard isometric views.
      - focus='...': If provided, runs the Anisotropic BRDF Shader to rank
        symbols by task relevance + deep-helper complexity weight, surfacing
        critical symbols (even private _helpers) instantly under the byte cap.
    """
    if focus:
        # Route to Anisotropic Shader implementation internally
        return lod_anisotropic_zoom(
            target=target,
            anisotropic_vector=focus,
            max_on_axis_symbols=max_on_axis_symbols,
            hydrate_top_k_l3=hydrate_top_k_l3,
            byte_budget=byte_budget,
            session_id=session_id,
            deduplicate_session_l3=deduplicate_session_l3,
            max_l3_chars_per_symbol=max_l3_chars_per_symbol,
        )
    # Standard v0.4 / v0.5 zoom logic...
    ...


def lod_trace(
    origin: str,
    direction: str = "both",
    max_hops: int = 2,
    ray_resolution: str = "L3",
    focus: Optional[str] = None,
    byte_budget: int = 12000,
    session_id: str = "default",
    deduplicate_session_l3: bool = True,
    max_l3_chars_per_symbol: int = 2400,
) -> Dict[str, Any]:
    """
    Cross-File Foveated Call-Graph Tracer (Engine Alias: lod_raycast).
    Traces execution paths across module boundaries, rendering L3 source on the
    immediate spine and bundling host-module L1 invariants for MMU governance.
    """
    return lod_raycast(
        origin=origin,
        direction=direction,
        max_hops=max_hops,
        ray_resolution=ray_resolution,
        anisotropic_vector=focus or "",
        byte_budget=byte_budget,
        session_id=session_id,
        deduplicate_session_l3=deduplicate_session_l3,
        max_l3_chars_per_symbol=max_l3_chars_per_symbol,
    )
```

---

## 4. Documentation & Whitepaper Positioning

In your `BENCHMARK_REPORT.md` and repository README, present the graphics analogy proudly as the **intellectual foundation** under the hood:

> *"Semantic LOD v0.5 treats a codebase not as a flat file tree, but as a 3D surface geometry rendered under dynamic task lighting. By borrowing anisotropic filtering, BRDF shading, and ray-casting from computer graphics, .lod solves the multi-file byte-budget bottleneck—delivering blazing-fast cross-cutting execution traces while maintaining strict architectural governance through its stateful MMU."*
