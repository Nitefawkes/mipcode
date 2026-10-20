#!/usr/bin/env python3
"""
Test suite for Semantic LOD v0.5:

1. lod_anisotropic_zoom (BRDF semantic shading + private-helper promotion)
2. lod_raycast (cross-file foveated L3/L2 ray-slicing)

Run from the repo root:

    python test_anisotropic_v05.py
    python test_anisotropic_v05.py mod://src/router.rs "dispatch apply verify" dispatch_batch
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import semantic_lod.mcp_server as server
from semantic_lod.mcp_server import (
    _compute_brdf_anisotropic_score,
    get_session,
    lod_anisotropic_zoom,
    lod_raycast,
    lod_zoom,
)

if not server.LOD_PATH.exists():
    server.LOD_PATH = ROOT / "demo_engine" / ".mip.json"

SESSION = "anisotropic-v05"


def main() -> int:
    data = server.load_container()
    modules = list(data.get("l1_blueprints", {}).keys())
    if not modules:
        print("No modules in container")
        return 1

    sample_mod = sys.argv[1] if len(sys.argv) > 1 else "mod://src/router.rs"
    sample_vec = sys.argv[2] if len(sys.argv) > 2 else "dispatch apply verify persist complete"
    sample_origin = sys.argv[3] if len(sys.argv) > 3 else "dispatch_batch"
    failures = []

    print(f"=== 1. Testing lod_anisotropic_zoom on {sample_mod} (vector='{sample_vec}') ===")
    aniso_res = lod_anisotropic_zoom(
        target=sample_mod,
        anisotropic_vector=sample_vec,
        max_on_axis_symbols=8,
        hydrate_top_k_l3=2,
        byte_budget=12000,
        session_id=SESSION,
    )
    if "error" in aniso_res:
        print(f"Error: {aniso_res['error']}")
        return 1

    aniso_bytes = len(json.dumps(aniso_res))
    print(
        f"On-axis rendered: {aniso_res.get('on_axis_symbols_rendered')}, "
        f"Culled off-axis: {aniso_res.get('culled_off_axis_symbols')}"
    )
    shaded = list(aniso_res.get("shaded_symbols", {}).items())
    for sym_name, info in shaded:
        print(f"  * {sym_name} -> BRDF score: {info.get('brdf_score')}, L3 included: {'l3_source' in info}")
    print(f"Anisotropic Zoom Payload: {aniso_bytes} bytes (<24KB pass: {aniso_bytes < 24000})\n")

    if aniso_bytes >= 24000:
        failures.append(f"anisotropic zoom payload {aniso_bytes} bytes exceeded 24 KB")
    if not aniso_res.get("l1_invariants"):
        failures.append("anisotropic zoom bundled no L1 invariants")
    if aniso_res.get("culled_off_axis_symbols", 0) <= 0:
        failures.append("anisotropic zoom did not cull off-axis symbols")
    if not shaded or "dispatch_batch" not in shaded[0][0]:
        failures.append(f"dispatch_batch was not promoted to the first on-axis symbol (got {shaded[:3]})")
    elif "l3_source" not in shaded[0][1]:
        failures.append("top on-axis symbol did not receive an L3 slice")
    else:
        print("[PASS] Last-in-file dispatch_batch ranked first and received L3.")

    # H03 shape: a deep private helper matching the vector outranks an early unrelated symbol.
    vector = ["persist", "research", "result", "completeness"]
    helper_score = _compute_brdf_anisotropic_score(
        "sym://research_dispatcher.py#_persist_research_result",
        {
            "signature": "def _persist_research_result(self, payload: dict) -> None",
            "docstring": "Persist the completed research result into the cache.",
            "outgoing_calls": ["write_cache", "checksum"],
            "referenced_identifiers": ["completeness", "payload", "cache"],
            "lines": [900, 960],
        },
        vector,
        ["INVARIANT: _persist_research_result must record completeness before ack."],
    )
    early_score = _compute_brdf_anisotropic_score(
        "sym://research_dispatcher.py#format_banner",
        {
            "signature": "def format_banner() -> str",
            "outgoing_calls": [],
            "referenced_identifiers": ["banner"],
            "lines": [1, 4],
        },
        vector,
        [],
    )
    print(f"Ordinal-48 helper score {helper_score} vs source-order sibling {early_score}")
    if helper_score < 1.0 or helper_score <= early_score:
        failures.append("private helper _persist_research_result was not promoted over an off-axis sibling")
    else:
        print("[PASS] _persist_research_result outranks an early off-axis sibling.")

    print(f"=== 2. Testing lod_raycast from origin='{sample_origin}' (vector='{sample_vec}') ===")
    ray_res = lod_raycast(
        origin=sample_origin,
        direction="both",
        max_hops=2,
        ray_resolution="L3",
        anisotropic_vector=sample_vec,
        byte_budget=12000,
        session_id=SESSION,
    )
    if "error" in ray_res:
        print(f"Error: {ray_res['error']}")
        return 1

    ray_bytes = len(json.dumps(ray_res))
    print(f"Touched modules: {list(ray_res.get('touched_modules_l1_invariants', {}).keys())}")
    for sym_id, node in ray_res.get("ray_spine", {}).items():
        print(f"  * [Hop {node.get('hop')}] {sym_id} -> BRDF: {node.get('brdf_score')}, L3: {'l3_source' in node}")
    print(f"Raycast Payload: {ray_bytes} bytes (<24KB pass: {ray_bytes < 24000})")

    if ray_bytes >= 24000:
        failures.append(f"raycast payload {ray_bytes} bytes exceeded 24 KB")
    leaked = [
        sym for sym, node in ray_res.get("ray_spine", {}).items()
        if node.get("hop", 0) >= 2 and "l3_source" in node
    ]
    near = [
        sym for sym, node in ray_res.get("ray_spine", {}).items()
        if node.get("hop", 99) <= 1 and node.get("l3_source") and not node.get("truncated_by_byte_budget")
    ]
    if leaked:
        failures.append(f"hop 2+ leaked L3: {leaked}")
    if not near:
        failures.append("hop 0/1 did not render L3")
    if not ray_res.get("anisotropic_vector"):
        failures.append("raycast dropped the anisotropic vector")

    session = get_session(SESSION)
    touched = set(ray_res.get("touched_modules_l1_invariants", {}))
    touched.add(aniso_res["target"])
    missing = [mod_id for mod_id in touched if mod_id not in session["l1_hydrated_modules"]]
    if missing:
        failures.append(f"L1 hydration hook missed {missing}")
    else:
        print(f"[PASS] L1 hydration recorded for {len(touched)} module(s).")

    untouched = next((mod_id for mod_id in modules if mod_id not in touched), None)
    if untouched is None:
        failures.append("every module was hydrated; page-fault gate cannot be checked")
    else:
        fault = lod_zoom(untouched, level="L3", session_id=SESSION)
        if fault.get("status") != "SEMANTIC_PAGE_FAULT_TRIGGERED":
            failures.append(f"untouched {untouched} returned {fault.get('status')}")
        else:
            print(f"[PASS] Untouched {untouched} still raises SEMANTIC_PAGE_FAULT_TRIGGERED.")

    if failures:
        print("\n[FAIL]")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n[PASS] Anisotropic zoom and raycast stay under 24 KB and keep the MMU gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
