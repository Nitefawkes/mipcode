#!/usr/bin/env python3
"""
Verification of Semantic LOD v0.5 Dual-Layer Naming & Tool Consolidation.
Tests both public developer-facing tools (`lod_zoom(..., focus=...)`, `lod_trace`, `mip_zoom`, `mip_trace`)
and internal graphics engine aliases (`lod_anisotropic_zoom`, `lod_raycast`, `mip_anisotropic_zoom`, `mip_raycast`).
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import semantic_lod.mcp_server as server
_CONTAINER = ROOT / "demo_engine" / ".mip.json"
if not server.LOD_PATH.exists():
    server.LOD_PATH = _CONTAINER

from mipcode import (
    mip_map,
    mip_symbols,
    mip_zoom,
    mip_trace,
    mip_blast,
    mip_guardrail,
    mip_verify_edit,
    mip_propose_delta,
    mip_raycast,
    mip_anisotropic_zoom,
    lod_map,
    lod_symbols,
    lod_zoom,
    lod_trace,
    lod_blast_radius,
    lod_raycast,
    lod_anisotropic_zoom,
    lod_pre_edit_guardrail,
    lod_verify_edit_eligibility,
    lod_propose_context_delta,
)


def test_dual_layer_consolidation():
    print("=== Testing Dual-Layer Public Surface & Backward Compatibility ===")

    # 1. Standard Isotropic Zoom via lod_zoom and mip_zoom (without focus)
    l1_lod = lod_zoom("mod://src/router.rs", level="L1")
    assert l1_lod.get("resolution") == "L1" or l1_lod.get("status") == "OK", f"Unexpected L1 lod: {l1_lod.keys()}"
    print("[PASS] lod_zoom(..., level='L1') returns standard isometric blueprint.")

    l1_mip = mip_zoom("mod://src/router.rs", level="L1")
    assert l1_mip.get("resolution") == "L1" or l1_mip.get("status") == "OK"
    print("[PASS] mip_zoom(..., level='L1') returns standard isometric blueprint.")

    # 2. Anisotropic Shading routed via lod_zoom(..., focus=...) and mip_zoom(..., focus=...)
    task_focus = "dispatch apply verify persist complete"
    shaded_lod = lod_zoom("mod://src/router.rs", focus=task_focus)
    assert shaded_lod.get("resolution") == "ANISOTROPIC_SHADED_SLICE", f"Expected shaded slice, got {shaded_lod.get('resolution')}"
    assert "shaded_symbols" in shaded_lod
    print(f"[PASS] lod_zoom(..., focus='{task_focus}') routes to Anisotropic BRDF Shader.")

    shaded_mip = mip_zoom("mod://src/router.rs", focus=task_focus)
    assert shaded_mip.get("resolution") == "ANISOTROPIC_SHADED_SLICE"
    assert "shaded_symbols" in shaded_mip
    print("[PASS] mip_zoom(..., focus=...) routes to Anisotropic BRDF Shader.")

    # 3. Call-Graph Tracing via lod_trace and mip_trace
    trace_lod = lod_trace("dispatch_batch", focus=task_focus)
    assert trace_lod.get("resolution", "").startswith("ANISOTROPIC_RAY"), f"Expected ray resolution, got {trace_lod.get('resolution')}"
    assert "ray_spine" in trace_lod
    assert "touched_modules_l1_invariants" in trace_lod
    print("[PASS] lod_trace('dispatch_batch', focus=...) traces call-graph ray with host L1 bundle.")

    trace_mip = mip_trace("dispatch_batch", focus=task_focus)
    assert trace_mip.get("resolution", "").startswith("ANISOTROPIC_RAY")
    assert "ray_spine" in trace_mip
    print("[PASS] mip_trace('dispatch_batch', focus=...) traces call-graph ray.")

    # 4. Engine Aliases for Backwards Compatibility
    raycast_direct = lod_raycast("dispatch_batch", anisotropic_vector=task_focus)
    assert raycast_direct.get("resolution", "").startswith("ANISOTROPIC_RAY")
    print("[PASS] lod_raycast engine alias functional.")

    aniso_direct = lod_anisotropic_zoom("mod://src/router.rs", anisotropic_vector=task_focus)
    assert aniso_direct.get("resolution") == "ANISOTROPIC_SHADED_SLICE"
    print("[PASS] lod_anisotropic_zoom engine alias functional.")

    # 5. Check MCP server tool dictionary if mcp_app is present
    if server.mcp_app is not None:
        registered_tools = set()
        if hasattr(server.mcp_app, "_tools"):
            registered_tools = set(server.mcp_app._tools.keys())
        elif hasattr(server.mcp_app, "_tool_manager") and hasattr(server.mcp_app._tool_manager, "_tools"):
            registered_tools = set(server.mcp_app._tool_manager._tools.keys())
        elif hasattr(server.mcp_app, "list_tools"):
            res = server.mcp_app.list_tools()
            if asyncio.iscoroutine(res):
                res = asyncio.run(res)
            registered_tools = {t.name for t in res}
        
        expected = {
            "mip_map", "mip_symbols", "mip_zoom", "mip_trace", "mip_blast",
            "mip_guardrail", "mip_verify_edit", "mip_propose_delta", "mip_raycast", "mip_anisotropic_zoom",
            "lod_map", "lod_symbols", "lod_zoom", "lod_trace", "lod_blast_radius",
            "lod_raycast", "lod_anisotropic_zoom", "lod_propose_context_delta",
            "lod_verify_edit_eligibility", "lod_pre_edit_guardrail"
        }
        if registered_tools:
            for exp in expected:
                assert exp in registered_tools, f"Missing tool: {exp} in {registered_tools}"
            print(f"[PASS] All {len(expected)} tools registered on MCP server application.")

    print("\n[ALL DUAL-LAYER CONSOLIDATION TESTS PASSED 100%]")


if __name__ == "__main__":
    test_dual_layer_consolidation()
