"""Widen Experiment 2 Part B reverse-chronologically past 180 merges to reach N=15..20 flagged PRs.

Zero cherry-picking:
1. Preserves the initial 180-merge scan in results/audit_rt_ledger/part_b_raw_180.json.
2. Expands in uniform merge-depth tiers across all 9 held-out repos:
   180 -> 400 -> 700 -> 1100 first-parent merges (max 3 flagged PRs per repo, stop at N=20).
3. Skips merges already checked in earlier tiers and reuses the exact step replay +
   ObligationLedger + diff-scoped mypy instrumentation from run_part_b_ledger.py.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\Projects\mipcode")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments" / "history_replay"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mipdelta.cache import FactCache
from replay_history import GitReader
from run_part_b_ledger import (
    OUT_DIR,
    REPOS,
    _git_lines,
    attach_mypy_diff,
    iter_linear_prs,
    replay_pr_steps,
)

TIERS = [(180, 400), (400, 700), (700, 1100)]
MAX_PER_REPO = 3
TARGET_TOTAL_PRS = 20


def _merge_set(repo: Path, n: int) -> set[str]:
    return set(_git_lines(repo, "rev-list", "--first-parent", "--merges", "-n", str(n), "HEAD"))


def summarize_prs(
    prs: list[dict[str, Any]],
    funnel: dict[str, dict[str, Any]],
    wall_seconds: float,
) -> dict[str, Any]:
    total_commits = sum(int(p["k"]) for p in prs)
    total_per_commit = sum(sum(int(s["findings_count"]) for s in p["per_commit_steps"]) for p in prs)
    total_stateless = sum(sum(int(cs["stateless_base_count"]) for cs in p["cum_steps"]) for p in prs)
    total_opened = sum(len(p["opened_events"]) for p in prs)
    total_discharged = sum(len(p["discharged_events"]) for p in prs)
    total_open_head = sum(len(p["final_open"]) for p in prs)

    discharge_rate_opened = (total_discharged / total_opened) if total_opened else 0.0
    discharge_rate_per_commit = (
        max(0, total_per_commit - total_open_head) / total_per_commit
        if total_per_commit
        else 0.0
    )
    realert_suppression = (
        (total_stateless - total_opened) / total_stateless if total_stateless else 0.0
    )

    line_shifts = sum(len(p["line_shifts_survived"]) for p in prs)
    churn_count = sum(len(p["suspected_churn"]) for p in prs)

    by_det: Counter[str] = Counter()
    discharge_causes: Counter[str] = Counter()
    for p in prs:
        for ev in p["opened_events"]:
            by_det[ev["detector"]] += 1
        for ev in p["discharged_events"]:
            discharge_causes[ev.get("discharge_cause", "unknown")] += 1

    mypy_head_prs = sum(1 for p in prs if p.get("mypy_new_errors_at_head"))

    return {
        "prs_selected": len(prs),
        "repos_represented": len({p["repo"] for p in prs}),
        "total_commits_across_prs": total_commits,
        "total_per_commit_findings": total_per_commit,
        "total_stateless_base_alerts": total_stateless,
        "total_ledger_opened": total_opened,
        "total_ledger_discharged": total_discharged,
        "total_open_at_merge_head": total_open_head,
        "natural_discharge_rate_vs_opened": round(discharge_rate_opened, 4),
        "natural_discharge_rate_vs_per_commit": round(discharge_rate_per_commit, 4),
        "realert_suppression_ratio": round(realert_suppression, 4),
        "line_shifts_survived_count": line_shifts,
        "suspected_anchor_churn_count": churn_count,
        "opened_by_detector": dict(by_det),
        "discharge_causes": dict(discharge_causes),
        "prs_with_mypy_new_errors_at_head": mypy_head_prs,
        "wall_seconds": round(wall_seconds, 2),
        "funnel": funnel,
    }


def main() -> None:
    t0 = time.perf_counter()
    raw_path = OUT_DIR / "part_b_raw.json"
    raw_180_path = OUT_DIR / "part_b_raw_180.json"
    if not raw_180_path.exists():
        shutil.copy2(raw_path, raw_180_path)
        print(f"Backed up initial 180-merge baseline to {raw_180_path}", flush=True)

    base_data = json.loads(raw_180_path.read_text(encoding="utf-8"))
    prs: list[dict[str, Any]] = list(base_data["prs"])
    base_wall = float(base_data["summary"].get("wall_seconds", 0.0))

    funnel: dict[str, dict[str, Any]] = {}
    repo_counts: Counter[str] = Counter(p["repo"] for p in prs)
    seen_short: set[tuple[str, str]] = {(p["repo"], p["merge_sha"]) for p in prs}

    for name, repo in REPOS:
        prev_f = dict(base_data["summary"]["funnel"].get(name, {}))
        total_merges = len(_git_lines(repo, "rev-list", "--first-parent", "--merges", "HEAD"))
        funnel[name] = {
            "linear_prod_prs_scanned": int(prev_f.get("linear_prod_prs_scanned", 0)),
            "flagged_prs_collected": int(prev_f.get("flagged_prs_collected", 0)),
            "scan_merges_frontier": min(180, total_merges),
            "total_first_parent_merges": total_merges,
        }

    print(
        f"Starting Part B widening from N={len(prs)} PRs (max {MAX_PER_REPO}/repo, target {TARGET_TOTAL_PRS})...",
        flush=True,
    )

    readers = {name: GitReader(repo) for name, repo in REPOS}
    caches = {name: FactCache() for name, _repo in REPOS}

    for prev_d, curr_d in TIERS:
        if len(prs) >= TARGET_TOTAL_PRS:
            break
        print(f"\n=== Tier {prev_d} -> {curr_d} first-parent merges ===", flush=True)
        for name, repo in REPOS:
            if len(prs) >= TARGET_TOTAL_PRS:
                break
            if repo_counts[name] >= MAX_PER_REPO:
                continue
            total_m = funnel[name]["total_first_parent_merges"]
            if prev_d >= total_m:
                print(f"  [{name}] exhausted ({total_m} total merges)", flush=True)
                continue

            prev_merges = _merge_set(repo, prev_d)
            eff_curr = min(curr_d, total_m)
            scanned_here = 0
            found_here = 0

            for pr in iter_linear_prs(repo, scan_merges=curr_d):
                msha = pr["merge_sha"]
                if msha in prev_merges or (name, msha[:10]) in seen_short:
                    continue
                scanned_here += 1
                funnel[name]["linear_prod_prs_scanned"] += 1
                rec = replay_pr_steps(readers[name], caches[name], name, pr)
                if rec is None:
                    continue
                attach_mypy_diff(repo, rec)
                prs.append(rec)
                seen_short.add((name, msha[:10]))
                repo_counts[name] += 1
                found_here += 1
                funnel[name]["flagged_prs_collected"] += 1
                print(
                    f"  [{name}] #{len(prs)}: {rec['merge_sha']} ({rec['k']} commits) "
                    f"opened={len(rec['opened_events'])} discharged={len(rec['discharged_events'])} "
                    f"head_open={len(rec['final_open'])} mypy_head={len(rec.get('mypy_new_errors_at_head', []))}",
                    flush=True,
                )
                if repo_counts[name] >= MAX_PER_REPO or len(prs) >= TARGET_TOTAL_PRS:
                    break

            funnel[name]["scan_merges_frontier"] = eff_curr
            print(
                f"  [{name}] tier({prev_d}->{eff_curr}): newly_scanned={scanned_here}, "
                f"newly_flagged={found_here}, repo_total={repo_counts[name]}",
                flush=True,
            )

    widen_wall = time.perf_counter() - t0
    summary = summarize_prs(prs, funnel, base_wall + widen_wall)
    summary["widen_wall_seconds"] = round(widen_wall, 2)
    payload = {"summary": summary, "prs": prs}
    raw_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("\n=== Widened Part B Summary ===", flush=True)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
