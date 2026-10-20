#!/usr/bin/env python3
"""
Test harness for lod_raycast.

Run from the repo root:

    python test_raycast.py
    python test_raycast.py dispatch_batch

Shoots a foveated L3 ray through the v0.4 call graph and checks that the payload
stays under the 24 KB retrieval cap, hop 0/1 carry source, hop 2+ stays at L2,
and `_record_session_l1_hydration` satisfies Fog-of-War only for modules on the ray.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import semantic_lod.mcp_server as server
from semantic_lod.mcp_server import get_session, lod_raycast, lod_zoom

# `mcp_server` treats argv[1] as a container path. A symbol argument is not one.
_CONTAINER = ROOT / "demo_engine" / ".mip.json"
if not server.LOD_PATH.exists():
    server.LOD_PATH = _CONTAINER


def run_test() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "dispatch_batch"
    session_id = "raycast-v05"

    print(f"\n[RAYCAST TEST] Shooting ray from: {target}")
    print(f"[RAYCAST TEST] Container: {server.LOD_PATH}")
    print("-" * 50)

    result = lod_raycast(
        origin=target,
        direction="both",
        max_hops=2,
        ray_resolution="L3",
        byte_budget=15000,
        session_id=session_id,
    )

    if "error" in result:
        print(f"Error: {result['error']}")
        return 1

    print(f"Touched Modules (L1 Invariants Only): {len(result.get('touched_modules_l1_invariants', {}))}")
    for mod_id, data in result.get("touched_modules_l1_invariants", {}).items():
        rules = data.get("declared_rules") or data.get("invariants") or []
        print(
            f"  - {mod_id}: {data['on_ray_symbols']} symbols on ray, "
            f"{data['culled_off_axis_symbols']} culled, {len(rules)} DECLARED_RULE invariants."
        )

    print(f"\nRay Edges Discovered: {len(result.get('ray_edges', []))}")
    print("\nSpine Nodes (Foveated Resolution):")
    hop_counts = {0: 0, 1: 0, 2: 0}
    for sym_id, node in result.get("ray_spine", {}).items():
        hop = node.get("hop")
        has_l3 = "l3_source" in node
        truncated = bool(node.get("truncated_by_byte_budget"))
        if isinstance(hop, int):
            hop_counts[hop] = hop_counts.get(hop, 0) + 1
        print(f"  - [Hop {hop}] {sym_id} (L3 rendered: {has_l3}, truncated: {truncated})")

    raw_bytes = len(json.dumps(result))
    print("\n" + "-" * 50)
    print(f"TOTAL PAYLOAD SIZE: {raw_bytes} bytes")

    failures = []
    if raw_bytes >= 24000:
        failures.append(f"payload {raw_bytes} bytes exceeded the 24 KB cap")
    else:
        print("[PASS] Well under the 24 KB Semantic LOD cap!")

    spine = result.get("ray_spine", {})
    l3_near = [
        sym for sym, node in spine.items()
        if node.get("hop", 99) <= 1 and not node.get("truncated_by_byte_budget") and "l3_source" in node and node["l3_source"]
    ]
    l2_far = [
        sym for sym, node in spine.items()
        if node.get("hop", 0) >= 2 and "l3_source" not in node and not node.get("truncated_by_byte_budget")
    ]
    leaked_l3 = [
        sym for sym, node in spine.items()
        if node.get("hop", 0) >= 2 and "l3_source" in node
    ]
    if not l3_near:
        failures.append("hop 0/1 did not render L3 source")
    else:
        print(f"[PASS] Foveated L3 on {len(l3_near)} hop 0/1 node(s).")
    if leaked_l3:
        failures.append(f"hop 2+ leaked L3 source: {leaked_l3}")
    if not l2_far:
        failures.append("no hop 2+ spine node dropped to an L2 signature")
    else:
        print(f"[PASS] Hop 2+ stayed at L2 for {len(l2_far)} node(s).")

    session = get_session(session_id)
    touched = result.get("touched_modules_l1_invariants", {})
    missing_l1 = [mod_id for mod_id in touched if mod_id not in session["l1_hydrated_modules"]]
    if missing_l1:
        failures.append(f"_record_session_l1_hydration missed {missing_l1}")
    else:
        print(f"[PASS] L1 hydration recorded for {len(touched)} touched module(s).")

    origin_mods = {server._target_to_mod_id(sym) for sym, node in spine.items() if node.get("hop") == 0}
    for mod_id in origin_mods:
        bundle = touched.get(mod_id) or {}
        if bundle.get("culled_off_axis_symbols", 0) <= 0:
            failures.append(f"origin module {mod_id} did not cull off-axis siblings")
        else:
            print(f"[PASS] Culled {bundle['culled_off_axis_symbols']} off-axis symbol(s) in {mod_id}.")
        if not (bundle.get("declared_rules") or bundle.get("invariants")):
            failures.append(f"origin module {mod_id} bundled no DECLARED_RULE invariants")

    container = server.load_container()
    untouched = next(
        (mod_id for mod_id in container.get("l1_blueprints", {}) if mod_id not in touched),
        None,
    )
    if untouched is None:
        failures.append("ray touched every module; cannot prove the page-fault gate still fires")
    else:
        fault = lod_zoom(untouched, level="L3", session_id=session_id)
        if fault.get("status") != "SEMANTIC_PAGE_FAULT_TRIGGERED":
            failures.append(
                f"untouched {untouched} returned {fault.get('status')}, expected SEMANTIC_PAGE_FAULT_TRIGGERED"
            )
        else:
            print(f"[PASS] Untouched {untouched} still raises SEMANTIC_PAGE_FAULT_TRIGGERED.")
        touched_mod = next(iter(touched))
        opened = lod_zoom(touched_mod, level="L3", session_id=session_id)
        if opened.get("status") != "OK":
            failures.append(
                f"ray-hydrated {touched_mod} returned {opened.get('status')}, expected OK after L1 hook"
            )
        else:
            print(f"[PASS] Ray-hydrated {touched_mod} opens at L3 without a second page fault.")

    if failures:
        print("\n[FAIL]")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("\n[PASS] Anisotropic ray respects foveation, the 24 KB cap, and Fog-of-War gating.")
    return 0


if __name__ == "__main__":
    sys.exit(run_test())
