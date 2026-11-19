"""Clone audited Sherlock repos at their pinned commits and run the frozen analyzer.

Never executes anything inside a clone except `git` itself.
Candidates are NOT computed here (they need labeled obligation sites; Task 6).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from match_hits import is_production_sol, norm_path  # noqa: E402

DEFAULT_CACHE = ROOT / ".mipdelta" / "sherlock_cache" / "clones"
_LOCAL_PATH_RE = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[/\\]|file://)", re.ASCII)


def _assert_no_local_paths(obj) -> None:
    match = _LOCAL_PATH_RE.search(json.dumps(obj, ensure_ascii=False))
    if match is not None:
        raise ValueError(f"Output contains local path pattern {match.group(0)!r}")


def remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _git(d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True,
                          encoding="utf-8")


def clone_at(repo: str, sha: str, cache: Path) -> Path:
    """Shallow-fetch `sha` of `repo` into cache/<owner>__<name>@<sha>; reuse if present."""
    d = Path(cache) / f"{repo.replace('/', '__')}@{sha}"
    if (d / ".git").exists():
        head = _git(d, "rev-parse", "HEAD")
        if head.returncode == 0 and head.stdout.strip() == sha:
            return d
    d.mkdir(parents=True, exist_ok=True)
    steps = [("init", "-q"),
             ("fetch", "--depth", "1", remote_url(repo), sha),
             ("checkout", "--quiet", "FETCH_HEAD")]
    for step in steps:
        r = _git(d, *step)
        if r.returncode != 0:
            raise RuntimeError(f"git {step[0]} failed for {repo}@{sha}: {r.stderr.strip()[:300]}")
    return d


def run_audit(root: Path, label: str = "") -> dict:
    """Run the analyzer with --root = clone root; return parsed JSON plus wall_ms."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "mipdelta.cli", "--audit-solidity", "--root", str(root), "--json"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    wall_ms = round((time.perf_counter() - t0) * 1000, 1)
    who = label or "analyzer run"
    if proc.returncode != 0:
        raise RuntimeError(f"{who}: analyzer exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"{who}: unparseable analyzer stdout ({exc})") from exc
    if not isinstance(data, dict) or "findings" not in data:
        raise RuntimeError(f"{who}: analyzer JSON has no 'findings'")
    data["wall_ms"] = wall_ms
    return data


def _production_counts(root: Path) -> tuple[int, int]:
    files = lines = 0
    for p in sorted(root.rglob("*.sol")):
        rel = p.relative_to(root).as_posix()
        if rel.split("/")[0] == ".git":
            continue
        if is_production_sol(rel):
            files += 1
            lines += len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    return files, lines


def audit_contest(contest: dict, clone: Path) -> dict:
    out = run_audit(clone, label=contest["name"])
    allf = out["findings"]
    kept = []
    for f in allf:
        path = norm_path(f.get("path", ""))
        if is_production_sol(path):
            kept.append({**f, "path": path})
    pfiles, plines = _production_counts(clone)
    return {
        "name": contest["name"],
        "companion_repo": contest["companion_repo"],
        "resolved_sha": contest["resolved_sha"],
        "files_scanned": out.get("files_scanned", 0),
        "production_files": pfiles,
        "production_lines": plines,
        "wall_ms": out["wall_ms"],
        "all_findings_count": len(allf),
        "findings": kept,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="which", required=True, choices=["dev", "measurement"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine-commit", required=True)
    ap.add_argument("--recall-set", default=None)
    ap.add_argument("--cache", default=None)
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rs_path = Path(args.recall_set) if args.recall_set else out_dir / "recall_set.json"
    cache = Path(args.cache) if args.cache else DEFAULT_CACHE
    contests = [c for c in json.loads(rs_path.read_text(encoding="utf-8"))["contests"]
                if c["set"] == args.which]

    results = []
    for c in contests:
        try:
            clone = clone_at(c["companion_repo"], c["resolved_sha"], cache)
            results.append(audit_contest(c, clone))
        except Exception as exc:  # per-contest isolation: record and continue
            msg = re.sub(r"(?:[A-Za-z]:[/\\][^\s'\"]*|file://\S*)", "<path>", str(exc))
            results.append({"name": c["name"], "companion_repo": c["companion_repo"],
                            "resolved_sha": c["resolved_sha"], "error": f"{type(exc).__name__}: {msg}"})
    payload = {"set": args.which, "engine_commit": args.engine_commit, "contests": results}
    _assert_no_local_paths(payload)
    (out_dir / f"hits_{args.which}.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
