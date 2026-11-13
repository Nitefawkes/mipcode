"""Run Experiment 1: Arm A/A1 vs. Arm B (Two-Stage Sieve) vs. Arm C (Unguided Diff Control).

Supports --limit N for small smoke runs, caches completed (arm, commit_id) calls in
outputs/predictions.jsonl, and produces scored metrics in results/two_stage_sieve/.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EXP_DIR = Path(__file__).resolve().parent
DATA_DIR = EXP_DIR / "data"
OUT_DIR = EXP_DIR / "outputs"
RESULTS_DIR = ROOT / "results" / "two_stage_sieve"

CLAUDE_EXE = r"C:\Users\17577\.local\bin\claude.exe"

ARM_B_SYSTEM = """You are a strict, concise Stage-2 triage reviewer for `mip-delta`, a Python cross-file contract checker.
Stage 1 flagged candidate cross-file or contract issues (unused newly added parameter, unforwarded optional parameter in a delegating wrapper, subclass/base override mismatch, removed callable still referenced, async/return-arity mismatch at caller, or newly raised exception not caught by caller).
Given the commit subject, changed test filenames, production `.py` diff, Stage-1 advisories, and post-commit code snippets at the flagged sites, classify the commit into ONE verdict:
- `confirm_omission`: concrete evidence in the diff and snippets shows a real bug, missing caller/override update, unforwarded option across sibling wrappers, dead newly-added parameter, or unhandled runtime break.
- `review_worthy`: ambiguous contract seam with real risk that warrants a developer's quick look even if it might turn out intentional.
- `suppress_false_alarm`: Stage-1 alert is clearly benign, intentional, name-shadowed, dynamically handled (`*args`/`**kwargs`/`locals()`), compat-preserved, or otherwise false.

Check carefully for common static-AST false positives before confirming:
- Local variable shadowing or unrelated same-named symbol in a different class/scope.
- Deliberate fixed-policy wrapper where a convenience helper is meant to pin an option rather than forward it.
- Compatible overrides where defaulted optional kwargs do not break callers, or override divergence is deliberate.
- Re-exported/aliased symbols still present under the same name, or parameters kept intentionally for interface compatibility."""

ARM_C_SYSTEM = """You are a strict, concise Python code reviewer inspecting a single commit diff for incomplete cross-file contract changes.
Given only the commit subject, changed test filenames, and the production `.py` diff, decide whether the commit appears to leave an unhandled contract omission:
- `confirm_omission`: concrete code in the diff strongly indicates an unfinished update elsewhere, broken caller/subclass contract, forgotten sibling wrapper propagation, unused newly added parameter, or unhandled new exception.
- `review_worthy`: risky contract change whose cross-file callers/subclasses plausibly need verification.
- `suppress_false_alarm`: commit looks internally complete and safe.

Only output `confirm_omission` or `review_worthy` when concrete code in the diff warrants halting or warning."""

ARM_B_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["confirm_omission", "review_worthy", "suppress_false_alarm"],
            },
            "reason": {"type": "string"},
        },
        "required": ["verdict", "reason"],
        "additionalProperties": False,
    }
)

ARM_C_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["confirm_omission", "review_worthy", "suppress_false_alarm"],
            },
            "suspected_symbol": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "suspected_symbol", "reason"],
        "additionalProperties": False,
    }
)


def format_arm_b_prompt(item: dict[str, Any]) -> str:
    tests_text = ", ".join(item["changed_test_files"]) if item["changed_test_files"] else "(none)"
    parts = [
        f"Repository: {item['repository']}",
        f"Commit: {item['commit']}",
        f"Subject: {item['subject']}",
        f"Changed test/support .py files: {tests_text}",
        "",
        f"=== STAGE-1 FINDINGS ({item['total_sites_raw']} raw sites, showing {len(item['findings'])} grouped) ===",
    ]
    for idx, f in enumerate(item["findings"], 1):
        lines_str = ",".join(str(x) for x in f.get("lines", [f["line"]]))
        parts.append(
            f"{idx}. [{f['detector']}] {f['path']}:{lines_str} "
            f"(anchor={f['anchor'] or '<module>'}, symbol={f['symbol']}, sites={f['site_count_in_group']}) "
            f"-- {f['evidence']}"
        )
    parts.append("")
    parts.append("=== POST-COMMIT SNIPPETS AT FLAGGED SITES ===")
    for snip in item["site_snippets"]:
        parts.append(f"--- {snip['path']} :: {snip['anchor']} (focus line {snip['focus_line']}) ---")
        if snip.get("imports"):
            parts.append("Top imports:\n" + snip["imports"])
        parts.append(f"Lines {snip['span'][0]}-{snip['span'][1]}:\n" + snip["code"])
    parts.append("")
    parts.append("=== PRODUCTION .PY DIFF ===")
    parts.append(item["prod_diff"] or "(empty diff)")
    return "\n".join(parts)


def format_arm_c_prompt(item: dict[str, Any]) -> str:
    tests_text = ", ".join(item["changed_test_files"]) if item["changed_test_files"] else "(none)"
    parts = [
        f"Repository: {item['repository']}",
        f"Commit: {item['commit']}",
        f"Subject: {item['subject']}",
        f"Changed test/support .py files: {tests_text}",
        "",
        "=== PRODUCTION .PY DIFF ===",
        item["prod_diff"] or "(empty diff)",
    ]
    return "\n".join(parts)


def invoke_claude_haiku(
    arm: str, item: dict[str, Any], system_prompt: str, schema: str, prompt_text: str, timeout_s: int = 90
) -> dict[str, Any]:
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [
                CLAUDE_EXE,
                "-p",
                "--model",
                "haiku",
                "--tools",
                "",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--setting-sources",
                "",
                "--system-prompt",
                system_prompt,
                "--output-format",
                "json",
                "--json-schema",
                schema,
            ],
            input=prompt_text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=td,
            timeout=timeout_s,
        )
    wall_s = round(time.perf_counter() - t0, 3)
    if proc.returncode != 0:
        raise RuntimeError(f"claude failed for {arm} {item['id']}: rc={proc.returncode} stderr={proc.stderr[:300]}")
    payload = json.loads(proc.stdout)
    structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        structured = json.loads(payload["result"])
    usage = payload.get("usage") or {}
    return {
        "arm": arm,
        "id": item["id"],
        "repository": item["repository"],
        "commit": item["commit"],
        "stage1_flagged": item["stage1_flagged"],
        "prompt_chars": len(prompt_text),
        "verdict": structured["verdict"],
        "suspected_symbol": structured.get("suspected_symbol", ""),
        "reason": structured.get("reason", ""),
        "duration_ms": payload.get("duration_ms"),
        "wall_s": wall_s,
        "total_cost_usd": float(payload.get("total_cost_usd") or 0.0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "model": next(iter((payload.get("modelUsage") or {}).keys()), "claude-haiku-5-5"),
    }


def load_cached_predictions(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    cached: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.exists():
        return cached
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cached[(row["arm"], row["id"])] = row
    return cached


def compute_metrics(
    items: list[dict[str, Any]],
    truth: dict[str, dict[str, Any]],
    preds: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    flagged_items = [x for x in items if x["stage1_flagged"]]
    silent_items = [x for x in items if not x["stage1_flagged"]]

    def eval_arm(arm_name: str, is_positive_fn, extra_cost_items=None) -> dict[str, Any]:
        kept_real = 0
        kept_revw = 0
        kept_false = 0
        kept_silent = 0
        n_real = sum(1 for x in flagged_items if truth[x["id"]]["commit_label"] == "real_omission")
        n_revw = sum(1 for x in flagged_items if truth[x["id"]]["commit_label"] == "review_worthy")
        n_false = sum(1 for x in flagged_items if truth[x["id"]]["commit_label"] == "false_only")
        n_silent = len(silent_items)
        fn_real: list[dict[str, Any]] = []
        fp_flagged: list[dict[str, Any]] = []
        fp_silent: list[dict[str, Any]] = []

        for x in flagged_items:
            pos = is_positive_fn(x)
            lbl = truth[x["id"]]["commit_label"]
            pred_row = preds.get((arm_name, x["id"]))
            if pos:
                if lbl == "real_omission":
                    kept_real += 1
                elif lbl == "review_worthy":
                    kept_revw += 1
                else:
                    kept_false += 1
                    fp_flagged.append(
                        {
                            "id": x["id"],
                            "detectors": truth[x["id"]]["detectors"],
                            "verdict": pred_row["verdict"] if pred_row else "flagged",
                            "reason": pred_row.get("reason", "") if pred_row else "",
                        }
                    )
            elif lbl == "real_omission":
                fn_real.append(
                    {
                        "id": x["id"],
                        "detectors": truth[x["id"]]["detectors"],
                        "verdict": pred_row["verdict"] if pred_row else "suppressed",
                        "reason": pred_row.get("reason", "") if pred_row else "",
                    }
                )

        for x in silent_items:
            if is_positive_fn(x):
                kept_silent += 1
                pred_row = preds.get((arm_name, x["id"]))
                fp_silent.append(
                    {
                        "id": x["id"],
                        "subject": x["subject"],
                        "verdict": pred_row["verdict"] if pred_row else "flagged",
                        "reason": pred_row.get("reason", "") if pred_row else "",
                    }
                )

        kept_flagged_total = kept_real + kept_revw + kept_false
        kept_useful = kept_real + kept_revw
        kept_all = kept_flagged_total + kept_silent
        useful_prec_on_flagged = round(kept_useful / kept_flagged_total, 4) if kept_flagged_total else 0.0
        useful_prec_overall = round(kept_useful / kept_all, 4) if kept_all else 0.0
        strict_prec_overall = round(kept_real / kept_all, 4) if kept_all else 0.0
        real_recall = round(kept_real / n_real, 4) if n_real else 0.0
        useful_recall = round(kept_useful / (n_real + n_revw), 4) if (n_real + n_revw) else 0.0
        false_suppression = round((n_false - kept_false) / n_false, 4) if n_false else 0.0
        silent_fp_rate = round(kept_silent / n_silent, 4) if n_silent else 0.0

        cost_rows = [preds[(arm_name, x["id"])] for x in (extra_cost_items or []) if (arm_name, x["id"]) in preds]
        return {
            "counts": {
                "flagged_kept": kept_flagged_total,
                "kept_real_omission": kept_real,
                "kept_review_worthy": kept_revw,
                "kept_false_only": kept_false,
                "kept_silent_controls": kept_silent,
                "total_dataset": {"real": n_real, "review_worthy": n_revw, "false_only": n_false, "silent": n_silent},
            },
            "metrics": {
                "useful_precision_flagged_subset": useful_prec_on_flagged,
                "useful_precision_all_67": useful_prec_overall,
                "strict_omission_precision_all_67": strict_prec_overall,
                "real_omission_recall": real_recall,
                "useful_commit_recall": useful_recall,
                "false_alarm_suppression_rate": false_suppression,
                "silent_control_fp_rate": silent_fp_rate,
            },
            "usage": {
                "llm_calls": len(cost_rows),
                "total_cost_usd": round(sum(r["total_cost_usd"] for r in cost_rows), 5),
                "input_tokens": sum(r["input_tokens"] for r in cost_rows),
                "cache_creation_input_tokens": sum(r["cache_creation_input_tokens"] for r in cost_rows),
                "cache_read_input_tokens": sum(r["cache_read_input_tokens"] for r in cost_rows),
                "output_tokens": sum(r["output_tokens"] for r in cost_rows),
                "total_wall_s": round(sum(r["wall_s"] for r in cost_rows), 2),
            },
            "fn_real_omissions": fn_real,
            "fp_flagged_commits": fp_flagged,
            "fp_silent_controls": fp_silent,
        }

    arms = {
        "arm_a_baseline": eval_arm("arm_a", lambda x: x["stage1_flagged"]),
        "arm_a1_drop_state_field": eval_arm(
            "arm_a1",
            lambda x: x["stage1_flagged"] and truth[x["id"]]["after_drop_state_field"] != "suppressed_by_ablation",
        ),
        "arm_b_keep_confirm_or_review": eval_arm(
            "arm_b",
            lambda x: x["stage1_flagged"]
            and preds[("arm_b", x["id"])]["verdict"] in {"confirm_omission", "review_worthy"},
            flagged_items,
        ),
        "arm_b_strict_confirm_only": eval_arm(
            "arm_b",
            lambda x: x["stage1_flagged"] and preds[("arm_b", x["id"])]["verdict"] == "confirm_omission",
            flagged_items,
        ),
        "arm_c_keep_confirm_or_review": eval_arm(
            "arm_c",
            lambda x: preds[("arm_c", x["id"])]["verdict"] in {"confirm_omission", "review_worthy"},
            items,
        ),
        "arm_c_strict_confirm_only": eval_arm(
            "arm_c",
            lambda x: preds[("arm_c", x["id"])]["verdict"] == "confirm_omission",
            items,
        ),
    }

    by_detector: dict[str, Counter[str]] = {}
    for x in flagged_items:
        lbl = truth[x["id"]]["commit_label"]
        v = preds[("arm_b", x["id"])]["verdict"]
        for det in truth[x["id"]]["detectors"]:
            by_detector.setdefault(det, Counter())[f"{lbl}|{v}"] += 1

    return {
        "arms": arms,
        "arm_b_by_detector": {det: dict(sorted(c.items())) for det, c in sorted(by_detector.items())},
    }


def render_report(summary: dict[str, Any]) -> str:
    arms = summary["arms"]

    def pct(v: float) -> str:
        return f"{v * 100:.1f}%"

    lines = [
        "# Experiment 1 Results: Two-Stage Precision Sieve",
        "",
        "## Headline Comparison (`37` Flagged + `30` Silent Contract-Changing Controls = `67` Commits)",
        "",
        "| Arm | Flagged Kept (`R+RW+F+S`) | Useful Prec (`(R+RW)/All`) | Strict Prec (`R/All`) | Real Recall (`/13`) | Useful Recall (`/18`) | False Suppr (`/19`) | Silent FP (`/30`) | LLM Calls | Nominal Cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows = [
        ("Arm A: `mip-delta` baseline (`f4361fd`)", arms["arm_a_baseline"]),
        ("Arm A1: `mip-delta` minus `state_field_review`", arms["arm_a1_drop_state_field"]),
        ("Arm B: Two-Stage (`confirm` + `review_worthy`)", arms["arm_b_keep_confirm_or_review"]),
        ("Arm B (strict): Two-Stage (`confirm_omission` only)", arms["arm_b_strict_confirm_only"]),
        ("Arm C: Unguided Haiku Diff (`confirm` + `review_worthy`)", arms["arm_c_keep_confirm_or_review"]),
        ("Arm C (strict): Unguided Haiku Diff (`confirm` only)", arms["arm_c_strict_confirm_only"]),
    ]
    for label, row in rows:
        c = row["counts"]
        m = row["metrics"]
        u = row["usage"]
        kept_str = f"{c['kept_real_omission']}+{c['kept_review_worthy']}+{c['kept_false_only']}+{c['kept_silent_controls']}={c['kept_real_omission']+c['kept_review_worthy']+c['kept_false_only']+c['kept_silent_controls']}"
        lines.append(
            f"| {label} | {kept_str} | {pct(m['useful_precision_all_67'])} | {pct(m['strict_omission_precision_all_67'])} | "
            f"{c['kept_real_omission']}/13 ({pct(m['real_omission_recall'])}) | {c['kept_real_omission']+c['kept_review_worthy']}/18 ({pct(m['useful_commit_recall'])}) | "
            f"{19 - c['kept_false_only']}/19 ({pct(m['false_alarm_suppression_rate'])}) | {c['kept_silent_controls']}/30 ({pct(m['silent_control_fp_rate'])}) | "
            f"{u['llm_calls']} | ${u['total_cost_usd']:.4f} |"
        )

    lines.extend(["", "## Arm B Breakdown by Stage-1 Detector (`commit_label | haiku_verdict`)", ""])
    for det, counts in summary["arm_b_by_detector"].items():
        summary_str = ", ".join(f"`{k}`: {v}" for k, v in counts.items())
        lines.append(f"- **`{det}`**: {summary_str}")

    for key, title in [
        ("arm_b_keep_confirm_or_review", "Arm B (`confirm` + `review_worthy`)"),
        ("arm_c_keep_confirm_or_review", "Arm C (`confirm` + `review_worthy`)"),
    ]:
        row = arms[key]
        lines.extend(["", f"## Error Analysis: {title}", ""])
        lines.append(f"- **Missed real omissions ({len(row['fn_real_omissions'])}):**")
        for fn in row["fn_real_omissions"]:
            lines.append(f"  - `{fn['id']}` ({', '.join(fn['detectors'])}) -> `{fn['verdict']}`: {fn['reason']}")
        lines.append(f"- **Surviving false alarms on F ({len(row['fp_flagged_commits'])}):**")
        for fp in row["fp_flagged_commits"]:
            lines.append(f"  - `{fp['id']}` ({', '.join(fp['detectors'])}) -> `{fp['verdict']}`: {fp['reason']}")
        if row["fp_silent_controls"]:
            lines.append(f"- **False positives on silent negative controls S ({len(row['fp_silent_controls'])}):**")
            for fps in row["fp_silent_controls"]:
                lines.append(f"  - `{fps['id']}` (`{fps['subject']}`) -> `{fps['verdict']}`: {fps['reason']}")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Smoke-test only first N commits per subset (sets --skip-score)")
    parser.add_argument("--workers", type=int, default=3, help="Concurrent Claude CLI subprocesses")
    parser.add_argument("--skip-score", action="store_true", help="Do not write full results summary (for partial smoke runs)")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = OUT_DIR / "predictions.jsonl"

    dataset = json.loads((DATA_DIR / "dataset.json").read_text(encoding="utf-8"))["items"]
    if args.limit is not None:
        flagged_sub = [x for x in dataset if x["stage1_flagged"]][: args.limit]
        silent_sub = [x for x in dataset if not x["stage1_flagged"]][: args.limit]
        run_items = flagged_sub + silent_sub
    else:
        run_items = dataset

    preds = load_cached_predictions(cache_path)
    jobs: list[tuple[str, dict[str, Any], str, str, str]] = []
    for item in run_items:
        if item["stage1_flagged"] and ("arm_b", item["id"]) not in preds:
            jobs.append(("arm_b", item, ARM_B_SYSTEM, ARM_B_SCHEMA, format_arm_b_prompt(item)))
        if ("arm_c", item["id"]) not in preds:
            jobs.append(("arm_c", item, ARM_C_SYSTEM, ARM_C_SCHEMA, format_arm_c_prompt(item)))

    print(f"Dataset items selected: {len(run_items)}; cached hits={len(preds)}; remaining LLM calls={len(jobs)}")
    write_lock = threading.Lock()
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {
                pool.submit(invoke_claude_haiku, arm, item, sys_p, schema, prompt): (arm, item["id"])
                for arm, item, sys_p, schema, prompt in jobs
            }
            done_n = 0
            for fut in concurrent.futures.as_completed(futs):
                row = fut.result()
                done_n += 1
                with write_lock:
                    preds[(row["arm"], row["id"])] = row
                    with cache_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(row) + "\n")
                print(
                    f"[{done_n}/{len(jobs)}] {row['arm']} {row['id']} -> {row['verdict']} "
                    f"(${row['total_cost_usd']:.4f}, {row['wall_s']}s)",
                    flush=True,
                )

    if args.limit is not None or args.skip_score:
        print("Smoke/partial run finished without full scoring.")
        return 0

    truth = json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    summary = compute_metrics(dataset, truth, preds)
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (RESULTS_DIR / "README.md").write_text(render_report(summary), encoding="utf-8")
    print("Wrote", RESULTS_DIR / "summary.json")
    print("Wrote", RESULTS_DIR / "README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
