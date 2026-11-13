"""Build the blinded dataset and separate ground-truth file for Experiment 1.

Reads the 10 held-out regression JSON reports, adjudications, and local OSS
repository clones (via Git plumbing only) to produce:
- experiments/two_stage_sieve/data/dataset.json (blinded inputs; NO labels)
- experiments/two_stage_sieve/data/ground_truth.json (commit-level labels)
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments" / "history_replay"))

from mipdelta.cache import FactCache  # noqa: E402
from mipdelta.ledger import enclosing_function  # noqa: E402
from replay_history import GitReader, analyze, is_support_path, is_test_path  # noqa: E402

MAX_DIFF_CHARS = 45_000
MAX_UNIQUE_SITES = 12
MAX_SNIPPETS = 8
MAX_SNIPPET_LINES = 100
MAX_SNIPPET_CHARS = 3_200


def is_prod_python(path: str) -> bool:
    return path.endswith(".py") and not is_test_path(path) and not is_support_path(path)


def git_changed_files(repo: Path, commit: str) -> tuple[list[str], list[str]]:
    raw = subprocess.run(
        ["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = [line.strip() for line in raw.splitlines() if line.strip()]
    prod = sorted(f for f in files if is_prod_python(f))
    tests_or_support = sorted(f for f in files if f.endswith(".py") and not is_prod_python(f))
    return prod, tests_or_support


def git_prod_diff(repo: Path, commit: str, prod_files: list[str]) -> tuple[str, bool]:
    if not prod_files:
        return "", False
    raw = subprocess.run(
        ["git", "-C", str(repo), "show", "--no-ext-diff", "--format=", "-U3", commit, "--", *prod_files],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    if len(raw) <= MAX_DIFF_CHARS:
        return raw, False
    marker = f"\n... [truncated after {MAX_DIFF_CHARS} of {len(raw)} chars] ...\n"
    return raw[:MAX_DIFF_CHARS] + marker, True


def numbered_slice(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    start = max(1, start_line)
    end = min(len(lines), end_line)
    return "\n".join(f"{idx:4d}: {lines[idx - 1]}" for idx in range(start, end + 1))


def extract_import_lines(text: str, limit: int = 25) -> str:
    picked = []
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if idx <= 120 and (stripped.startswith("import ") or stripped.startswith("from ")):
            picked.append(f"{idx:4d}: {line}")
            if len(picked) >= limit:
                break
    return "\n".join(picked)


def extract_snippet(
    reader: GitReader, cache: FactCache, commit_blobs: dict[str, str], path: str, line: int, anchor: str
) -> dict[str, Any] | None:
    if path not in commit_blobs:
        return None
    text = reader.texts(commit_blobs, [path]).get(path)
    if text is None:
        return None
    facts = cache.load(path, text)
    fn_key = anchor or enclosing_function(facts, line)
    fn = facts.functions.get(fn_key) if fn_key else None
    if fn is not None:
        span_start, span_end = fn.line, fn.end_line
        if span_end - span_start + 1 > MAX_SNIPPET_LINES:
            half = MAX_SNIPPET_LINES // 2
            span_start = max(fn.line, line - half)
            span_end = min(fn.end_line, span_start + MAX_SNIPPET_LINES - 1)
    else:
        span_start, span_end = max(1, line - 20), line + 20
    code = numbered_slice(text, span_start, span_end)
    if len(code) > MAX_SNIPPET_CHARS:
        code = code[:MAX_SNIPPET_CHARS] + "\n... [snippet truncated]"
    return {
        "path": path,
        "anchor": fn_key or f"<module:{line}>",
        "focus_line": line,
        "span": [span_start, span_end],
        "imports": extract_import_lines(text),
        "code": code,
    }


def load_adjudication_labels(held_out_dir: Path) -> tuple[dict[tuple[Any, ...], str], dict[tuple[Any, ...], str]]:
    exact: dict[tuple[Any, ...], str] = {}
    loose: dict[tuple[Any, ...], str] = {}
    for adj_path in [held_out_dir / "adjudication.json", held_out_dir / "round2" / "adjudication.json"]:
        rows = json.loads(adj_path.read_text(encoding="utf-8"))
        rows = rows if isinstance(rows, list) else rows["sites"]
        for row in rows:
            ke = (
                row["repository"],
                row["commit"],
                row["detector"],
                row["site_path"],
                row["site_line"],
                row["changed_symbol"],
                row["tool_message"],
            )
            kl = (
                row["repository"],
                row["commit"],
                row["detector"],
                row["site_path"],
                row["site_line"],
                row["changed_symbol"],
            )
            exact[ke] = row["final_label"]
            loose[kl] = row["final_label"]
    return exact, loose


def classify_commit_truth(
    repo_name: str,
    commit_entry: dict[str, Any],
    labels_exact: dict[tuple[Any, ...], str],
    labels_loose: dict[tuple[Any, ...], str],
) -> dict[str, Any]:
    site_labels = []
    non_state_site_labels = []
    detectors = sorted({s["detector"] for s in commit_entry["sites"]})
    for s in commit_entry["sites"]:
        ke = (repo_name, commit_entry["commit"], s["detector"], s["path"], s["line"], s["symbol"], s.get("evidence", ""))
        kl = (repo_name, commit_entry["commit"], s["detector"], s["path"], s["line"], s["symbol"])
        lbl = labels_exact.get(ke) or labels_loose.get(kl) or "unlabeled"
        site_labels.append(lbl)
        if s["detector"] != "state_field_review":
            non_state_site_labels.append(lbl)

    def summarize_lbls(lbls: list[str]) -> str:
        if not lbls:
            return "suppressed_by_ablation"
        if "real_omission" in lbls:
            return "real_omission"
        if "review_worthy" in lbls:
            return "review_worthy"
        if set(lbls) == {"false_report"}:
            return "false_only"
        return "other"

    commit_label = summarize_lbls(site_labels)
    assert commit_label in {"real_omission", "review_worthy", "false_only"}, (
        f"Unexpected label {commit_label} for {repo_name}:{commit_entry['commit']}: {site_labels}"
    )
    return {
        "commit_label": commit_label,
        "after_drop_state_field": summarize_lbls(non_state_site_labels),
        "detectors": detectors,
        "total_sites": len(commit_entry["sites"]),
    }


def summarize_findings_and_snippets(
    reader: GitReader, cache: FactCache, full_sha: str, sites: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blobs = reader.python_blobs(full_sha)
    grouped: list[dict[str, Any]] = []
    seen_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for s in sites:
        key = (s["detector"], s["symbol"], s["path"], s.get("anchor", ""))
        if key not in seen_groups:
            item = {
                "detector": s["detector"],
                "symbol": s["symbol"],
                "path": s["path"],
                "line": s["line"],
                "anchor": s.get("anchor", ""),
                "provenance": s.get("provenance", "static_ast"),
                "evidence": s.get("evidence", ""),
                "site_count_in_group": 1,
                "lines": [s["line"]],
            }
            seen_groups[key] = item
            grouped.append(item)
        else:
            seen_groups[key]["site_count_in_group"] += 1
            if len(seen_groups[key]["lines"]) < 6:
                seen_groups[key]["lines"].append(s["line"])

    capped_findings = grouped[:MAX_UNIQUE_SITES]
    snippets: list[dict[str, Any]] = []
    seen_targets: set[tuple[str, str]] = set()
    for s in capped_findings:
        target = (s["path"], s["anchor"])
        if target in seen_targets:
            continue
        seen_targets.add(target)
        snip = extract_snippet(reader, cache, blobs, s["path"], s["line"], s["anchor"])
        if snip is not None:
            snippets.append(snip)
        if len(snippets) >= MAX_SNIPPETS:
            break
    return capped_findings, snippets


def resolve_full_sha(repo: Path, short_or_full: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", short_or_full],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def recent_commits(repo: Path, head_full: str, count: int) -> list[tuple[str, str, str]]:
    raw = subprocess.run(
        ["git", "-C", str(repo), "log", "--no-merges", f"--max-count={count}", "--format=%H%x00%P%x00%s", head_full],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")
    rows = []
    for line in reversed(raw.splitlines()):
        sha, parents, subject = (line.split("\x00") + ["", ""])[:3]
        parent = parents.split(" ")[0] if parents else ""
        if parent:
            rows.append((sha, parent, subject))
    return rows


def sample_silent_controls(
    repo: Path,
    report: dict[str, Any],
    flagged_short_shas: set[str],
    cache: FactCache,
    rng: random.Random,
    n_samples: int,
) -> list[dict[str, Any]]:
    reader = GitReader(repo)
    try:
        head_full = resolve_full_sha(repo, report["head"])
        window = [
            c
            for c in recent_commits(repo, head_full, report["commits_replayed"])
            if c[0][:12] not in flagged_short_shas
        ]
        indices = list(range(len(window)))
        rng.shuffle(indices)
        picked: list[dict[str, Any]] = []
        for idx in indices:
            sha, parent, subject = window[idx]
            try:
                cross_file, rows = analyze(reader, cache, parent, sha, enable=())
            except (RecursionError, ValueError):
                continue
            if not cross_file:
                continue
            production = [(f, a) for f, a in rows if not is_test_path(f.path) and not is_support_path(f.path)]
            if production:
                continue
            prod_files, test_files = git_changed_files(repo, sha)
            if not prod_files:
                continue
            diff_text, diff_truncated = git_prod_diff(repo, sha, prod_files)
            picked.append(
                {
                    "id": f"{report['repository']}:{sha[:12]}",
                    "round": report["_round"],
                    "repository": report["repository"],
                    "commit": sha[:12],
                    "subject": subject,
                    "stage1_flagged": False,
                    "changed_prod_files": prod_files,
                    "changed_test_files": test_files,
                    "prod_diff": diff_text,
                    "prod_diff_truncated": diff_truncated,
                    "total_sites_raw": 0,
                    "findings": [],
                    "site_snippets": [],
                }
            )
            if len(picked) >= n_samples:
                break
        assert len(picked) == n_samples, f"Only found {len(picked)} silent controls for {report['repository']}"
        return picked
    finally:
        reader.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r1-repos", type=Path, default=Path(r"C:\tmp\mipcode-heldout-20261112\repos"))
    parser.add_argument("--r2-repos", type=Path, default=Path(r"C:\tmp\mipcode-heldout2-20261112\repos"))
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--silent-per-repo", type=int, default=3)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "experiments" / "two_stage_sieve" / "data",
    )
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    held_out_dir = ROOT / "results" / "held_out"
    labels_exact, labels_loose = load_adjudication_labels(held_out_dir)
    rng = random.Random(args.seed)
    cache = FactCache()

    dataset_items: list[dict[str, Any]] = []
    truth_map: dict[str, dict[str, Any]] = {}

    repo_dirs = {"round1": args.r1_repos, "round2": args.r2_repos}
    for rnd in ["round1", "round2"]:
        reg_dir = held_out_dir / "regression" / rnd
        for path in sorted(reg_dir.glob("*.json")):
            if path.name == "summary.json":
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            report["_round"] = rnd
            repo_name = report["repository"]
            repo_path = repo_dirs[rnd] / repo_name
            assert repo_path.exists(), f"Missing clone: {repo_path}"
            cache = FactCache()
            print(f"Processing {rnd}/{repo_name}...", flush=True)

            reader = GitReader(repo_path)
            flagged_shas: set[str] = set()
            try:
                for entry in report["flagged_commits"]:
                    short_sha = entry["commit"]
                    flagged_shas.add(short_sha)
                    full_sha = resolve_full_sha(repo_path, short_sha)
                    prod_files, test_files = git_changed_files(repo_path, full_sha)
                    diff_text, diff_truncated = git_prod_diff(repo_path, full_sha, prod_files)
                    grouped_findings, snippets = summarize_findings_and_snippets(
                        reader, cache, full_sha, entry["sites"]
                    )
                    cid = f"{repo_name}:{short_sha}"
                    dataset_items.append(
                        {
                            "id": cid,
                            "round": rnd,
                            "repository": repo_name,
                            "commit": short_sha,
                            "subject": entry["subject"],
                            "stage1_flagged": True,
                            "changed_prod_files": prod_files,
                            "changed_test_files": test_files,
                            "prod_diff": diff_text,
                            "prod_diff_truncated": diff_truncated,
                            "total_sites_raw": len(entry["sites"]),
                            "findings": grouped_findings,
                            "site_snippets": snippets,
                        }
                    )
                    truth_map[cid] = classify_commit_truth(repo_name, entry, labels_exact, labels_loose)
            finally:
                reader.close()

            silent_items = sample_silent_controls(
                repo_path, report, flagged_shas, cache, rng, args.silent_per_repo
            )
            for item in silent_items:
                dataset_items.append(item)
                truth_map[item["id"]] = {
                    "commit_label": "silent_control",
                    "after_drop_state_field": "silent_control",
                    "detectors": [],
                    "total_sites": 0,
                }

    dataset_path = args.out_dir / "dataset.json"
    truth_path = args.out_dir / "ground_truth.json"
    dataset_path.write_text(json.dumps({"seed": args.seed, "items": dataset_items}, indent=2) + "\n", encoding="utf-8")
    truth_path.write_text(json.dumps(truth_map, indent=2) + "\n", encoding="utf-8")

    flagged_n = sum(1 for x in dataset_items if x["stage1_flagged"])
    silent_n = sum(1 for x in dataset_items if not x["stage1_flagged"])
    print(f"Wrote {len(dataset_items)} commits (flagged={flagged_n}, silent={silent_n}) -> {dataset_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
