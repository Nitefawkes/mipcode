"""Offline read-only Solidity / Web3 contract-delta evaluation (`mip-delta --audit-solidity`).

Evaluates `mipdelta.solidity` strictly read-only against curated Web3 targets:
1. ERC7786OpenBridge (`try_catch_sink_review` outbox zero-padding defect)
2. Blinded DeFi pairs & benign controls (ERC-4626 vault patch delta, lending borrow cap delta, escrow, staking)
3. Admitted Aqua real campaign production Solidity archives (`1inch/aqua`, `1inch/swap-vm`, `1inch/solidity-utils`) read in-memory from `input.zip`.

Writes sanitized outputs (zero local filesystem paths) to `results/solidity_bounty/`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from mipdelta.solidity import analyze_solidity_delta, audit_solidity_workspace, parse_solidity_file

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "results" / "solidity_bounty"
_LOCAL_PATH_RE = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[/\\]|file://)", re.ASCII)
_NON_PROD_SEGMENTS = frozenset({"test", "tests", "mock", "mocks", "lib", "node_modules", "script", "scripts", "forge-std"})


def _assert_no_local_paths(obj: Any) -> None:
    dumped = json.dumps(obj, ensure_ascii=False)
    match = _LOCAL_PATH_RE.search(dumped)
    if match is not None:
        raise ValueError(f"Sanitized summary contains local path pattern {match.group(0)!r}")


def _finding_dict(f: Any) -> dict[str, Any]:
    return {
        "detector": f.detector,
        "symbol": f.symbol,
        "path": f.path,
        "start_line": f.start_line,
        "end_line": f.end_line,
        "evidence": list(f.evidence),
    }


def _count_contracts_and_functions(files: dict[str, str]) -> tuple[int, int]:
    contracts = 0
    functions = 0
    for path, src in files.items():
        parsed = parse_solidity_file(path, src)
        contracts += len(parsed.contracts)
        functions += sum(len(c.functions) for c in parsed.contracts)
    return contracts, functions


def _is_prod_sol(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    lower = norm.lower()
    if not lower.endswith(".sol") or lower.endswith((".t.sol", ".s.sol")):
        return False
    parts = [p for p in lower.split("/") if p]
    return not any(p in _NON_PROD_SEGMENTS for p in parts[:-1])


def _default_openbridge_path() -> Path:
    env = os.environ.get("MIPDELTA_OPENBRIDGE_SOL")
    if env:
        return Path(env)
    return Path.home() / "Documents" / "Codex" / "ozcc-hunt" / "contracts" / "crosschain" / "ERC7786OpenBridge.sol"


def _default_blinded_dir() -> Path:
    env = os.environ.get("MIPDELTA_BLINDED_DIR")
    if env:
        return Path(env)
    return Path("C:/defi_bounty_investigator/examples/blinded")


def _default_aqua_zip() -> Path:
    env = os.environ.get("MIPDELTA_AQUA_ZIP")
    if env:
        return Path(env)
    return Path("C:/AquaRuntime/admissions/run.aqua-real-296d9c436068/input.zip")


def _eval_openbridge(openbridge_path: Path) -> dict[str, Any]:
    if not openbridge_path.is_file():
        return {"status": "missing", "files": 0, "findings_count": 0, "findings": []}
    rel = "contracts/crosschain/ERC7786OpenBridge.sol"
    files = {rel: openbridge_path.read_text(encoding="utf-8")}
    t0 = time.perf_counter()
    findings = audit_solidity_workspace(files)
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    n_contracts, n_funcs = _count_contracts_and_functions(files)
    return {
        "status": "evaluated",
        "target": "ERC7786OpenBridge",
        "files": len(files),
        "contracts": n_contracts,
        "functions": n_funcs,
        "wall_ms": wall_ms,
        "findings_count": len(findings),
        "findings": [_finding_dict(f) for f in findings],
    }


def _eval_blinded_suite(blinded_dir: Path) -> dict[str, Any]:
    if not blinded_dir.is_dir():
        return {"status": "missing"}
    out: dict[str, Any] = {"status": "evaluated", "pairs": {}, "controls": {}}
    pairs = (
        ("vault_4626", "vault_4626.sol", "vault_4626_patched.sol", "Vault4626.sol"),
        ("synthetic_lending", "synthetic_lending.sol", "synthetic_lending_patched.sol", "SyntheticLendingPool.sol"),
    )
    for label, before_name, after_name, virtual_name in pairs:
        bp = blinded_dir / before_name
        ap = blinded_dir / after_name
        if not (bp.is_file() and ap.is_file()):
            continue
        before_src = bp.read_text(encoding="utf-8")
        after_src = ap.read_text(encoding="utf-8")
        t0 = time.perf_counter()
        delta_findings, delta_meta = analyze_solidity_delta({virtual_name: before_src}, {virtual_name: after_src})
        before_findings = audit_solidity_workspace({before_name: before_src})
        after_findings = audit_solidity_workspace({after_name: after_src})
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        out["pairs"][label] = {
            "wall_ms": wall_ms,
            "before_findings": [_finding_dict(f) for f in before_findings],
            "after_findings": [_finding_dict(f) for f in after_findings],
            "delta_findings": [_finding_dict(f) for f in delta_findings],
            "structural_delta": delta_meta,
        }

    for cname in ("benign_escrow.sol", "benign_staking_rewards.sol"):
        cp = blinded_dir / cname
        if not cp.is_file():
            continue
        files = {cname: cp.read_text(encoding="utf-8")}
        t0 = time.perf_counter()
        findings = audit_solidity_workspace(files)
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        n_contracts, n_funcs = _count_contracts_and_functions(files)
        out["controls"][cname] = {
            "files": 1,
            "contracts": n_contracts,
            "functions": n_funcs,
            "wall_ms": wall_ms,
            "findings_count": len(findings),
            "findings": [_finding_dict(f) for f in findings],
        }
    return out


def _eval_aqua_archive(zip_path: Path) -> dict[str, Any]:
    if not zip_path.is_file():
        return {"status": "missing", "components": []}
    components: dict[str, dict[str, str]] = defaultdict(dict)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in sorted(zf.infolist(), key=lambda x: x.filename):
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if not _is_prod_sol(name):
                continue
            # Group by `evidence/sources/<scope>/<repo>/...` or top-level archive folder
            parts = name.split("/")
            if len(parts) >= 4 and parts[0] == "evidence" and parts[1] == "sources":
                comp_key = f"{parts[2]}/{parts[3]}"
                rel_in_comp = "/".join(parts[4:]) if len(parts) > 4 else parts[-1]
            else:
                comp_key = parts[0] if len(parts) > 1 else "root"
                rel_in_comp = "/".join(parts[1:]) if len(parts) > 1 else name
            components[comp_key][rel_in_comp] = zf.read(info.filename).decode("utf-8", errors="replace")

    rows: list[dict[str, Any]] = []
    for comp_key, files in sorted(components.items()):
        t0 = time.perf_counter()
        findings = audit_solidity_workspace(files)
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        n_contracts, n_funcs = _count_contracts_and_functions(files)
        rows.append(
            {
                "component": comp_key,
                "files_scanned": len(files),
                "contracts": n_contracts,
                "functions": n_funcs,
                "wall_ms": wall_ms,
                "findings_count": len(findings),
                "findings": [_finding_dict(f) for f in findings],
            }
        )
    return {
        "status": "evaluated",
        "components_scanned": len(rows),
        "total_files": sum(r["files_scanned"] for r in rows),
        "total_contracts": sum(r["contracts"] for r in rows),
        "total_functions": sum(r["functions"] for r in rows),
        "total_findings": sum(r["findings_count"] for r in rows),
        "total_wall_ms": round(sum(r["wall_ms"] for r in rows), 3),
        "components": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline read-only Solidity bounty evaluation.")
    parser.add_argument("--openbridge", type=Path, default=_default_openbridge_path())
    parser.add_argument("--blinded-dir", type=Path, default=_default_blinded_dir())
    parser.add_argument("--aqua-zip", type=Path, default=_default_aqua_zip())
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "summary.json")
    args = parser.parse_args(argv)

    summary = {
        "experiment": "solidity_bounty_offline_eval",
        "openbridge": _eval_openbridge(args.openbridge),
        "blinded_defi_suite": _eval_blinded_suite(args.blinded_dir),
        "aqua_campaign_archive": _eval_aqua_archive(args.aqua_zip),
    }
    _assert_no_local_paths(summary)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out.relative_to(ROOT)}")
    print(
        f"  OpenBridge findings: {summary['openbridge'].get('findings_count')} "
        f"(status={summary['openbridge'].get('status')})"
    )
    if "pairs" in summary["blinded_defi_suite"]:
        for k, v in summary["blinded_defi_suite"]["pairs"].items():
            print(
                f"  Blinded pair {k}: delta_findings={len(v['delta_findings'])} "
                f"added_vars={v['structural_delta'].get('added_state_vars')}"
            )
        for k, v in summary["blinded_defi_suite"]["controls"].items():
            print(f"  Blinded control {k}: findings={v['findings_count']}")
    aq = summary["aqua_campaign_archive"]
    print(
        f"  Aqua archive ({aq.get('status')}): components={aq.get('components_scanned')} "
        f"files={aq.get('total_files')} contracts={aq.get('total_contracts')} "
        f"findings={aq.get('total_findings')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
