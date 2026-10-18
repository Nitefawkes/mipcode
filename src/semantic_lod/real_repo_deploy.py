#!/usr/bin/env python3
"""
real_repo_deploy.py — Non-Destructive Real-Repository Deployment & Pre-Edit Guardrail CLI (v0.4)

Compiles any target repository into an external `.lod.json` container + incremental SQLite store
(leaving the target repository working tree 100% untouched on disk) and executes the Semantic LOD
Pre-Edit Interception Hook (`lod_verify_edit_eligibility`) and Single-Call Hybrid Guardrail
(`lod_pre_edit_guardrail`).

Usage:
  python -m semantic_lod.real_repo_deploy [repo_path] [target_module_or_symbol]
"""

import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from semantic_lod import compiler as builder
from semantic_lod import mcp_server as server


def run_repo_guardrail_audit(
    repo_path: str = "demo_engine",
    target: Optional[str] = None,
    include_tests: bool = True,
) -> dict:
    repo = Path(repo_path).resolve()
    if not repo.exists():
        raise FileNotFoundError(f"Repository path not found: {repo}")

    artifacts_dir = Path(__file__).resolve().parent.parent.parent / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    slug = repo.name.lower().replace(" ", "_")
    out_lod = artifacts_dir / f"{slug}.lod.json"
    out_sqlite = artifacts_dir / f"{slug}.lod.sqlite"

    # 1. Cold Full Build
    if out_lod.exists():
        out_lod.unlink()
    if out_sqlite.exists():
        out_sqlite.unlink()

    t0 = time.perf_counter()
    container_cold = builder.build_lod_container(
        str(repo),
        output_file=str(out_lod),
        sqlite_file=str(out_sqlite),
        default_l0_token_budget=800,
        incremental=False,
    )
    cold_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    # 2. Warm Incremental SQLite Rebuild (0 dirty files)
    container_warm = builder.build_lod_container(
        str(repo),
        output_file=str(out_lod),
        sqlite_file=str(out_sqlite),
        default_l0_token_budget=800,
        incremental=True,
    )
    warm_ms = container_warm["incremental_stats"]["elapsed_ms"]

    server.LOD_PATH = out_lod
    server.SESSION_STATE.clear()

    blueprints = container_cold.get("l1_blueprints", {})
    if not target:
        top_mods = list(container_cold["l0_world_map"]["top_modules_by_centrality"].keys())
        target = top_mods[0] if top_mods else next(iter(blueprints.keys()), "")

    mod_id = target.split("#", 1)[0].replace("sym://", "mod://")
    if not mod_id.startswith("mod://"):
        mod_id = f"mod://{mod_id.lstrip('./')}"
    rel_file = mod_id.replace("mod://", "")

    sid = "guardrail_audit"
    gate1 = server.lod_verify_edit_eligibility(
        file_path=rel_file,
        session_id=sid,
        intent="modify_signature",
    )
    gate2 = server.lod_verify_edit_eligibility(
        file_path=rel_file,
        session_id=sid,
        intent="modify_signature",
    )
    guardrail = server.lod_pre_edit_guardrail(
        target=target,
        session_id=sid,
        include_tests=include_tests,
    )
    gate_after = server.lod_verify_edit_eligibility(
        file_path=rel_file,
        session_id=sid,
        intent="modify_signature",
    )

    json_bytes = guardrail.get("json_bytes", 1)
    md_bytes = guardrail.get("compact_markdown_bytes", 1)
    reduction_pct = round((1.0 - (md_bytes / max(json_bytes, 1))) * 100.0, 1)

    return {
        "project": repo.name,
        "git_commit": container_cold.get("git_commit"),
        "total_modules_indexed": len(blueprints),
        "compilation_performance": {
            "cold_full_build_ms": cold_ms,
            "warm_incremental_sqlite_ms": warm_ms,
            "speedup_factor": round(cold_ms / max(warm_ms, 0.1), 1),
            "reused_modules_count": container_warm["incremental_stats"]["reused_modules_count"],
        },
        "target_evaluated": target,
        "pre_edit_hook_gates": {
            "gate1_cold_edit": {
                "eligible": gate1["eligible"],
                "exit_code": gate1["exit_code"],
                "status": gate1["status"],
            },
            "gate2_before_blast_radius": {
                "eligible": gate2["eligible"],
                "exit_code": gate2["exit_code"],
                "status": gate2["status"],
            },
            "gate_after_guardrail": {
                "eligible": gate_after["eligible"],
                "exit_code": gate_after["exit_code"],
                "status": gate_after["status"],
                "freshness": gate_after.get("freshness", {}).get("status"),
            },
        },
        "guardrail_summary": {
            "production_callers_count": guardrail.get("production_callers_count", 0),
            "test_callers_count": guardrail.get("test_callers_count", 0),
            "non_call_references_count": len(guardrail.get("non_call_references", [])),
            "referenced_constants": list(guardrail.get("referenced_module_constants", {}).keys()),
            "json_bytes": json_bytes,
            "compact_markdown_bytes": md_bytes,
            "markdown_byte_reduction_pct": reduction_pct,
            "compact_markdown": guardrail.get("compact_markdown", ""),
        },
    }


def main_cli() -> None:
    pos = [a for a in sys.argv[1:] if not a.startswith("--")]
    repo_arg = pos[0] if len(pos) > 0 else "demo_engine"
    target_arg = pos[1] if len(pos) > 1 else None
    include_tests = "--no-tests" not in sys.argv[1:]
    result = run_repo_guardrail_audit(repo_arg, target=target_arg, include_tests=include_tests)
    if "--markdown" in sys.argv[1:]:
        print(result["guardrail_summary"]["compact_markdown"])
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main_cli()
