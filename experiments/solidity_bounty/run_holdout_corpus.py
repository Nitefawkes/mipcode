"""Run untouched zero-shot Solidity holdout evaluation (v0.6.4 frozen) on 8 external repos.

Clones shallow ``--depth 1`` snapshots of 8 out-of-sample Solidity repositories
that were never part of the 12-target calibration set into ``--work-dir``
(outside this repository), runs ``mipdelta.solidity.audit_solidity_workspace``
unmodified, and writes sanitized summaries to ``results/solidity_bounty/holdout_corpus.json``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mipdelta.cli import _load_solidity_sources  # noqa: E402
from mipdelta.solidity import audit_solidity_workspace  # noqa: E402
from run_contest_corpus import (  # noqa: E402
    _assert_no_local_paths,
    _count_contracts_and_functions,
    _is_prod_sol,
)

ROOT = SCRIPT_DIR.parents[1]
RESULTS_DIR = ROOT / "results" / "solidity_bounty"

HOLDOUT_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    ("v4-core", "https://github.com/Uniswap/v4-core.git", "src", "Uniswap v4 core AMM pools, hooks & libraries"),
    ("permit2", "https://github.com/Uniswap/permit2.git", "src", "Uniswap Permit2 signature-based token approvals"),
    ("morpho-blue", "https://github.com/morpho-org/morpho-blue.git", "src", "Morpho Blue minimal lending primitive & math"),
    ("metamorpho", "https://github.com/morpho-org/metamorpho.git", "src", "MetaMorpho ERC-4626 curated lending vaults"),
    ("openzeppelin-contracts", "https://github.com/OpenZeppelin/openzeppelin-contracts.git", "contracts", "OpenZeppelin Contracts core Solidity standards"),
    ("solmate", "https://github.com/transmissions11/solmate.git", "src", "Solmate gas-optimized core building blocks"),
    ("limit-order-protocol", "https://github.com/1inch/limit-order-protocol.git", "contracts", "1inch Limit Order Protocol v4 settlement"),
    ("sablier-v2-core", "https://github.com/sablier-labs/v2-core.git", "lockup/src", "Sablier Lockup V2 streaming core contracts"),
)


def _git_head_sha(repo_dir: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
    ).strip()


def _ensure_clone(name: str, url: str, work_dir: Path, *, pull: bool = False) -> tuple[Path, str]:
    dest = work_dir / name
    if dest.exists() and (dest / ".git").exists():
        if pull:
            subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"], check=True, capture_output=True)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", url, str(dest)], check=True, capture_output=True)
    return dest, _git_head_sha(dest)


def _eval_holdout(
    name: str, url: str, subdir: str, desc: str, work_dir: Path, *, pull: bool = False
) -> dict[str, Any]:
    repo_dir, head_sha = _ensure_clone(name, url, work_dir, pull=pull)
    scan_root = repo_dir / subdir
    raw = _load_solidity_sources(scan_root)
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
        "url": url,
        "subdir": subdir,
        "description": desc,
        "head_sha": head_sha,
        "head_sha_short": head_sha[:12],
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run untouched v0.6.4 frozen Solidity zero-shot holdout.")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "solidity-holdout-repos",
        help="Directory outside this repo for shallow clones.",
    )
    parser.add_argument("--pull", action="store_true", help="Refresh cached clones to upstream HEAD.")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "holdout_corpus.json")
    args = parser.parse_args(argv)

    rows: list[dict[str, Any]] = []
    for name, url, subdir, desc in HOLDOUT_TARGETS:
        row = _eval_holdout(name, url, subdir, desc, args.work_dir, pull=args.pull)
        rows.append(row)
        print(
            f"[holdout] {name:24s} sha={row['head_sha_short']} files={row['files_scanned']:4d} "
            f"contracts={row['contracts']:4d} funcs={row['functions']:5d} "
            f"wall={row['wall_ms']:7.1f}ms findings={row['findings_count']}",
            flush=True,
        )

    silent = [r for r in rows if r["findings_count"] == 0]
    totals = {
        "frozen_version": "v0.6.4",
        "targets_evaluated": len(rows),
        "targets_silent": len(silent),
        "targets_flagged": len(rows) - len(silent),
        "files_scanned": sum(r["files_scanned"] for r in rows),
        "contracts": sum(r["contracts"] for r in rows),
        "functions": sum(r["functions"] for r in rows),
        "total_wall_ms": round(sum(r["wall_ms"] for r in rows), 3),
        "total_findings": sum(r["findings_count"] for r in rows),
    }
    payload = {"experiment": "solidity_holdout_corpus", "totals": totals, "targets": rows}
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
