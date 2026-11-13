"""Experiment 2 — Part B: ObligationLedger lifecycle audit across real multi-commit PRs.

Selects up to 20 recent clean linear non-merge PRs (2 <= k <= 12 commits) that
emit >= 1 production mip-delta finding across 9 OSS clones (round-robin across
repositories, strictly reverse-chronological without cherry-picking).

Compares:
  1. Per-commit incremental checks (c_{i-1} -> c_i)
  2. Stateless base-ref checks at every push (P_0 -> c_i)
  3. Stateful ObligationLedger across c_1 .. c_k -> merge_head (P_0 -> c_k)
  4. Diff-scoped installed mypy errors introduced over P_0 on touched production files

Tracks natural WIP discharge rate, mid-PR re-alert suppression, and anchor
stability vs spurious ID/ordinal/empty-anchor churn. Zero src/mipdelta edits.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments" / "history_replay"))

from mipdelta.cache import FactCache  # noqa: E402
from mipdelta.ledger import ObligationLedger, enclosing_function  # noqa: E402
from mipdelta.model import Finding  # noqa: E402
from replay_history import GitReader, analyze, is_support_path, is_test_path  # noqa: E402

REPOS: tuple[tuple[str, Path], ...] = (
    ("httpx", Path(r"C:\tmp\mipcode-heldout-20261112\repos\httpx")),
    ("marshmallow", Path(r"C:\tmp\mipcode-heldout-20261112\repos\marshmallow")),
    ("mkdocs", Path(r"C:\tmp\mipcode-heldout-20261112\repos\mkdocs")),
    ("rich", Path(r"C:\tmp\mipcode-heldout-20261112\repos\rich")),
    ("pytest", Path(r"C:\tmp\mipcode-heldout2-20261112\repos\pytest")),
    ("requests", Path(r"C:\tmp\mipcode-heldout2-20261112\repos\requests")),
    ("scrapy", Path(r"C:\tmp\mipcode-heldout2-20261112\repos\scrapy")),
    ("urllib3", Path(r"C:\tmp\mipcode-heldout2-20261112\repos\urllib3")),
    ("paramiko", Path(r"C:\tmp\mipcode-heldout2-20261112\repos\paramiko")),
)

MYPY_EXE = Path(r"C:\tmp\exp2-venvs\probe\Scripts\mypy.exe")
SCRATCH_MYPY_ROOT = Path(r"C:\tmp\exp2-partb-mypy")
OUT_DIR = ROOT / "results" / "audit_rt_ledger"


def is_prod_python(path: str) -> bool:
    return path.endswith(".py") and not is_test_path(path) and not is_support_path(path)


def _git_lines(repo: Path, *args: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    return out.splitlines()


def iter_linear_prs(repo: Path, scan_merges: int = 180):
    lines = _git_lines(repo, "log", "--merges", "--first-parent", "-n", str(scan_merges), "--format=%H%x00%P%x00%s")
    for raw in lines:
        parts = raw.split("\x00")
        if len(parts) < 3:
            continue
        merge_sha, parent_str, subject = parts[0], parts[1], parts[2]
        parents = parent_str.split()
        if len(parents) != 2:
            continue
        p1, p2 = parents
        revs = _git_lines(repo, "rev-list", "--reverse", "--parents", f"{p1}..{p2}")
        if not (2 <= len(revs) <= 12):
            continue
        parsed_revs = [rv.split() for rv in revs]
        if any(len(parts) != 2 for parts in parsed_revs):
            continue
        mb = _git_lines(repo, "merge-base", p1, p2)[0].strip()
        if parsed_revs[0][1] != mb:
            continue
        changed = _git_lines(repo, "diff", "--name-only", f"{mb}..{p2}")
        prod_changed = [p for p in changed if is_prod_python(p)]
        if not prod_changed:
            continue
        subj_lines = _git_lines(repo, "log", "--reverse", "--format=%H%x00%s", f"{mb}..{p2}")
        subj_map = dict(line.split("\x00", 1) for line in subj_lines if "\x00" in line)
        commits = [
            {"sha": rv_sha, "parent": rv_parent, "subject": subj_map.get(rv_sha, "")}
            for rv_sha, rv_parent in parsed_revs
        ]
        yield {
            "merge_sha": merge_sha,
            "merge_subject": subject,
            "base": mb,
            "head": p2,
            "k": len(commits),
            "commits": commits,
            "prod_changed_files": sorted(prod_changed),
        }


def _finding_dict(f: Finding, anchor: str, oid: str = "") -> dict[str, Any]:
    return {
        "id": oid,
        "detector": f.detector,
        "symbol": f.symbol,
        "path": f.path,
        "start_line": f.start_line,
        "end_line": f.end_line,
        "anchor": anchor,
        "confidence": f.confidence,
        "resolution_source": f.resolution_source,
        "evidence": f.evidence[0] if f.evidence else "",
    }


def _classify_discharge(
    reader: GitReader,
    cache: FactCache,
    prev_sha: str,
    curr_sha: str,
    item: Any,
) -> str:
    path = item.finding.path
    anchor = item.anchor
    prev_blobs = reader.python_blobs(prev_sha)
    curr_blobs = reader.python_blobs(curr_sha)
    if path not in curr_blobs:
        return "file_removed_or_renamed"
    if prev_blobs.get(path) == curr_blobs.get(path):
        return "target_side_change"
    if not anchor:
        return "file_level_edit"
    prev_text = reader.texts(prev_blobs, [path]).get(path)
    curr_text = reader.texts(curr_blobs, [path]).get(path)
    prev_fn = cache.load(path, prev_text).functions.get(anchor) if prev_text is not None else None
    curr_fn = cache.load(path, curr_text).functions.get(anchor) if curr_text is not None else None
    if curr_fn is None:
        return "anchor_renamed_or_removed"
    if (prev_fn.source_slice if prev_fn else None) != curr_fn.source_slice:
        return "site_function_edit"
    return "same_file_other_edit"


def _source_snippet(reader: GitReader, sha: str, path: str, line: int, window: int = 8) -> str:
    blobs = reader.python_blobs(sha)
    text = reader.texts(blobs, [path]).get(path)
    if text is None:
        return ""
    lines = text.splitlines()
    lo = max(1, line - window)
    hi = min(len(lines), line + window)
    return "\n".join(f"{n:4d}: {lines[n - 1]}" for n in range(lo, hi + 1))


def replay_pr_steps(
    reader: GitReader, cache: FactCache, repo_name: str, pr: dict[str, Any]
) -> dict[str, Any] | None:
    base = pr["base"]
    commits = pr["commits"]
    ledger = ObligationLedger()

    per_commit_rows = []
    cum_steps = []
    any_flagged = False
    any_cross_file = False

    prev_sha = base
    prev_open_by_id: dict[str, Any] = {}
    first_seen_step: dict[str, int] = {}
    discharged_history: list[dict[str, Any]] = []
    opened_history: list[dict[str, Any]] = []
    line_shifts: list[dict[str, Any]] = []
    suspected_churn: list[dict[str, Any]] = []

    for step_idx, cinfo in enumerate(commits, 1):
        csha = cinfo["sha"]
        try:
            inc_cf, inc_rows = analyze(reader, cache, prev_sha, csha, enable=())
            inc_prod = [(f, a) for f, a in inc_rows if is_prod_python(f.path)]

            cum_cf, cum_rows = analyze(reader, cache, base, csha, enable=())
            cum_prod = [(f, a) for f, a in cum_rows if is_prod_python(f.path)]
        except (RecursionError, ValueError, SyntaxError):
            return None

        if inc_cf or cum_cf:
            any_cross_file = True
        if inc_prod or cum_prod:
            any_flagged = True

        anchor_lookup = {
            (f.detector, f.symbol, f.path, f.start_line, f.end_line): a for f, a in cum_prod
        }
        upd = ledger.update(
            [f for f, _ in cum_prod],
            lambda f, am=anchor_lookup: am.get((f.detector, f.symbol, f.path, f.start_line, f.end_line), ""),
        )

        curr_open_by_id = {item.id: item for item in upd.open}
        for item in upd.opened:
            first_seen_step[item.id] = step_idx
            rec = _finding_dict(item.finding, item.anchor, item.id)
            rec["opened_at_step"] = step_idx
            rec["snippet"] = _source_snippet(reader, csha, item.finding.path, item.finding.start_line)
            opened_history.append(rec)

        step_discharged = []
        for item in upd.discharged:
            cause = _classify_discharge(reader, cache, prev_sha, csha, item)
            rec = _finding_dict(item.finding, item.anchor, item.id)
            rec["opened_at_step"] = first_seen_step.get(item.id)
            rec["discharged_at_step"] = step_idx
            rec["discharge_cause"] = cause
            discharged_history.append(rec)
            step_discharged.append(rec)

        for oid, item in curr_open_by_id.items():
            old = prev_open_by_id.get(oid)
            if old is not None and old.finding.start_line != item.finding.start_line:
                line_shifts.append(
                    {
                        "id": oid,
                        "step": step_idx,
                        "path": item.finding.path,
                        "anchor": item.anchor,
                        "old_line": old.finding.start_line,
                        "new_line": item.finding.start_line,
                        "delta": item.finding.start_line - old.finding.start_line,
                    }
                )

        for d_item in upd.discharged:
            for o_item in upd.opened:
                same_det_sym = (d_item.finding.detector, d_item.finding.symbol) == (
                    o_item.finding.detector,
                    o_item.finding.symbol,
                )
                same_det_path = (d_item.finding.detector, d_item.finding.path) == (
                    o_item.finding.detector,
                    o_item.finding.path,
                )
                if same_det_sym or (same_det_path and (d_item.anchor == "" or o_item.anchor == "")):
                    suspected_churn.append(
                        {
                            "step": step_idx,
                            "discharged_id": d_item.id,
                            "discharged_path": d_item.finding.path,
                            "discharged_line": d_item.finding.start_line,
                            "discharged_anchor": d_item.anchor,
                            "opened_id": o_item.id,
                            "opened_path": o_item.finding.path,
                            "opened_line": o_item.finding.start_line,
                            "opened_anchor": o_item.anchor,
                            "detector": d_item.finding.detector,
                            "symbol": d_item.finding.symbol,
                        }
                    )

        per_commit_rows.append(
            {
                "step": step_idx,
                "sha": csha[:10],
                "subject": cinfo["subject"][:100],
                "cross_file": inc_cf,
                "findings_count": len(inc_prod),
                "findings": [_finding_dict(f, a) for f, a in inc_prod],
            }
        )
        cum_steps.append(
            {
                "step": step_idx,
                "sha": csha[:10],
                "cross_file": cum_cf,
                "stateless_base_count": len(cum_prod),
                "ledger_opened_count": len(upd.opened),
                "ledger_discharged_count": len(upd.discharged),
                "ledger_open_count": len(upd.open),
            }
        )

        prev_open_by_id = curr_open_by_id
        prev_sha = csha

    if not any_flagged:
        return None

    final_open = []
    for item in ledger.open():
        rec = _finding_dict(item.finding, item.anchor, item.id)
        rec["opened_at_step"] = first_seen_step.get(item.id)
        rec["snippet"] = _source_snippet(reader, commits[-1]["sha"], item.finding.path, item.finding.start_line)
        final_open.append(rec)

    return {
        "repo": repo_name,
        "merge_sha": pr["merge_sha"][:10],
        "merge_subject": pr["merge_subject"][:120],
        "base": base[:10],
        "base_full": base,
        "head": pr["head"][:10],
        "k": pr["k"],
        "commits": commits,
        "prod_changed_files": pr["prod_changed_files"],
        "any_cross_file": any_cross_file,
        "per_commit_steps": per_commit_rows,
        "cum_steps": cum_steps,
        "opened_events": opened_history,
        "discharged_events": discharged_history,
        "final_open": final_open,
        "line_shifts_survived": line_shifts,
        "suspected_churn": suspected_churn,
    }


def _materialize_tree(repo: Path, commit: str, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    reader = GitReader(repo)
    try:
        blobs = reader.python_blobs(commit)
        texts = reader.texts(blobs)
        for rel, text in texts.items():
            p = dest.joinpath(*rel.split("/"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    finally:
        reader.close()


def _run_mypy_on_targets(tree: Path, targets: list[str]) -> set[tuple[str, str, str]]:
    existing = [t for t in targets if (tree / t).is_file()]
    if not existing:
        return set()
    env = os.environ.copy()
    mypy_paths = [str(tree)]
    if (tree / "src").is_dir():
        mypy_paths.insert(0, str(tree / "src"))
    env["MYPYPATH"] = os.pathsep.join(mypy_paths)
    cmd = [
        str(MYPY_EXE),
        "--ignore-missing-imports",
        "--follow-imports=silent",
        "--no-error-summary",
        "--show-error-codes",
        "--no-incremental",
        "--python-version",
        "3.12",
        *existing,
    ]
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    errors: set[tuple[str, str, str]] = set()
    for line in proc.stdout.splitlines():
        if ": error:" not in line:
            continue
        # path:line: error: msg [code]
        parts = line.split(": error:", 1)
        loc = parts[0]
        msg = parts[1].strip()
        path_part = loc.rsplit(":", 1)[0].replace("\\", "/")
        code = ""
        if msg.endswith("]") and "[" in msg:
            code = msg[msg.rfind("[") + 1 : -1]
        errors.add((path_part, code, msg))
    return errors


def attach_mypy_diff(repo: Path, record: dict[str, Any]) -> None:
    tree = SCRATCH_MYPY_ROOT / record["repo"]
    # Targets = union of changed prod files + files flagged by mipdelta
    flagged_files = {e["path"] for e in record["opened_events"]} | {e["path"] for e in record["final_open"]}
    targets = sorted((set(record["prod_changed_files"]) | flagged_files) & set(
        _git_lines(repo, "ls-tree", "-r", "--name-only", record["commits"][-1]["sha"])
    ))
    base_targets = sorted(set(targets) & set(_git_lines(repo, "ls-tree", "-r", "--name-only", record["base_full"])))

    _materialize_tree(repo, record["base_full"], tree)
    base_errors = _run_mypy_on_targets(tree, base_targets)

    # Also check mypy new errors at each step that opened/discharged/open, plus final step
    interesting_steps = set()
    for row in record["cum_steps"]:
        if row["ledger_opened_count"] or row["ledger_discharged_count"] or row["ledger_open_count"]:
            interesting_steps.add(row["step"])
    interesting_steps.add(record["k"])

    step_mypy: dict[int, list[dict[str, str]]] = {}
    for step_idx in sorted(interesting_steps):
        csha = record["commits"][step_idx - 1]["sha"]
        _materialize_tree(repo, csha, tree)
        curr_errors = _run_mypy_on_targets(tree, targets)
        new_errs = sorted(curr_errors - base_errors)
        step_mypy[step_idx] = [{"path": p, "code": c, "message": m} for p, c, m in new_errs]

    record["mypy_base_error_count"] = len(base_errors)
    record["mypy_new_errors_by_step"] = step_mypy
    record["mypy_new_errors_at_head"] = step_mypy.get(record["k"], [])
    shutil.rmtree(tree, ignore_errors=True)


def main() -> None:
    t0 = time.perf_counter()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SCRATCH_MYPY_ROOT.mkdir(parents=True, exist_ok=True)

    per_repo_pool: dict[str, list[dict[str, Any]]] = {}
    funnel: dict[str, dict[str, int]] = {}

    for repo_name, repo_path in REPOS:
        flagged: list[dict[str, Any]] = []
        scanned = 0
        reader = GitReader(repo_path)
        cache = FactCache()
        last_clear = 0
        try:
            for pr in iter_linear_prs(repo_path, scan_merges=180):
                scanned += 1
                if cache.stats.parses - last_clear > 4000:
                    cache.clear()
                    last_clear = cache.stats.parses
                rec = replay_pr_steps(reader, cache, repo_name, pr)
                if rec is not None:
                    flagged.append(rec)
                    if len(flagged) >= 3:
                        break
        finally:
            reader.close()
        per_repo_pool[repo_name] = flagged
        funnel[repo_name] = {
            "linear_prod_prs_scanned": scanned,
            "flagged_prs_collected": len(flagged),
        }
        print(
            f"[{repo_name}] scanned={scanned} flagged={len(flagged)}",
            flush=True,
        )

    # Interleave round-robin until N = 20 PRs
    selected: list[dict[str, Any]] = []
    idx = 0
    while len(selected) < 20:
        added_round = False
        for repo_name, _ in REPOS:
            pool = per_repo_pool[repo_name]
            if idx < len(pool):
                selected.append(pool[idx])
                added_round = True
                if len(selected) >= 20:
                    break
        if not added_round:
            break
        idx += 1

    repo_map = dict(REPOS)
    for rec in selected:
        attach_mypy_diff(repo_map[rec["repo"]], rec)

    # Aggregate statistics
    total_per_commit = sum(s["findings_count"] for r in selected for s in r["per_commit_steps"])
    total_stateless_base = sum(s["stateless_base_count"] for r in selected for s in r["cum_steps"])
    total_opened = sum(len(r["opened_events"]) for r in selected)
    total_discharged = sum(len(r["discharged_events"]) for r in selected)
    total_open_head = sum(len(r["final_open"]) for r in selected)
    total_line_shifts = sum(len(r["line_shifts_survived"]) for r in selected)
    total_suspected_churn = sum(len(r["suspected_churn"]) for r in selected)

    det_counts = Counter(e["detector"] for r in selected for e in r["opened_events"])
    discharge_causes = Counter(e["discharge_cause"] for r in selected for e in r["discharged_events"])

    discharge_rate_opened = (total_opened - total_open_head) / total_opened if total_opened else 0.0
    discharge_rate_per_commit = (
        max(0, total_per_commit - total_open_head) / total_per_commit if total_per_commit else 0.0
    )

    summary = {
        "prs_selected": len(selected),
        "repos_represented": len({r["repo"] for r in selected}),
        "total_commits_across_prs": sum(r["k"] for r in selected),
        "total_per_commit_findings": total_per_commit,
        "total_stateless_base_alerts": total_stateless_base,
        "total_ledger_opened": total_opened,
        "total_ledger_discharged": total_discharged,
        "total_open_at_merge_head": total_open_head,
        "natural_discharge_rate_vs_opened": round(discharge_rate_opened, 4),
        "natural_discharge_rate_vs_per_commit": round(discharge_rate_per_commit, 4),
        "realert_suppression_ratio": round(1.0 - (total_opened / total_stateless_base), 4) if total_stateless_base else 0.0,
        "line_shifts_survived_count": total_line_shifts,
        "suspected_anchor_churn_count": total_suspected_churn,
        "opened_by_detector": dict(det_counts),
        "discharge_causes": dict(discharge_causes),
        "prs_with_mypy_new_errors_at_head": sum(1 for r in selected if r["mypy_new_errors_at_head"]),
        "wall_seconds": round(time.perf_counter() - t0, 2),
        "funnel": funnel,
    }

    payload = {"summary": summary, "prs": selected}
    (OUT_DIR / "part_b_raw.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("\n=== PART B SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
