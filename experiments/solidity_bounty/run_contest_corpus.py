"""Evaluate `mip-delta --audit-solidity` across 12 live contest & bug-bounty Solidity targets.

Scans strictly read-only across:
- 8 local contest / bounty repositories (`oz-community-contracts`, `dhedge-v2`,
  `twyne-v1`, `veda-boring-vault`, `lombard-evm`, `strata-contracts`,
  `origin-dollar`, `termmax-v2`)
- 4 admitted Aqua campaign archive components (`supporting/aqua`, `target/aqua`,
  `target/solidity-utils`, `target/swap-vm`) read in memory from `input.zip`.

Writes sanitized results (zero local filesystem paths) to `results/solidity_bounty/contest_corpus.json`.
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

from mipdelta.cli import _load_solidity_sources
from mipdelta.solidity import audit_solidity_workspace, parse_solidity_file

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "results" / "solidity_bounty"
_LOCAL_PATH_RE = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[/\\]|file://)", re.ASCII)
_EXCLUDE_SEGMENTS = frozenset(
    {"mock", "mocks", "test", "tests", "archive", "vendor", "dependencies", "script", "scripts", "forge-std", "lib", "node_modules"}
)


def _assert_no_local_paths(obj: Any) -> None:
    dumped = json.dumps(obj, ensure_ascii=False)
    match = _LOCAL_PATH_RE.search(dumped)
    if match is not None:
        raise ValueError(f"Sanitized summary contains local path pattern {match.group(0)!r}")


def _is_prod_sol(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    lower = norm.lower()
    if not lower.endswith(".sol") or lower.endswith((".t.sol", ".s.sol")):
        return False
    parts = [p for p in lower.split("/") if p]
    return not any(p in _EXCLUDE_SEGMENTS for p in parts[:-1])


def _count_contracts_and_functions(files: dict[str, str]) -> tuple[int, int]:
    contracts = 0
    functions = 0
    for path, src in files.items():
        parsed = parse_solidity_file(path, src)
        contracts += len(parsed.contracts)
        functions += sum(len(c.functions) for c in parsed.contracts)
    return contracts, functions


def _default_codex_dir() -> Path:
    env = os.environ.get("MIPDELTA_CONTEST_ROOT")
    if env:
        return Path(env)
    return Path.home() / "Documents" / "Codex"


def _default_aqua_zip() -> Path:
    env = os.environ.get("MIPDELTA_AQUA_ZIP")
    if env:
        return Path(env)
    return Path("C:/AquaRuntime/admissions/run.aqua-real-296d9c436068/input.zip")


def _contest_targets(codex_root: Path) -> tuple[tuple[str, Path, str], ...]:
    return (
        ("oz-community-contracts", codex_root / "ozcc-hunt" / "contracts", "openzeppelin-community-contracts (crosschain / governance / access)"),
        ("dhedge-v2", codex_root / "dhedge-v2-hunt" / "contracts", "dHEDGE V2 asset/contract guards & routers"),
        ("twyne-v1", codex_root / "twyne-contracts-v1" / "src", "Twyne V1 collateral vaults & operators"),
        ("veda-boring-vault", codex_root / "veda-boring-vault" / "src", "Veda BoringVault roles, tellers & decoders"),
        ("lombard-evm", codex_root / "lombard-evm-smart-contracts.hunts" / "bridgev2-message-identity-20260914" / "contracts", "Lombard LBTC & BridgeV2"),
        ("strata-contracts", codex_root / "strata-contracts.hunts" / "loss-redemption-20260914" / "contracts", "Strata CDO tranches & strategies"),
        ("origin-dollar", codex_root / "origin-dollar.hunts" / "ousd-curve-amo-swap-sandwich-20260914" / "contracts" / "contracts", "Origin Dollar OUSD/OETH vault & strategies"),
        ("termmax-v2", codex_root / "termmax-contract-v2.hunts" / "source-lock-20260913" / "contracts", "TermMax V2 fixed-rate markets & routers"),
    )


def _eval_dir_target(name: str, root: Path, description: str) -> dict[str, Any]:
    if not root.is_dir():
        return {"target": name, "kind": "contest_repo", "description": description, "status": "missing"}
    raw = _load_solidity_sources(root)
    files = {k: v for k, v in raw.items() if _is_prod_sol(k)}
    t0 = time.perf_counter()
    findings = audit_solidity_workspace(files)
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    contracts, functions = _count_contracts_and_functions(files)
    by_detector: dict[str, int] = {}
    for f in findings:
        by_detector[f.detector] = by_detector.get(f.detector, 0) + 1
    return {
        "target": name,
        "kind": "contest_repo",
        "description": description,
        "status": "evaluated",
        "files_scanned": len(files),
        "contracts": contracts,
        "functions": functions,
        "wall_ms": wall_ms,
        "findings_count": len(findings),
        "by_detector": by_detector,
        "findings": [
            {
                "detector": f.detector,
                "symbol": f.symbol,
                "path": f.path,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "evidence": list(f.evidence),
            }
            for f in findings
        ],
    }


def _eval_aqua_targets(zip_path: Path) -> list[dict[str, Any]]:
    if not zip_path.is_file():
        return []
    components: dict[str, dict[str, str]] = defaultdict(dict)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in sorted(zf.infolist(), key=lambda x: x.filename):
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if not _is_prod_sol(name):
                continue
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
        contracts, functions = _count_contracts_and_functions(files)
        rows.append(
            {
                "target": f"aqua:{comp_key}",
                "kind": "aqua_archive",
                "description": f"Pinned Aqua admission component ({comp_key})",
                "status": "evaluated",
                "files_scanned": len(files),
                "contracts": contracts,
                "functions": functions,
                "wall_ms": wall_ms,
                "findings_count": len(findings),
                "by_detector": {},
                "findings": [
                    {
                        "detector": f.detector,
                        "symbol": f.symbol,
                        "path": f.path,
                        "start_line": f.start_line,
                        "end_line": f.end_line,
                        "evidence": list(f.evidence),
                    }
                    for f in findings
                ],
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark --audit-solidity on 12 contest/bounty Solidity targets.")
    parser.add_argument("--codex-root", type=Path, default=_default_codex_dir())
    parser.add_argument("--aqua-zip", type=Path, default=_default_aqua_zip())
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "contest_corpus.json")
    args = parser.parse_args(argv)

    rows: list[dict[str, Any]] = []
    for name, path, desc in _contest_targets(args.codex_root):
        row = _eval_dir_target(name, path, desc)
        rows.append(row)
        print(
            f"[solidity] {row['target']:24s} files={row.get('files_scanned', 0):4d} "
            f"contracts={row.get('contracts', 0):4d} funcs={row.get('functions', 0):5d} "
            f"wall={row.get('wall_ms', 0.0):7.1f}ms findings={row.get('findings_count', 0)}"
        )
    for row in _eval_aqua_targets(args.aqua_zip):
        rows.append(row)
        print(
            f"[solidity] {row['target']:24s} files={row.get('files_scanned', 0):4d} "
            f"contracts={row.get('contracts', 0):4d} funcs={row.get('functions', 0):5d} "
            f"wall={row.get('wall_ms', 0.0):7.1f}ms findings={row.get('findings_count', 0)}"
        )

    evaluated = [r for r in rows if r.get("status") == "evaluated"]
    silent = [r for r in evaluated if r.get("findings_count", 0) == 0]
    totals = {
        "targets_evaluated": len(evaluated),
        "targets_silent": len(silent),
        "targets_flagged": len(evaluated) - len(silent),
        "files_scanned": sum(r["files_scanned"] for r in evaluated),
        "contracts": sum(r["contracts"] for r in evaluated),
        "functions": sum(r["functions"] for r in evaluated),
        "total_wall_ms": round(sum(r["wall_ms"] for r in evaluated), 3),
        "total_findings": sum(r["findings_count"] for r in evaluated),
    }
    payload = {"experiment": "solidity_contest_corpus", "totals": totals, "targets": rows}
    _assert_no_local_paths(payload)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.out.relative_to(ROOT)} ({totals['targets_evaluated']} targets, "
        f"{totals['files_scanned']} files, {totals['total_findings']} findings, {totals['total_wall_ms']} ms)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
