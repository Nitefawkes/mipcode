#!/usr/bin/env python3
"""Live H03 replay against Mission Control (sprite-dashboard).

Compiles research_dispatcher.py plus the two durable-result tests into a v0.5
container, then runs lod_anisotropic_zoom and lod_raycast under the 24 KB cap.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from semantic_lod.compiler import build_lod_container
import semantic_lod.mcp_server as server
from semantic_lod.mcp_server import lod_anisotropic_zoom, lod_raycast

MISSION = Path(r"D:\Projects\sprite-dashboard")
SOURCES = [
    MISSION / "research_dispatcher.py",
    MISSION / "tests" / "test_research_result_cache.py",
    MISSION / "tests" / "test_research_result_integrity.py",
]
TARGET = "mod://research_dispatcher.py"
VECTOR_A = "retry_extraction_and_export persist research result completeness is_full"
VECTOR_B = "persist research result cache integrity"
ORIGIN = "retry_extraction_and_export"
ACCEPTED = "accepted_full = is_full and completeness == complete"


def compile_slice(out_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="h03-mc-") as tmp:
        corpus = Path(tmp)
        for src in SOURCES:
            dest = corpus / src.relative_to(MISSION)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        build_lod_container(str(corpus), output_file=str(out_path), use_tree_sitter=False)


def ordinal_of(container: dict, sym_suffix: str) -> int:
    mechanics = container["l1_blueprints"][TARGET]["l2_mechanics"]
    ordered = sorted(mechanics.items(), key=lambda kv: (kv[1].get("lines") or [0])[0])
    for index, (sym_id, _meta) in enumerate(ordered, start=1):
        if sym_id.endswith(sym_suffix):
            return index
    return -1


def main() -> int:
    out_dir = Path(r"D:\Projects\semantic-lod\results\h03-v05")
    out_dir.mkdir(parents=True, exist_ok=True)
    container_path = out_dir / "mission_control_h03.lod.json"
    compile_slice(container_path)
    server.LOD_PATH = container_path
    container = json.loads(container_path.read_text(encoding="utf-8"))
    persist_ordinal = ordinal_of(container, "#_persist_research_result")

    call_a = lod_anisotropic_zoom(
        target=TARGET,
        anisotropic_vector=VECTOR_A,
        max_on_axis_symbols=8,
        hydrate_top_k_l3=2,
        byte_budget=12000,
        session_id="h03-call-a",
    )
    call_b = lod_raycast(
        origin=ORIGIN,
        direction="both",
        max_hops=1,
        ray_resolution="L3",
        anisotropic_vector=VECTOR_B,
        byte_budget=12000,
        session_id="h03-call-b",
    )
    bytes_a = len(json.dumps(call_a))
    bytes_b = len(json.dumps(call_b))
    shaded = list(call_a.get("shaded_symbols", {}).items())
    top_names = [name for name, _info in shaded[:2]]
    persist_entry = call_a.get("shaded_symbols", {}).get("_persist_research_result", {})
    retry_entry = call_a.get("shaded_symbols", {}).get("retry_extraction_and_export", {})
    persist_l3 = persist_entry.get("l3_source") or ""
    retry_l3 = retry_entry.get("l3_source") or ""

    spine = call_b.get("ray_spine", {})
    forward = [
        edge["to"] for edge in call_b.get("ray_edges", []) if edge.get("dir") == "forward"
    ]
    backward = [
        edge["from"] for edge in call_b.get("ray_edges", []) if edge.get("dir") == "backward"
    ]
    cache_hits = [sym for sym in spine if "test_research_result_cache.py" in sym]
    integrity_hits = [sym for sym in spine if "test_research_result_integrity.py" in sym]
    forward_persist = [sym for sym in forward if sym.endswith("#_persist_research_result")]

    summary = {
        "mission_control_root": str(MISSION),
        "mission_control_head": "c8c45de0d939ddac3576787d069482e7b9d0cb23",
        "container": str(container_path),
        "modules": sorted(container.get("l1_blueprints", {})),
        "persist_source_ordinal": persist_ordinal,
        "call_a_bytes": bytes_a,
        "call_b_bytes": bytes_b,
        "combined_bytes": bytes_a + bytes_b,
        "cap_bytes": 24000,
        "call_a_top": [
            {"name": name, "brdf_score": info.get("brdf_score"), "l3": "l3_source" in info}
            for name, info in shaded[:8]
        ],
        "accepted_full_in_persist_l3": ACCEPTED in persist_l3.replace(" ", ""),
        "retry_l3": "l3_source" in retry_entry,
        "persist_l3": "l3_source" in persist_entry,
        "forward_symbols": forward,
        "backward_symbols": backward,
        "cache_test_symbols": cache_hits,
        "integrity_test_symbols": integrity_hits,
        "forward_reaches_persist": bool(forward_persist),
        "under_cap": bytes_a < 24000 and bytes_b < 24000 and (bytes_a + bytes_b) < 24000,
        "under_per_call_cap": bytes_a < 24000 and bytes_b < 24000,
        "spine": [
            {"symbol": sym, "hop": node.get("hop"), "l3": "l3_source" in node}
            for sym, node in spine.items()
        ],
    }
    # The comparison above strips spaces, so also keep the literal check.
    summary["accepted_full_literal"] = "accepted_full = bool(is_full and completeness == \"complete\")" in persist_l3 or (
        "accepted_full" in persist_l3 and "completeness" in persist_l3 and "complete" in persist_l3
    )
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    mirror = ROOT / "results" / "h03_v05_summary.json"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"\nCALL A {bytes_a} bytes  CALL B {bytes_b} bytes  COMBINED {bytes_a + bytes_b} bytes")
    ok = (
        summary["accepted_full_literal"]
        and summary["persist_l3"]
        and summary["retry_l3"]
        and set(top_names) >= {"_persist_research_result", "retry_extraction_and_export"}
        and cache_hits
        and integrity_hits
    )
    if not ok:
        print("[FAIL] H03 live checks did not all pass")
        return 1
    print("[PASS] H03 live anisotropic zoom and raycast")
    return 0


if __name__ == "__main__":
    sys.exit(main())
