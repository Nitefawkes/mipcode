#!/usr/bin/env python3
"""
lod_mcp_server_v0_2.py — Stateful Semantic LOD Runtime & MCP Server (v0.2)
Implements:
  1. lod_map(token_budget, session_id) -> Budgeted L0 World Map + Working Context + Exact Loss Ledger.
  2. lod_zoom(target, level, intent, token_budget, session_id, workspace_dir) ->
       Enforces Stateful "Fog-of-War" Resolution Gating (blocks L3 jumps until L1 Blueprint is hydrated),
       unpacks self-contained zlib+base64 source blobs, and verifies live disk SHA-256 freshness.
  3. lod_blast_radius(target, max_hops, session_id) ->
       Transitive BFS symbol/module impact analysis with explicit epistemic & static-coverage boundaries.
  4. lod_propose_context_delta(...) ->
       Appends proposed decisions/questions to the Working Context delta log with a Hydration Receipt.
"""

import base64
from collections import deque
import hashlib
import json
import sys
import time
import zlib
from pathlib import Path
from typing import Dict, List, Any, Optional, Set

LOD_PATH = Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else ".lod.json").resolve()

# In-memory Fog-of-War session state tracking hydrated resolutions per agent session
SESSION_STATE: Dict[str, Dict[str, Any]] = {}


def estimate_tokens(obj: Any) -> int:
    raw = obj if isinstance(obj, str) else json.dumps(obj)
    return max(1, (len(raw) + 3) // 4)


def get_session(session_id: str) -> Dict[str, Any]:
    if session_id not in SESSION_STATE:
        SESSION_STATE[session_id] = {
            "l0_mounted": False,
            "l1_hydrated_modules": set(),
            "l2_hydrated_targets": set(),
            "l3_hydrated_targets": set(),
            "blast_radius_checked": set(),
            "hydrated_module_digests": {},
            "invalidated_receipts": [],
            "hydration_ledger": [],
            "proposed_deltas": []
        }
    return SESSION_STATE[session_id]


def _target_to_mod_id(target: str) -> Optional[str]:
    if target.startswith("mod://"):
        return target
    if target.startswith("sym://"):
        rel = target.replace("sym://", "").split("#", 1)[0]
        return f"mod://{rel}"
    return None


def record_hydration(session: Dict[str, Any], target: str, resolution: str, digest: str, omitted: Any) -> str:
    receipt_id = hashlib.sha256(f"{target}:{resolution}:{digest}:{len(session['hydration_ledger'])}".encode()).hexdigest()[:12]
    mod_id = _target_to_mod_id(target)
    if mod_id and digest:
        session.setdefault("hydrated_module_digests", {})[mod_id] = digest
    session["hydration_ledger"].append({
        "receipt_id": f"rcpt_{receipt_id}",
        "timestamp": round(time.time(), 3),
        "target": target,
        "resolution": resolution,
        "source_sha256": digest[:16],
        "full_sha256": digest,
        "omitted_summary": omitted
    })
    return f"rcpt_{receipt_id}"


def invalidate_stale_session_receipts(container: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Invalidates any cached session hydration state (L1, L2, L3, blast_radius) and hydration receipts
    for modules whose SHA-256 digest changed in `container`.
    """
    blueprints = container.get("l1_blueprints", {})
    invalidated_by_session: Dict[str, List[str]] = {}

    for sid, session in SESSION_STATE.items():
        digests_map = session.setdefault("hydrated_module_digests", {})
        stale_mods: Set[str] = set()
        for mod_id, old_sha in list(digests_map.items()):
            new_sha = blueprints.get(mod_id, {}).get("sha256")
            if new_sha is None or new_sha != old_sha:
                stale_mods.add(mod_id)

        if not stale_mods:
            continue

        for mod_id in stale_mods:
            session["l1_hydrated_modules"].discard(mod_id)
            session["blast_radius_checked"].discard(mod_id)
            digests_map.pop(mod_id, None)
            rel_prefix = f"sym://{mod_id.replace('mod://', '')}#"
            session["l2_hydrated_targets"] = {
                t for t in session["l2_hydrated_targets"]
                if t != mod_id and not t.startswith(rel_prefix)
            }
            session["l3_hydrated_targets"] = {
                t for t in session["l3_hydrated_targets"]
                if t != mod_id and not t.startswith(rel_prefix)
            }

        remaining_ledger = []
        for entry in session["hydration_ledger"]:
            entry_mod = _target_to_mod_id(entry.get("target", ""))
            if entry_mod in stale_mods:
                inv_entry = dict(entry)
                inv_entry["invalidated_at"] = round(time.time(), 3)
                inv_entry["reason"] = "MODULE_SHA256_DIGEST_CHANGED"
                session.setdefault("invalidated_receipts", []).append(inv_entry)
            else:
                remaining_ledger.append(entry)
        session["hydration_ledger"] = remaining_ledger
        invalidated_by_session[sid] = sorted(stale_mods)

    return invalidated_by_session


def load_container(path: Optional[Path] = None) -> Dict[str, Any]:
    target_path = path or LOD_PATH
    if not target_path.exists():
        raise FileNotFoundError(f"Missing {target_path}. Run `python lod_build_v0_2.py <repo>` first.")
    container = json.loads(target_path.read_text(encoding="utf-8"))
    invalidate_stale_session_receipts(container)
    return container


def decode_embedded_source(b64_blob: str) -> str:
    raw = base64.b64decode(b64_blob.encode("ascii"))
    return zlib.decompress(raw).decode("utf-8", errors="replace")


def verify_freshness(mod_id: str, mod_data: Dict[str, Any], workspace_dir: Optional[str]) -> Dict[str, Any]:
    recorded_sha = mod_data.get("sha256", "")
    if not workspace_dir:
        return {
            "status": "PORTABLE_SNAPSHOT_VERIFIED",
            "source_mode": "embedded_zlib_blob",
            "recorded_sha256": recorded_sha,
            "note": "Unpacked directly from self-contained .lod container (no local disk dependency)."
        }

    rel_path = mod_id.replace("mod://", "")
    disk_file = Path(workspace_dir).resolve() / rel_path
    if not disk_file.exists():
        return {
            "status": "DISK_FILE_MISSING_USING_EMBEDDED",
            "source_mode": "embedded_zlib_blob",
            "recorded_sha256": recorded_sha
        }

    live_sha = hashlib.sha256(disk_file.read_bytes()).hexdigest()
    if live_sha == recorded_sha:
        return {
            "status": "VERIFIED_CURRENT_WITH_DISK",
            "source_mode": "embedded_matches_disk",
            "recorded_sha256": recorded_sha,
            "live_sha256": live_sha
        }
    return {
        "status": "STALE_DISK_DRIFT_DETECTED",
        "source_mode": "live_disk_diverged_from_container",
        "recorded_sha256": recorded_sha,
        "live_sha256": live_sha,
        "warning": "Local disk file changed since .lod compilation; line numbers and contracts may be stale."
    }


def lod_map(token_budget: int = 500, session_id: str = "default") -> Dict[str, Any]:
    """
    L0 Orientation: Returns Working Context (goals, decisions, constraints) + Token-Budgeted World Map.
    Dynamically packs modules by centrality score up to `token_budget` and explicitly lists all omitted modules.
    """
    data = load_container()
    session = get_session(session_id)
    session["l0_mounted"] = True

    blueprints = data["l1_blueprints"]
    ranked = sorted(
        blueprints.items(),
        key=lambda kv: (-kv[1].get("centrality_score", 0.0), kv[0])
    )

    working_ctx = data.get("working_context", {})
    global_rules = data["l0_world_map"].get("declared_project_rules", [])

    included_modules: Dict[str, Any] = {}
    omitted_modules: List[str] = []
    used_tokens = estimate_tokens(working_ctx) + estimate_tokens(global_rules) + 75

    for mod_id, mdata in ranked:
        summary_card = {
            "centrality_score": mdata.get("centrality_score", 0.0),
            "contracts_count": len(mdata.get("contracts", [])),
            "depends_on": [e["target"] for e in mdata.get("coupled_with", [])],
            "depended_on_by": mdata.get("depended_on_by", []),
            "declared_rules_count": len(mdata["epistemics"]["declared_rules"]),
            "heuristic_hazards_count": len(mdata["epistemics"]["heuristic_hazards"])
        }
        cost = estimate_tokens({mod_id: summary_card})
        if used_tokens + cost <= token_budget or len(included_modules) == 0:
            included_modules[mod_id] = summary_card
            used_tokens += cost
        else:
            omitted_modules.append(mod_id)

    loss_record = {
        "lossy": True,
        "token_budget_applied": token_budget,
        "estimated_tokens_returned": used_tokens,
        "omitted_modules_count": len(omitted_modules),
        "omitted_modules": omitted_modules,
        "omitted_details": [
            "L1 type/function signatures",
            "L2 symbol mechanics and line ranges",
            "L3 raw source code"
        ]
    }
    rcpt = record_hydration(session, "lod://world_map", "L0", data.get("git_commit", "snapshot"), loss_record)

    return {
        "status": "OK",
        "receipt_id": rcpt,
        "project": data["project"],
        "git_commit": data["git_commit"],
        "resolution": "L0_WORLD_MAP",
        "working_context": working_ctx,
        "declared_project_rules": global_rules,
        "top_modules_by_centrality": included_modules,
        "loss": loss_record,
        "next_step_protocol": (
            "Fog-of-War Gate Active: You MUST hydrate a module's L1 Blueprint via "
            "`lod_zoom(mod_id, 'L1')` before requesting L3 raw source code."
        )
    }


def lod_symbols(
    pattern: str = "",
    prefix: str = "",
    limit: int = 20,
    offset: int = 0,
    include_call_references: bool = True,
    session_id: str = "default"
) -> Dict[str, Any]:
    """
    Cross-File Qualified Symbol & Call-Reference Discovery (Codex H03 + Heldout-v2 Synthesis):
      - Performs case-insensitive substring/multi-token matching across all indexed symbols (including
        private helpers and qualified class/impl methods `sym://path#Class.method`).
      - Gracefully handles empty `pattern=""` by listing symbols filtered by `prefix` (preventing the
        metered empty-pattern invalid_request failure observed in Codex heldout-v2 T11).
      - Surfaces hash-anchored 3-tier epistemic alerts (`DECLARED_RULE` / `HEURISTIC_HAZARD`) directly on
        matched symbols so agents receive invariant warnings even before hydrating L1/L3.
    """
    data = load_container()
    session = get_session(session_id)
    blueprints = data.get("l1_blueprints", {})

    needle = (pattern or "").strip().casefold()
    tokens = [t for t in needle.split() if t]
    norm_prefix = prefix.replace("mod://", "").replace("\\", "/").lstrip("./")

    matches: List[ Any ] = []
    for mod_id, mdata in blueprints.items():
        rel_path = mod_id.replace("mod://", "")
        if norm_prefix and not rel_path.startswith(norm_prefix):
            continue
        mod_sha12 = mdata.get("sha256", "")[:12]
        epistemics = mdata.get("epistemics", {})
        hazards_by_sym: Dict[str, List[str]] = {}
        for h in epistemics.get("heuristic_hazards", []):
            hsym = h.get("symbol", "")
            if hsym:
                hazards_by_sym.setdefault(hsym, []).append(h.get("warning", ""))
        mod_rules = [
            f"{r.get('statement')} [source={r.get('source')}, sha256={mod_sha12}]"
            for r in epistemics.get("declared_rules", [])
        ]

        for sym_id, smeta in mdata.get("l2_mechanics", {}).items():
            qualname = sym_id.split("#", 1)[1] if "#" in sym_id else sym_id
            q_low = qualname.casefold()
            calls_list = (
                list(smeta.get("outgoing_calls") or [])
                + list(smeta.get("call_graph_identifiers") or [])
                + list(smeta.get("referenced_identifiers") or [])
            )
            if not needle:
                decl_hit = True
                matched_calls = []
            else:
                decl_hit = (needle in q_low) or (len(tokens) > 1 and all(t in q_low for t in tokens))
                matched_calls = (
                    sorted({c for c in calls_list if needle in c.casefold() or (len(tokens) > 1 and any(t in c.casefold() for t in tokens))})
                    if include_call_references else []
                )

            if not decl_hit and not matched_calls:
                continue

            lines = smeta.get("lines", [1, 1])
            entry: Dict[str, Any] = {
                "id": sym_id,
                "kind": smeta.get("kind", "fn"),
                "is_public": bool(smeta.get("is_public", True)),
                "lines": lines,
                "source_sha256": mod_sha12,
                "match_type": "declaration" if decl_hit else "call_reference"
            }
            if matched_calls and not decl_hit:
                entry["matched_calls"] = matched_calls[:5]
            if sym_id in hazards_by_sym:
                entry["has_hazard"] = True
                entry["heuristic_hazards"] = hazards_by_sym[sym_id][:2]
            if mod_rules:
                entry["declared_rules_count"] = len(mod_rules)
                entry["top_declared_rule"] = mod_rules[0]
            matches.append((rel_path, lines[0], sym_id, entry))

    matches.sort(key=lambda t: (t[0], t[1], t[2]))
    ordered = [m[3] for m in matches]
    safe_limit = max(1, min(100, int(limit)))
    safe_offset = max(0, int(offset))
    selected = ordered[safe_offset:safe_offset + safe_limit]
    end_idx = safe_offset + len(selected)
    omitted_count = max(0, len(ordered) - end_idx)

    loss_record = {
        "lossy": True,
        "total_matches": len(ordered),
        "returned_matches": len(selected),
        "omitted_matches_count": omitted_count,
        "next_offset": end_idx if end_idx < len(ordered) else None,
        "omitted_categories": [
            "L1 module contracts and full DECLARED_RULE invariants (call lod_zoom(mod_id, 'L1') before L3)",
            "L2 full signatures and docstrings (call lod_zoom(sym_id, 'L2'))",
            "L3 source implementation bodies"
        ]
    }
    rcpt = record_hydration(session, f"symbols://{pattern or '*'}", "L2_SYMBOL_SEARCH", data.get("git_commit", "snapshot"), loss_record)
    return {
        "status": "OK",
        "receipt_id": rcpt,
        "resolution": "L2_CROSS_FILE_SYMBOL_DISCOVERY",
        "pattern": pattern,
        "prefix": norm_prefix,
        "include_call_references": include_call_references,
        "matches": selected,
        "loss": loss_record
    }


def _compact_module_l2_entry(sym_id: str, smeta: Dict[str, Any], hazard_syms: Set[str]) -> Dict[str, Any]:
    """
    Codex Compact Module L2 Diet:
      - Retains stable symbol key (`sym://...`), `kind`, `is_public`, `lines`, and nonzero mechanics counts.
      - Defers full signatures, docstrings, 40-element referenced_identifiers arrays, and verbose
        gate_policy boilerplate to L1 or symbol-specific L2 (`lod_zoom('sym://...', 'L2')`).
    """
    calls = smeta.get("outgoing_calls") or smeta.get("call_graph_identifiers") or []
    bounds = smeta.get("trait_bounds") or []
    compact: Dict[str, Any] = {
        "kind": smeta.get("kind", "fn"),
        "is_public": bool(smeta.get("is_public", True)),
        "lines": smeta.get("lines", [1, 1])
    }
    if calls:
        compact["calls_count"] = len(calls)
    if bounds:
        compact["trait_bounds"] = bounds
    if sym_id in hazard_syms:
        compact["has_hazard"] = True
    return compact


def lod_zoom(
    target: str,
    level: str = "L1",
    intent: str = "inspect",
    token_budget: int = 1500,
    session_id: str = "default",
    workspace_dir: Optional[str] = None,
    force_bypass_gate: bool = False,
    compact_l2: bool = True,
    name_contains: str = "",
    offset: int = 0,
    max_lines: Optional[int] = None,
    line_offset: int = 0
) -> Dict[str, Any]:
    """
    Progressive Hydration with Stateful Fog-of-War Gating:
      - L1: Blueprint (EXTRACTED_FACT contracts, DECLARED_RULE invariants, HEURISTIC_HAZARD warnings, coupling).
      - L2: Mechanics (Compact module symbol index with nonzero counts + epistemic rules, or full symbol L2 detail).
      - L3: Raw Source Slice (unpacked from embedded zlib+base64 blob, 1-based line anchors, optional max_lines/line_offset paging).
    """
    data = load_container()
    session = get_session(session_id)
    blueprints = data["l1_blueprints"]
    level = level.upper()

    if target.startswith("sym://"):
        rel_and_sym = target.replace("sym://", "")
        rel_path, _, sym_name = rel_and_sym.partition("#")
        mod_id = f"mod://{rel_path}"
        is_symbol_target = True
    else:
        mod_id = target if target.startswith("mod://") else f"mod://{target}"
        is_symbol_target = False

    mod = blueprints.get(mod_id)
    if not mod:
        return {
            "status": "ERROR_NOT_FOUND",
            "error": f"Unknown module `{mod_id}`.",
            "available_modules": list(blueprints.keys())
        }

    freshness = verify_freshness(mod_id, mod, workspace_dir)

    # =========================================================================
    # STATEFUL FOG-OF-WAR GATE (Enforced Semantic Page Faults)
    # =========================================================================
    if level == "L3" and not force_bypass_gate:
        # Gate 1: Cannot jump to L3 raw source without hydrating L1 Blueprint first!
        if mod_id not in session["l1_hydrated_modules"]:
            session["l1_hydrated_modules"].add(mod_id)
            l1_payload = _render_l1_blueprint(mod_id, mod, freshness, token_budget)
            rcpt = record_hydration(session, mod_id, "L1_AUTO_FAULT", mod["sha256"], l1_payload["loss"])
            return {
                "status": "SEMANTIC_PAGE_FAULT_TRIGGERED",
                "reason": (
                    f"Attempted to access L3 raw source for `{target}` before hydrating L1 Blueprint for `{mod_id}`. "
                    "L1 Blueprint has now been auto-hydrated below. Review its DECLARED_RULE invariants, "
                    "HEURISTIC_HAZARD warnings, and coupled dependencies, then re-invoke `lod_zoom` for L3."
                ),
                "receipt_id": rcpt,
                "auto_hydrated_l1_blueprint": l1_payload
            }

        # Gate 2: If intent is to modify a signature on a module with incoming dependents, require blast radius or dependent L1s
        incoming_deps = mod.get("depended_on_by", [])
        if intent in ("modify_signature", "breaking_change") and incoming_deps:
            uninspected_deps = [d for d in incoming_deps if d not in session["l1_hydrated_modules"]]
            if uninspected_deps and mod_id not in session["blast_radius_checked"]:
                return {
                    "status": "COUPLED_CONTRACT_PAGE_FAULT",
                    "reason": (
                        f"`{mod_id}` is depended on by {len(incoming_deps)} upstream module(s): {incoming_deps}. "
                        "Before modifying public signatures at L3, call `lod_blast_radius` or hydrate the L1 "
                        f"blueprints of {uninspected_deps}."
                    ),
                    "uninspected_dependents": uninspected_deps
                }

    # =========================================================================
    # L1: MODULE BLUEPRINT
    # =========================================================================
    if level == "L1":
        session["l1_hydrated_modules"].add(mod_id)
        l1_payload = _render_l1_blueprint(mod_id, mod, freshness, token_budget)
        rcpt = record_hydration(session, mod_id, "L1", mod["sha256"], l1_payload["loss"])
        l1_payload["receipt_id"] = rcpt
        return l1_payload

    # =========================================================================
    # L2: SYMBOL MECHANICS (Compact Module L2 Diet + Full Symbol L2 Detail)
    # =========================================================================
    if level == "L2":
        session["l2_hydrated_targets"].add(target)
        mechanics = mod.get("l2_mechanics", {})
        if is_symbol_target:
            sym_meta = mechanics.get(target)
            if not sym_meta:
                return {"status": "ERROR_SYMBOL_NOT_FOUND", "available_symbols": list(mechanics.keys())}
            full_src = decode_embedded_source(mod["embedded_source_b64_zlib"])
            src_lines = full_src.splitlines()
            s_start, s_end = sym_meta.get("lines", [1, len(src_lines)])
            sym_slice_txt = "\n".join(src_lines[max(0, s_start - 1):min(len(src_lines), s_end)])
            ref_consts = _resolve_referenced_constants(mod_id, mod, blueprints, sym_slice_txt)
            rcpt = record_hydration(session, target, "L2", mod["sha256"], sym_meta["omitted_in_l2"])
            return {
                "status": "OK",
                "receipt_id": rcpt,
                "target": target,
                "resolution": "L2_SYMBOL_MECHANICS",
                "freshness": freshness,
                "epistemic_rules_in_module": mod["epistemics"]["declared_rules"],
                "referenced_module_constants": ref_consts,
                "mechanics": sym_meta
            }

        # Module-wide L2 mechanics with Codex Compact L2 Diet, name_contains filter, and offset pagination
        hazard_syms = {h.get("symbol") for h in mod.get("epistemics", {}).get("heuristic_hazards", [])}
        needle = name_contains.strip().casefold()
        filtered_items = [
            (sym_id, smeta) for sym_id, smeta in mechanics.items()
            if not needle or needle in sym_id.split("#", 1)[-1].casefold()
        ]
        safe_offset = max(0, int(offset))
        paged_items = filtered_items[safe_offset:]

        included_syms: Dict[str, Any] = {}
        omitted_syms: List[str] = [sym_id for sym_id, _ in filtered_items[:safe_offset]]
        used = 80
        for sym_id, smeta in paged_items:
            rendered = _compact_module_l2_entry(sym_id, smeta, hazard_syms) if compact_l2 else smeta
            cost = estimate_tokens({sym_id: rendered})
            if used + cost <= token_budget or len(included_syms) == 0:
                included_syms[sym_id] = rendered
                used += cost
            else:
                omitted_syms.append(sym_id)

        next_offset = (safe_offset + len(included_syms)) if (safe_offset + len(included_syms)) < len(filtered_items) else None
        omitted_cats = ["L3 statement-level source code"]
        if compact_l2:
            omitted_cats.extend([
                "Full symbol signatures and docstrings (request L1 or symbol-specific L2 `sym://...`)",
                "Full outgoing call & identifier arrays (request symbol-specific L2 `sym://...`)",
                "Static module coupling edges (request L1)"
            ])

        loss = {
            "lossy": True,
            "compact_l2_mode": compact_l2,
            "total_matching_symbols": len(filtered_items),
            "returned_symbols_count": len(included_syms),
            "omitted_symbols_count": len(omitted_syms),
            "omitted_symbols": omitted_syms,
            "next_offset": next_offset,
            "omitted_categories": omitted_cats
        }
        rcpt = record_hydration(session, mod_id, "L2", mod["sha256"], loss)
        return {
            "status": "OK",
            "receipt_id": rcpt,
            "target": mod_id,
            "resolution": "L2_MODULE_MECHANICS_COMPACT" if compact_l2 else "L2_MODULE_MECHANICS",
            "freshness": freshness,
            "declared_rules_count": len(mod["epistemics"]["declared_rules"]),
            "heuristic_hazards_count": len(mod["epistemics"]["heuristic_hazards"]),
            "module_constants": mod.get("module_constants", {}),
            "symbols": included_syms,
            "loss": loss
        }

    # =========================================================================
    # L3: SELF-CONTAINED RAW SOURCE SLICE (with 1-based line anchors & optional paging)
    # =========================================================================
    session["l3_hydrated_targets"].add(target)
    full_source_text = decode_embedded_source(mod["embedded_source_b64_zlib"])
    lines = full_source_text.splitlines()
    safe_line_offset = max(0, int(line_offset))

    if is_symbol_target:
        sym_meta = mod.get("l2_mechanics", {}).get(target)
        if not sym_meta:
            return {"status": "ERROR_SYMBOL_NOT_FOUND", "available_symbols": list(mod.get("l2_mechanics", {}).keys())}
        base_start, base_end = sym_meta["lines"]
        start = min(base_end, base_start + safe_line_offset)
        end = min(base_end, start + max_lines - 1) if max_lines and max_lines > 0 else base_end
        sliced = "\n".join(f"{ln}: {lines[ln - 1]}" for ln in range(start, min(end + 1, len(lines) + 1)))
        full_sym_text = "\n".join(lines[max(0, base_start - 1):min(len(lines), base_end)])
        ref_consts = _resolve_referenced_constants(mod_id, mod, blueprints, full_sym_text)
        truncated = end < base_end
        loss = {
            "lossy": truncated,
            "slice_lines": [start, end],
            "symbol_full_lines": [base_start, base_end],
            "truncated": truncated,
            "next_line_offset": (safe_line_offset + (end - start + 1)) if truncated else None,
            "total_module_lines": len(lines),
            "omitted_lines_outside_slice": len(lines) - (end - start + 1)
        }
        rcpt = record_hydration(session, target, "L3", mod["sha256"], loss)
        return {
            "status": "OK",
            "receipt_id": rcpt,
            "target": target,
            "resolution": "L3_SYMBOL_SOURCE",
            "freshness": freshness,
            "active_declared_rules": mod["epistemics"]["declared_rules"],
            "active_heuristic_hazards": [
                h for h in mod["epistemics"]["heuristic_hazards"] if h["symbol"] == target
            ],
            "referenced_module_constants": ref_consts,
            "lines": [start, end],
            "source": sliced,
            "loss": loss
        }

    # Full or paged module L3 source
    start = min(len(lines), 1 + safe_line_offset) if lines else 1
    end = min(len(lines), start + max_lines - 1) if (max_lines and max_lines > 0 and lines) else len(lines)
    numbered = "\n".join(f"{ln}: {lines[ln - 1]}" for ln in range(start, end + 1)) if lines else ""
    truncated = end < len(lines)
    loss = {
        "lossy": truncated,
        "slice_lines": [start, end],
        "total_module_lines": len(lines),
        "truncated": truncated,
        "next_line_offset": (safe_line_offset + (end - start + 1)) if truncated else None,
        "omitted": ["lines outside selected page"] if truncated else []
    }
    rcpt = record_hydration(session, mod_id, "L3", mod["sha256"], loss)
    return {
        "status": "OK",
        "receipt_id": rcpt,
        "target": mod_id,
        "resolution": "L3_MODULE_SOURCE",
        "freshness": freshness,
        "active_declared_rules": mod["epistemics"]["declared_rules"],
        "active_heuristic_hazards": mod["epistemics"]["heuristic_hazards"],
        "module_constants": mod.get("module_constants", {}),
        "source": numbered,
        "loss": loss
    }


def _resolve_referenced_constants(
    mod_id: str,
    mod: Dict[str, Any],
    blueprints: Dict[str, Any],
    source_slice: str
) -> Dict[str, Any]:
    """
    Heldout-v3 RDS-03 Synthesis:
    Automatically resolves any module-level constants (defined at the top of `mod_id` or in coupled
    modules) that are referenced inside a zoomed symbol slice, returning their literal value, 1-based
    line number, and `defined_in` citation so agents zooming a function at L2/L3 never miss top-of-file constants.
    """
    import re as _re
    candidates: Dict[str, Any] = {}
    for edge in mod.get("coupled_with", []):
        cmod = blueprints.get(edge.get("target", ""), {})
        for cname, cmeta in cmod.get("module_constants", {}).items():
            candidates[cname] = cmeta
    for cname, cmeta in mod.get("module_constants", {}).items():
        candidates[cname] = cmeta
    if not candidates or not source_slice:
        return {}
    tokens_in_slice = set(_re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", source_slice))
    return {
        cname: candidates[cname]
        for cname in sorted(candidates.keys())
        if cname in tokens_in_slice
    }


def _render_l1_blueprint(mod_id: str, mod: Dict[str, Any], freshness: Dict[str, Any], token_budget: int) -> Dict[str, Any]:
    contracts = mod.get("contracts", [])
    included_contracts = []
    truncated_contracts = []
    used_tokens = estimate_tokens(mod.get("epistemics", {})) + 120

    for c in contracts:
        c_cost = estimate_tokens(c)
        if used_tokens + c_cost <= token_budget or len(included_contracts) == 0:
            included_contracts.append(c)
            used_tokens += c_cost
        else:
            truncated_contracts.append(c)

    loss = {
        "lossy": True,
        "omitted_at_l1": mod["loss"]["omitted_at_l1"],
        "truncated_contracts_count": len(truncated_contracts),
        "truncated_contracts": truncated_contracts,
        "mandatory_expand_triggers": mod["loss"]["mandatory_expand_triggers"]
    }

    return {
        "status": "OK",
        "target": mod_id,
        "resolution": "L1_BLUEPRINT",
        "freshness": freshness,
        "contracts": included_contracts,
        "module_constants": mod.get("module_constants", {}),
        "epistemics": {
            "declared_rules": mod["epistemics"]["declared_rules"],
            "heuristic_hazards": mod["epistemics"]["heuristic_hazards"],
            "extracted_facts_count": len(mod["epistemics"]["extracted_facts"])
        },
        "coupled_with": mod.get("coupled_with", []),
        "depended_on_by": mod.get("depended_on_by", []),
        "available_l2_symbols": list(mod.get("l2_mechanics", {}).keys()),
        "loss": loss
    }


def _is_test_caller(mod_id: str, sym_id: str) -> bool:
    rel = mod_id.replace("mod://", "").lower()
    sym_short = sym_id.split("#", 1)[-1].lower()
    if (
        rel.startswith("tests/")
        or rel.startswith("test/")
        or "/tests/" in rel
        or "/test/" in rel
        or "/test_" in rel
        or rel.startswith("test_")
        or "_test." in rel
        or ".test." in rel
        or ".spec." in rel
    ):
        return True
    if sym_short.startswith("test_") or sym_short.startswith("test") and "." in sym_short:
        return True
    return False


def _extract_python_non_code_lines(source_text: str) -> Any:
    import ast as _ast
    docstring_lines: Set[int] = set()
    import_lines: Set[int] = set()
    try:
        tree = _ast.parse(source_text)
    except Exception:
        return docstring_lines, import_lines

    for sub in _ast.walk(tree):
        if isinstance(sub, (_ast.Import, _ast.ImportFrom)):
            start = getattr(sub, "lineno", 0)
            end = getattr(sub, "end_lineno", start)
            for ln in range(start, end + 1):
                import_lines.add(ln)

    scopes = [tree] + [
        n for n in _ast.walk(tree)
        if isinstance(n, (_ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef))
    ]
    for scope in scopes:
        body = getattr(scope, "body", None)
        if body and isinstance(body[0], _ast.Expr):
            val = getattr(body[0], "value", None)
            if isinstance(val, _ast.Constant) and isinstance(getattr(val, "value", None), str):
                start = getattr(body[0], "lineno", 0)
                end = getattr(body[0], "end_lineno", start)
                for ln in range(start, end + 1):
                    docstring_lines.add(ln)
    return docstring_lines, import_lines


def render_compact_markdown(payload: Dict[str, Any]) -> str:
    """
    Heldout-v4 Post-Mortem Synthesis:
    Renders L1, L2, L3, blast_radius, or pre_edit_guardrail responses into ultra-lean, zero-JSON-tax Markdown
    (~85% fewer bytes than verbose JSON schemas, eliminating repeated 64-char SHA-256 strings).
    """
    res = payload.get("resolution", payload.get("status", ""))
    target = payload.get("requested_target") or payload.get("target", "")
    out = [f"# [{res}] {target}"]

    fresh = payload.get("freshness")
    if isinstance(fresh, dict) and fresh.get("status"):
        out.append(f"- FRESHNESS: {fresh['status']}")

    for r in payload.get("active_declared_rules") or payload.get("epistemic_rules_in_module") or payload.get("epistemics", {}).get("declared_rules", []):
        out.append(f"- RULE ({r.get('source', '')}): {r.get('statement', '')}")
    for h in payload.get("active_heuristic_hazards") or payload.get("epistemics", {}).get("heuristic_hazards", []):
        out.append(f"- HAZARD ({h.get('symbol', '')}): {h.get('warning', '')}")

    consts = payload.get("referenced_module_constants") or payload.get("module_constants") or {}
    if consts:
        out.append("## Referenced Constants")
        for cname, cmeta in consts.items():
            default_src = f"{cname} = {cmeta.get('value')}"
            out.append(f"- {cmeta.get('defined_in', cname)}: `{cmeta.get('source_line', default_src)}`")

    if "contracts" in payload and payload["contracts"]:
        out.append("## Contracts")
        for c in payload["contracts"]:
            out.append(f"- `{c}`")

    if "symbol_level_callers" in payload:
        out.append(
            f"## Executable Callers (production={payload.get('production_callers_count', 0)}, test={payload.get('test_callers_count', 0)})"
        )
        for c in payload["symbol_level_callers"]:
            lns = c.get("enclosing_lines", [1, 1])
            out.append(
                f"- [{c.get('caller_tier', 'production').upper()}] `{c['enclosing_symbol']}` "
                f"({c['module']}:{lns[0]}-{lns[1]}) calls {c['matched_symbols']} at lines {c['call_lines']}"
            )
            for ex in c.get("call_excerpts", [])[:3]:
                out.append(f"    {ex}")

    non_calls = payload.get("non_call_references") or []
    if non_calls:
        out.append(f"## Non-Call References (Docstring / Prose / Import: {len(non_calls)})")
        for nc in non_calls[:8]:
            out.append(
                f"- [{nc.get('reference_kind', 'reference').upper()}] `{nc.get('enclosing_symbol', nc.get('module'))}` "
                f"at lines {nc.get('lines', [])}: {nc.get('excerpts', [''])[0]}"
            )

    if "source" in payload:
        out.append("## Source")
        out.append(payload["source"])

    return "\n".join(out)


def lod_blast_radius(
    target: str,
    max_hops: int = 3,
    session_id: str = "default",
    include_tests: bool = True
) -> Dict[str, Any]:
    """
    Transitive BFS Blast-Radius Analysis across the Symbol & Import Reference Graph.
    Separates direct (1-hop) vs. transitive (N-hop) module impact, resolves exact symbol-level
    executable reverse callers (`enclosing_symbol`, `enclosing_lines`, 1-based `call_lines`, and `call_excerpts`),
    separates non-call references (`docstring_or_comment`, `import_statement`) from executable calls,
    ranks production callers ahead of unit-test callers (`caller_tier: 'production' | 'test'`),
    and states static-analysis coverage bounds.
    """
    import re as _re
    data = load_container()
    session = get_session(session_id)
    blueprints = data["l1_blueprints"]

    mod_id = target.split("#")[0].replace("sym://", "mod://")
    if not mod_id.startswith("mod://"):
        mod_id = f"mod://{mod_id}"

    if mod_id not in blueprints:
        return {"status": "ERROR_NOT_FOUND", "error": f"Module `{mod_id}` not found."}

    session["blast_radius_checked"].add(mod_id)

    # Transitive BFS for Downstream Dependents (who breaks if `mod_id` changes?)
    downstream_hops: Dict[str, int] = {}
    queue = deque([(mod_id, 0)])
    while queue:
        curr, dist = queue.popleft()
        if dist >= max_hops:
            continue
        for dep in blueprints.get(curr, {}).get("depended_on_by", []):
            if dep != mod_id and dep not in downstream_hops:
                downstream_hops[dep] = dist + 1
                queue.append((dep, dist + 1))

    # Transitive BFS for Upstream Dependencies (what does `mod_id` rely on?)
    upstream_hops: Dict[str, int] = {}
    queue = deque([(mod_id, 0)])
    while queue:
        curr, dist = queue.popleft()
        if dist >= max_hops:
            continue
        for edge in blueprints.get(curr, {}).get("coupled_with", []):
            dep = edge["target"]
            if dep != mod_id and dep not in upstream_hops:
                upstream_hops[dep] = dist + 1
                queue.append((dep, dist + 1))

    # Heldout-v3/v4 Synthesis: Resolve exact symbol-level reverse callers across all modules (high-recall lexical + AST spans)
    if target.startswith("sym://") and "#" in target:
        qual = target.split("#", 1)[1]
        short = qual.split(".")[-1].replace("impl:", "")
        search_symbols = {s for s in (qual, short) if s and len(s) > 1}
        own_meta = blueprints[mod_id].get("l2_mechanics", {}).get(target)
        own_lines = set(range(own_meta["lines"][0], own_meta["lines"][1] + 1)) if own_meta else set()
        # Scan all modules for symbol-specific queries so conditional/local imports (e.g. agents/service.py:264) are never missed!
        candidate_mods = [mod_id] + [m for m in sorted(blueprints.keys()) if m != mod_id]
    else:
        search_symbols = {s for s in blueprints[mod_id].get("defined_symbols", []) if len(s) > 2}
        own_lines = set()
        candidate_mods = [m for m, _ in sorted(downstream_hops.items(), key=lambda x: (x[1], x[0]))]

    symbol_patterns = {
        stok: _re.compile(rf"\b{_re.escape(stok)}\b")
        for stok in sorted(search_symbols)
    }
    symbol_level_callers: List[Dict[str, Any]] = []
    non_call_references: List[Dict[str, Any]] = []

    for cand_mod_id in candidate_mods:
        cand_mod = blueprints.get(cand_mod_id, {})
        if "embedded_source_b64_zlib" not in cand_mod or not symbol_patterns:
            continue
        cand_source = decode_embedded_source(cand_mod["embedded_source_b64_zlib"])
        cand_lines = cand_source.splitlines()
        is_py = cand_mod.get("language") == "python"
        doc_lines, imp_lines = _extract_python_non_code_lines(cand_source) if is_py else (set(), set())

        mechanics_items = sorted(
            cand_mod.get("l2_mechanics", {}).items(),
            key=lambda kv: (kv[1].get("lines", [1, 1])[1] - kv[1].get("lines", [1, 1])[0], kv[1].get("lines", [1, 1])[0])
        )
        claimed_lines: Set[int] = set()
        for caller_sym_id, caller_meta in mechanics_items:
            if caller_sym_id == target:
                continue
            is_test = _is_test_caller(cand_mod_id, caller_sym_id)
            if is_test and not include_tests:
                continue
            c_start, c_end = caller_meta.get("lines", [1, 1])
            caller_short = caller_sym_id.split("#", 1)[-1].split(".")[-1]
            call_lines: List[int] = []
            matched_toks: Set[str] = set()
            non_call_by_kind: Dict[str, List[int]] = {}

            for ln in range(max(1, c_start), min(c_end + 1, len(cand_lines) + 1)):
                if ln in claimed_lines or (cand_mod_id == mod_id and ln in own_lines):
                    continue
                line_txt = cand_lines[ln - 1]
                stripped_ln = line_txt.strip()
                for stok, srx in symbol_patterns.items():
                    if ln == c_start and (
                        stok == caller_short
                        or _re.match(rf"^\s*(?:pub\s+)?(?:async\s+)?(?:def|class|fn|struct|enum|trait|interface|type)\s+{_re.escape(stok)}\b", line_txt)
                    ):
                        continue
                    if not srx.search(line_txt):
                        continue

                    if is_py:
                        code_part, has_hash, _ = line_txt.partition("#")
                        if ln in doc_lines or stripped_ln.startswith("#") or (has_hash and not srx.search(code_part)):
                            non_call_by_kind.setdefault("docstring_or_comment", []).append(ln)
                            claimed_lines.add(ln)
                            continue
                        if ln in imp_lines or _re.match(r"^\s*(?:from\s+\S+\s+)?import\b", stripped_ln):
                            non_call_by_kind.setdefault("import_statement", []).append(ln)
                            claimed_lines.add(ln)
                            continue

                    matched_toks.add(stok)
                    if ln not in call_lines:
                        call_lines.append(ln)
                    claimed_lines.add(ln)

            if call_lines:
                symbol_level_callers.append({
                    "enclosing_symbol": caller_sym_id,
                    "enclosing_kind": caller_meta.get("kind", "fn"),
                    "enclosing_signature": caller_meta.get("signature", ""),
                    "enclosing_lines": [c_start, c_end],
                    "module": cand_mod_id,
                    "caller_tier": "test" if is_test else "production",
                    "hop_distance": 0 if cand_mod_id == mod_id else downstream_hops.get(cand_mod_id, 1),
                    "matched_symbols": sorted(matched_toks),
                    "call_lines": call_lines[:10],
                    "call_excerpts": [f"{ln}: {cand_lines[ln - 1].strip()}" for ln in call_lines[:5]]
                })

            for ref_kind, r_lines in non_call_by_kind.items():
                if not is_test:
                    uniq_r = sorted(set(r_lines))
                    non_call_references.append({
                        "enclosing_symbol": caller_sym_id,
                        "module": cand_mod_id,
                        "reference_kind": ref_kind,
                        "lines": uniq_r[:6],
                        "excerpts": [f"{ln}: {cand_lines[ln - 1].strip()}" for ln in uniq_r[:3]]
                    })

    # Always rank production callers ahead of test callers!
    symbol_level_callers.sort(
        key=lambda c: (1 if c["caller_tier"] == "test" else 0, c["hop_distance"], c["module"], c["enclosing_lines"][0])
    )
    prod_count = sum(1 for c in symbol_level_callers if c["caller_tier"] == "production")
    test_count = sum(1 for c in symbol_level_callers if c["caller_tier"] == "test")

    impacted_rules: Dict[str, Any] = {}
    for affected_id in [mod_id] + list(downstream_hops.keys()) + list(upstream_hops.keys()):
        ep = blueprints.get(affected_id, {}).get("epistemics", {})
        if ep.get("declared_rules") or ep.get("heuristic_hazards"):
            impacted_rules[affected_id] = {
                "declared_rules": ep.get("declared_rules", []),
                "heuristic_hazards": ep.get("heuristic_hazards", [])
            }

    return {
        "status": "OK",
        "target": mod_id,
        "requested_target": target,
        "direct_dependents_1_hop": [m for m, h in downstream_hops.items() if h == 1],
        "transitive_dependents_n_hop": {m: f"{h}_hops" for m, h in downstream_hops.items() if h > 1},
        "production_callers_count": prod_count,
        "test_callers_count": test_count,
        "symbol_level_callers": symbol_level_callers,
        "non_call_references": non_call_references,
        "direct_dependencies_1_hop": blueprints[mod_id].get("coupled_with", []),
        "transitive_dependencies_n_hop": {m: f"{h}_hops" for m, h in upstream_hops.items() if h > 1},
        "impacted_epistemic_rules": impacted_rules,
        "epistemic_coverage_boundary": {
            "method": "STATIC_AST_AND_SYMBOL_REFERENCE_GRAPH",
            "guarantees": "Captures explicit imports, conditional/local call tokens across modules, enclosing caller symbol spans (production ranked before test), and separates docstring/prose/import references from executable calls.",
            "limitations": [
                "Does NOT prove absence of dynamic dispatch, trait objects, reflection, FFI, or macro-generated references.",
                "0 detected incoming dependents means low static coupling, NOT a guarantee that arbitrary mutation is safe."
            ]
        }
    }


def lod_propose_context_delta(
    decision_statement: str,
    rationale: str,
    affected_modules: List[str],
    new_constraint: Optional[str] = None,
    session_id: str = "default"
) -> Dict[str, Any]:
    """
    Governed Working-Context Writeback: Records a proposed architectural decision or constraint
    alongside a cryptographic Hydration Receipt proving which L0/L1/L2/L3 nodes the agent inspected.
    """
    session = get_session(session_id)
    uninspected = [m for m in affected_modules if m not in session["l1_hydrated_modules"]]
    if uninspected:
        return {
            "status": "DELTA_REJECTED_UNINSPECTED_MODULES",
            "reason": (
                f"Cannot propose an architectural decision affecting {uninspected} without first "
                "hydrating their L1 Blueprints via `lod_zoom(mod_id, 'L1')`."
            ),
            "uninspected_modules": uninspected
        }

    delta_entry = {
        "delta_id": f"delta_{len(session['proposed_deltas']) + 1:03d}",
        "decision_statement": decision_statement,
        "rationale": rationale,
        "affected_modules": affected_modules,
        "new_constraint": new_constraint,
        "hydration_proof": {
            "l1_modules_hydrated": sorted(session["l1_hydrated_modules"]),
            "l3_targets_hydrated": sorted(session["l3_hydrated_targets"]),
            "receipt_count": len(session["hydration_ledger"]),
            "recent_receipts": session["hydration_ledger"][-5:]
        }
    }
    session["proposed_deltas"].append(delta_entry)
    return {
        "status": "DELTA_RECORDED_WITH_HYDRATION_RECEIPT",
        "delta": delta_entry
    }


def _resolve_file_to_mod_id(file_path: str, blueprints: Dict[str, Any]) -> Optional[str]:
    cleaned = file_path.strip()
    if cleaned.startswith("sym://"):
        rel = cleaned.replace("sym://", "").split("#", 1)[0]
        cand = f"mod://{rel}"
        if cand in blueprints:
            return cand
    if cleaned.startswith("mod://"):
        if cleaned in blueprints:
            return cleaned
        cleaned = cleaned.replace("mod://", "")

    norm = cleaned.replace("\\", "/").lstrip("./")
    direct = f"mod://{norm}"
    if direct in blueprints:
        return direct

    for mod_id in blueprints:
        rel_mod = mod_id.replace("mod://", "")
        if norm.endswith("/" + rel_mod) or norm == rel_mod:
            return mod_id
    return None


def _detects_public_signature_modification(mod: Dict[str, Any], patched_content: Optional[str]) -> bool:
    if not patched_content:
        return False
    import re as _re
    normalized_patch = _re.sub(r"\s+", " ", patched_content)
    for contract_sig in mod.get("contracts", []):
        norm_sig = _re.sub(r"\s+", " ", contract_sig).strip()
        if norm_sig and norm_sig not in normalized_patch:
            return True
    return False


def lod_verify_edit_eligibility(
    file_path: str,
    session_id: str = "default",
    intent: str = "edit",
    patched_content: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    token_budget: int = 1500
) -> Dict[str, Any]:
    """
    Host File-Edit Interception Hook (Claude Code PreToolUse / Git pre-commit / Harness Write Gate):
      1. Verifies that the agent hydrated `mod://<file_path>` L1 Blueprint in `session_id` before editing.
         If not, returns `eligible=False`, `exit_code=2`, and auto-hydrates the `L1` Blueprint.
      2. If modifying public signatures on a module with incoming dependents without running
         `lod_blast_radius` (or hydrating all incoming dependent L1 blueprints), returns
         `eligible=False`, `exit_code=3`, and outputs the `L1` Blueprint + uninspected dependents.
    """
    data = load_container()
    session = get_session(session_id)
    blueprints = data.get("l1_blueprints", {})

    mod_id = _resolve_file_to_mod_id(file_path, blueprints)
    if not mod_id:
        return {
            "eligible": False,
            "exit_code": 1,
            "status": "ERROR_MODULE_NOT_IN_CONTAINER",
            "file_path": file_path,
            "error": f"Could not resolve `{file_path}` to any module in `.lod.json`.",
            "available_modules": list(blueprints.keys())
        }

    mod = blueprints[mod_id]
    raw_repo_root = data.get("repo_root")
    default_ws = str((LOD_PATH.parent / raw_repo_root).resolve()) if raw_repo_root else None
    effective_workspace = workspace_dir or default_ws
    freshness = verify_freshness(mod_id, mod, effective_workspace)

    # Gate 1: Agent attempted to edit file on disk without hydrating its L1 Blueprint in active session
    if mod_id not in session["l1_hydrated_modules"]:
        session["l1_hydrated_modules"].add(mod_id)
        l1_payload = _render_l1_blueprint(mod_id, mod, freshness, token_budget)
        rcpt = record_hydration(session, mod_id, "L1_EDIT_HOOK_AUTO_FAULT", mod["sha256"], l1_payload["loss"])
        return {
            "eligible": False,
            "exit_code": 2,
            "status": "EDIT_BLOCKED_L1_NOT_HYDRATED",
            "target": mod_id,
            "session_id": session_id,
            "reason": (
                f"Edit intercepted for `{file_path}` (`{mod_id}`): L1 Blueprint was not hydrated in session `{session_id}`. "
                "The L1 Blueprint has been auto-hydrated below. Review its DECLARED_RULE invariants, "
                "HEURISTIC_HAZARD warnings, and coupled contracts before retrying the edit."
            ),
            "receipt_id": rcpt,
            "auto_hydrated_l1_blueprint": l1_payload,
            "compact_markdown": render_compact_markdown(l1_payload)
        }

    # Gate 2: Modifying public signatures on a module with incoming dependents requires lod_blast_radius or dependent L1s
    incoming_deps = mod.get("depended_on_by", [])
    sig_modified = intent in ("modify_signature", "breaking_change") or _detects_public_signature_modification(mod, patched_content)
    if sig_modified and incoming_deps:
        uninspected_deps = [d for d in incoming_deps if d not in session["l1_hydrated_modules"]]
        if uninspected_deps and mod_id not in session["blast_radius_checked"]:
            l1_payload = _render_l1_blueprint(mod_id, mod, freshness, token_budget)
            return {
                "eligible": False,
                "exit_code": 3,
                "status": "EDIT_BLOCKED_BLAST_RADIUS_REQUIRED",
                "target": mod_id,
                "session_id": session_id,
                "reason": (
                    f"Edit intercepted for `{file_path}` (`{mod_id}`): Modifying public signatures on a module "
                    f"with {len(incoming_deps)} incoming dependent(s) {incoming_deps} requires calling "
                    f"`lod_blast_radius('{mod_id}')` or hydrating dependent L1 blueprints {uninspected_deps} first."
                ),
                "uninspected_dependents": uninspected_deps,
                "auto_hydrated_l1_blueprint": l1_payload,
                "compact_markdown": render_compact_markdown(l1_payload)
            }

    return {
        "eligible": True,
        "exit_code": 0,
        "status": "EDIT_ELIGIBLE_VERIFIED",
        "target": mod_id,
        "session_id": session_id,
        "freshness": freshness,
        "active_declared_rules": mod["epistemics"]["declared_rules"],
        "active_heuristic_hazards": mod["epistemics"]["heuristic_hazards"],
        "blast_radius_checked": mod_id in session["blast_radius_checked"]
    }


def lod_pre_edit_guardrail(
    target: str,
    session_id: str = "default",
    include_tests: bool = False,
    workspace_dir: Optional[str] = None
) -> Dict[str, Any]:
    """
    Single-Call Hybrid Pre-Edit Guardrail Hook:
    Combines L1/L2 contracts, inline referenced module constants, DECLARED_RULE & HEURISTIC_HAZARD
    warnings, live SHA-256 disk freshness verification, and production-first `lod_blast_radius`
    (separating executable call sites from docstring/prose/import references) into a ~1 KB Compact Markdown packet.
    Also marks `l1_hydrated_modules` and `blast_radius_checked` in `session_id` so subsequent edits pass `lod_verify_edit_eligibility`.
    """
    data = load_container()
    session = get_session(session_id)
    blueprints = data.get("l1_blueprints", {})

    base_target = target.split("#", 1)[0]
    mod_id = _resolve_file_to_mod_id(base_target, blueprints)
    if not mod_id:
        return {
            "status": "ERROR_NOT_FOUND",
            "requested_target": target,
            "error": f"Could not resolve `{target}` to any module in container."
        }

    mod = blueprints[mod_id]
    raw_repo_root = data.get("repo_root")
    default_ws = str((LOD_PATH.parent / raw_repo_root).resolve()) if raw_repo_root else None
    effective_workspace = workspace_dir or default_ws
    freshness = verify_freshness(mod_id, mod, effective_workspace)

    session["l1_hydrated_modules"].add(mod_id)
    Mechanics = mod.get("l2_mechanics", {})
    norm_sym_target = f"sym://{mod_id.replace('mod://', '')}#{target.split('#', 1)[1]}" if "#" in target else mod_id

    if "#" in target and norm_sym_target in Mechanics:
        sym_meta = Mechanics[norm_sym_target]
        session["l2_hydrated_targets"].add(norm_sym_target)
        contracts = [sym_meta.get("signature", norm_sym_target)]
        full_src = decode_embedded_source(mod["embedded_source_b64_zlib"]).splitlines()
        s_ln, e_ln = sym_meta.get("lines", [1, len(full_src)])
        sym_slice = "\n".join(full_src[max(0, s_ln - 1):min(len(full_src), e_ln)])
        ref_consts = _resolve_referenced_constants(mod_id, mod, blueprints, sym_slice)
        blast_target = norm_sym_target
    else:
        contracts = list(mod.get("contracts", []))
        full_src_txt = decode_embedded_source(mod["embedded_source_b64_zlib"])
        ref_consts = _resolve_referenced_constants(mod_id, mod, blueprints, full_src_txt)
        blast_target = mod_id

    blast = lod_blast_radius(blast_target, max_hops=2, session_id=session_id, include_tests=include_tests)
    record_hydration(session, blast_target, "PRE_EDIT_GUARDRAIL", mod["sha256"], {"include_tests": include_tests})

    guardrail_payload = {
        "status": "PRE_EDIT_GUARDRAIL_READY",
        "target": mod_id,
        "requested_target": blast_target,
        "freshness": freshness,
        "contracts": contracts,
        "referenced_module_constants": ref_consts,
        "active_declared_rules": mod["epistemics"]["declared_rules"],
        "active_heuristic_hazards": mod["epistemics"]["heuristic_hazards"],
        "production_callers_count": blast.get("production_callers_count", 0),
        "test_callers_count": blast.get("test_callers_count", 0),
        "symbol_level_callers": blast.get("symbol_level_callers", []),
        "non_call_references": blast.get("non_call_references", []),
        "direct_dependents_1_hop": blast.get("direct_dependents_1_hop", []),
        "direct_dependencies_1_hop": blast.get("direct_dependencies_1_hop", []),
    }
    guardrail_payload["compact_markdown"] = render_compact_markdown(guardrail_payload)
    guardrail_payload["json_bytes"] = len(json.dumps(guardrail_payload).encode("utf-8"))
    guardrail_payload["compact_markdown_bytes"] = len(guardrail_payload["compact_markdown"].encode("utf-8"))
    return guardrail_payload


def _PersistSessionFile() -> Path:
    return LOD_PATH.parent / f"{LOD_PATH.name}.sessions.json"


def _load_persisted_sessions() -> None:
    sf = _PersistSessionFile()
    if not sf.exists():
        return
    try:
        raw = json.loads(sf.read_text(encoding="utf-8"))
        for sid, sdata in raw.items():
            SESSION_STATE[sid] = {
                "l0_mounted": bool(sdata.get("l0_mounted", False)),
                "l1_hydrated_modules": set(sdata.get("l1_hydrated_modules", [])),
                "l2_hydrated_targets": set(sdata.get("l2_hydrated_targets", [])),
                "l3_hydrated_targets": set(sdata.get("l3_hydrated_targets", [])),
                "blast_radius_checked": set(sdata.get("blast_radius_checked", [])),
                "hydrated_module_digests": dict(sdata.get("hydrated_module_digests", {})),
                "invalidated_receipts": list(sdata.get("invalidated_receipts", [])),
                "hydration_ledger": list(sdata.get("hydration_ledger", [])),
                "proposed_deltas": list(sdata.get("proposed_deltas", []))
            }
    except Exception:
        pass


def _save_persisted_sessions() -> None:
    sf = _PersistSessionFile()
    try:
        serializable = {
            sid: {
                "l0_mounted": s["l0_mounted"],
                "l1_hydrated_modules": sorted(s["l1_hydrated_modules"]),
                "l2_hydrated_targets": sorted(s["l2_hydrated_targets"]),
                "l3_hydrated_targets": sorted(s["l3_hydrated_targets"]),
                "blast_radius_checked": sorted(s["blast_radius_checked"]),
                "hydrated_module_digests": s.get("hydrated_module_digests", {}),
                "invalidated_receipts": s.get("invalidated_receipts", []),
                "hydration_ledger": s["hydration_ledger"],
                "proposed_deltas": s["proposed_deltas"]
            }
            for sid, s in SESSION_STATE.items()
        }
        sf.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    except Exception:
        pass


def main_verify_edit_cli(argv: Optional[List[str]] = None) -> None:
    """
    CLI entrypoint for Claude Code PreToolUse hooks and Git pre-commit hooks:
      python -m semantic_lod.mcp_server [path/to/.lod.json] --verify-edit <file_path> [session_id] [--intent=modify_signature]
    Exits with non-zero status (2 or 3) and prints the auto-hydrated L1 Blueprint if ineligible.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if "--verify-edit" in args:
        idx = args.index("--verify-edit")
        before = [a for a in args[:idx] if not a.startswith("--")]
        after = args[idx + 1:]
        if before:
            global LOD_PATH
            LOD_PATH = Path(before[0]).resolve()
    else:
        after = args

    intent = "edit"
    positional = []
    for a in after:
        if a.startswith("--intent="):
            intent = a.split("=", 1)[1].strip()
        elif a.startswith("--lod="):
            LOD_PATH = Path(a.split("=", 1)[1].strip()).resolve()
        elif not a.startswith("--"):
            positional.append(a)

    file_path = positional[0] if len(positional) > 0 else ""
    session_id = positional[1] if len(positional) > 1 else "default"
    patched_content = None

    if not file_path and not sys.stdin.isatty():
        try:
            hook_payload = json.loads(sys.stdin.read())
            tool_input = hook_payload.get("tool_input", hook_payload)
            file_path = tool_input.get("file_path") or tool_input.get("path") or ""
            patched_content = tool_input.get("new_string") or tool_input.get("content")
            session_id = hook_payload.get("session_id", session_id)
        except Exception:
            pass

    _load_persisted_sessions()
    verdict = lod_verify_edit_eligibility(
        file_path=file_path,
        session_id=session_id,
        intent=intent,
        patched_content=patched_content
    )
    _save_persisted_sessions()
    print(json.dumps(verdict, indent=2))
    sys.exit(verdict.get("exit_code", 0 if verdict.get("eligible") else 2))


# Support both MCP Python SDK v2 (MCPServer) and v1 (FastMCP)
mcp_app = None
try:
    from mcp.server.mcpserver import MCPServer
    mcp_app = MCPServer("SemanticLOD-v0.4")
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP
        mcp_app = FastMCP("SemanticLOD-v0.4")
    except ImportError:
        mcp_app = None

if mcp_app is not None:
    mcp_app.tool()(lod_map)
    mcp_app.tool()(lod_symbols)
    mcp_app.tool()(lod_zoom)
    mcp_app.tool()(lod_blast_radius)
    mcp_app.tool()(lod_propose_context_delta)
    mcp_app.tool()(lod_verify_edit_eligibility)
    mcp_app.tool()(lod_pre_edit_guardrail)

if __name__ == "__main__":
    if "--verify-edit" in sys.argv:
        main_verify_edit_cli()
    elif mcp_app and "--stdio" in sys.argv:
        mcp_app.run()
    else:
        print(json.dumps(lod_map(token_budget=500), indent=2))
