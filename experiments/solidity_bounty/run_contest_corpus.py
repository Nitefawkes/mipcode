"""Evaluate `mip-delta --audit-solidity` across 12 live contest & bug-bounty Solidity targets.

Scans strictly read-only across:
- 8 local contest / bounty repositories (`oz-community-contracts`, `dhedge-v2`,
  `twyne-v1`, `veda-boring-vault`, `lombard-evm`, `strata-contracts`,
  `origin-dollar`, `termmax-v2`)
- 4 admitted Aqua campaign archive components (`supporting/aqua`, `target/aqua`,
  `target/solidity-utils`, `target/swap-vm`) read in memory from `input.zip`.

Writes sanitized results (zero local filesystem paths, full `url`/`head_sha`/`subdir`
for deep-linking) to `results/solidity_bounty/contest_corpus.json`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _ContestTargetSpec:
    name: str
    repo_root: Path
    subdir: str
    url: str
    fallback_sha: str
    description: str


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


def _git_head_and_url(repo_dir: Path, fallback_url: str, fallback_sha: str) -> tuple[str, str]:
    sha = fallback_sha
    url = fallback_url
    proc_sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc_sha.returncode == 0 and proc_sha.stdout.strip():
        sha = proc_sha.stdout.strip()
    proc_url = subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc_url.returncode == 0 and proc_url.stdout.strip().startswith("https://"):
        url = proc_url.stdout.strip()
    return url, sha


def _contest_targets(codex_root: Path) -> tuple[_ContestTargetSpec, ...]:
    oz_worktree = codex_root / "ozcc-hunt.worktrees" / "axelar-identity-20260913"
    oz_root = oz_worktree if (oz_worktree / "contracts").is_dir() else (codex_root / "ozcc-hunt")
    return (
        _ContestTargetSpec(
            name="oz-community-contracts",
            repo_root=oz_root,
            subdir="contracts",
            url="https://github.com/OpenZeppelin/openzeppelin-community-contracts.git",
            fallback_sha="0361935dc5edd233adbb9fbae5871ccbb8c739d8",
            description="openzeppelin-community-contracts (crosschain / governance / access)",
        ),
        _ContestTargetSpec(
            name="dhedge-v2",
            repo_root=codex_root / "dhedge-v2-hunt",
            subdir="contracts",
            url="https://github.com/dhedge/V2-Public.git",
            fallback_sha="f09febf528895fdd5b69f68b0dd3a9bb7d9ecc0c",
            description="dHEDGE V2 asset/contract guards & routers",
        ),
        _ContestTargetSpec(
            name="twyne-v1",
            repo_root=codex_root / "twyne-contracts-v1",
            subdir="src",
            url="https://github.com/0xTwyne/twyne-contracts-v1.git",
            fallback_sha="beb4bf15a70b1a8abcf7e17e8a08f556ee5f84ab",
            description="Twyne V1 collateral vaults & operators",
        ),
        _ContestTargetSpec(
            name="veda-boring-vault",
            repo_root=codex_root / "veda-boring-vault",
            subdir="src",
            url="https://github.com/Veda-Labs/boring-vault.git",
            fallback_sha="a3fc204083358eef2fc5c27e801fe45652f89de3",
            description="Veda BoringVault roles, tellers & decoders",
        ),
        _ContestTargetSpec(
            name="lombard-evm",
            repo_root=codex_root / "lombard-evm-smart-contracts.hunts" / "bridgev2-message-identity-20260914",
            subdir="contracts",
            url="https://github.com/lombard-finance/evm-smart-contracts.git",
            fallback_sha="7fe83e56f2265c0c281747c8dd3bb141d3022c46",
            description="Lombard LBTC & BridgeV2",
        ),
        _ContestTargetSpec(
            name="strata-contracts",
            repo_root=codex_root / "strata-contracts.hunts" / "loss-redemption-20260914",
            subdir="contracts",
            url="https://github.com/Strata-Markets/contracts.git",
            fallback_sha="2be97f9b22c34e536475e6ea28370073a97b94c0",
            description="Strata CDO tranches & strategies",
        ),
        _ContestTargetSpec(
            name="origin-dollar",
            repo_root=codex_root / "origin-dollar.hunts" / "ousd-curve-amo-swap-sandwich-20260914",
            subdir="contracts/contracts",
            url="https://github.com/OriginProtocol/origin-dollar.git",
            fallback_sha="8b0cf08ab90dca90aef9fc1d302ac1c11496890d",
            description="Origin Dollar OUSD/OETH vault & strategies",
        ),
        _ContestTargetSpec(
            name="termmax-v2",
            repo_root=codex_root / "termmax-contract-v2.hunts" / "source-lock-20260913",
            subdir="contracts",
            url="https://github.com/term-structure/termmax-contract-v2.git",
            fallback_sha="e314f3f849577dfecd4614f148c4df81fdf8c72d",
            description="TermMax V2 fixed-rate markets & routers",
        ),
    )


def _eval_dir_target(spec: _ContestTargetSpec) -> dict[str, Any]:
    scan_root = spec.repo_root / spec.subdir
    url, head_sha = _git_head_and_url(spec.repo_root, spec.url, spec.fallback_sha)
    if not scan_root.is_dir():
        return {
            "target": spec.name,
            "kind": "contest_repo",
            "url": url,
            "subdir": spec.subdir,
            "head_sha": head_sha,
            "head_sha_short": head_sha[:12],
            "description": spec.description,
            "status": "missing",
        }
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
        "target": spec.name,
        "kind": "contest_repo",
        "url": url,
        "subdir": spec.subdir,
        "head_sha": head_sha,
        "head_sha_short": head_sha[:12],
        "description": spec.description,
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


def _read_aqua_release_meta(zf: zipfile.ZipFile) -> dict[str, tuple[str, str]]:
    meta: dict[str, tuple[str, str]] = {}
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("evidence/releases/") and name.endswith(".json"):
            data = json.loads(zf.read(info.filename).decode("utf-8", errors="replace"))
            repo_name = Path(name).stem
            repo_url = str(data.get("repository_url") or "")
            if repo_url and not repo_url.endswith(".git"):
                repo_url += ".git"
            sha = str(data.get("commit_sha") or "")
            if repo_url and sha:
                meta[repo_name] = (repo_url, sha)
    return meta


def _eval_aqua_targets(zip_path: Path) -> list[dict[str, Any]]:
    if not zip_path.is_file():
        return []
    components: dict[str, dict[str, str]] = defaultdict(dict)
    with zipfile.ZipFile(zip_path, "r") as zf:
        release_meta = _read_aqua_release_meta(zf)
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
        repo_short = comp_key.split("/")[-1]
        url, head_sha = release_meta.get(repo_short, ("", ""))
        t0 = time.perf_counter()
        findings = audit_solidity_workspace(files)
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        contracts, functions = _count_contracts_and_functions(files)
        rows.append(
            {
                "target": f"aqua:{comp_key}",
                "kind": "aqua_archive",
                "url": url,
                "subdir": "",
                "head_sha": head_sha,
                "head_sha_short": head_sha[:12],
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
    for spec in _contest_targets(args.codex_root):
        row = _eval_dir_target(spec)
        rows.append(row)
        print(
            f"[solidity] {row['target']:24s} sha={row.get('head_sha_short', '')} "
            f"files={row.get('files_scanned', 0):4d} contracts={row.get('contracts', 0):4d} "
            f"funcs={row.get('functions', 0):5d} wall={row.get('wall_ms', 0.0):7.1f}ms "
            f"findings={row.get('findings_count', 0)}"
        )
    for row in _eval_aqua_targets(args.aqua_zip):
        rows.append(row)
        print(
            f"[solidity] {row['target']:24s} sha={row.get('head_sha_short', '')} "
            f"files={row.get('files_scanned', 0):4d} contracts={row.get('contracts', 0):4d} "
            f"funcs={row.get('functions', 0):5d} wall={row.get('wall_ms', 0.0):7.1f}ms "
            f"findings={row.get('findings_count', 0)}"
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
