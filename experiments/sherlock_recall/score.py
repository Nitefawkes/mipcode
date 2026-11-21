"""Scoring for the Sherlock recall study: coverage, recall, miss buckets, decision rule, agreement.

Pure functions over the JSON the earlier stages wrote. Nothing here calls a model or changes
the sample (spec section 5: no top-up).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import PRICES  # noqa: E402
from match_hits import is_production_sol, norm_path  # noqa: E402

CLASSES = ["in_class", "adjacent", "out_of_class"]
NO_NEW_PARSER = {"sibling_guard_asymmetry", "safety_param_weighting", "existing_detector_gap",
                 "fork_upstream_diff"}
LOW_BOUND, MEANINGFUL_BOUND = 0.15, 0.40
TIE_MARGIN = 3
DEFAULT_DIR = ROOT / "results" / "sherlock_recall"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n if n else None), "ci": [lo, hi]}


def outcome(ci) -> str:
    lo, hi = ci
    low_partial = lo < LOW_BOUND <= hi
    partial_meaningful = lo < MEANINGFUL_BOUND <= hi
    if low_partial and partial_meaningful:
        return "inconclusive:low|meaningful"
    if low_partial:
        return "inconclusive:low|partial"
    if partial_meaningful:
        return "inconclusive:partial|meaningful"
    if lo >= MEANINGFUL_BOUND:
        return "meaningful"
    if lo >= LOW_BOUND:
        return "partial"
    return "low"


# ------------------------------------------------------------------ class coverage

def _final(row: dict) -> dict:
    return row.get("final") or {}


def class_coverage(labels: list[dict]) -> dict:
    def block(rows: list[dict]) -> dict:
        n = len(rows)
        counts = Counter((_final(r).get("class") or "unlabeled") for r in rows)
        return {"n": n, **{c: {"count": counts.get(c, 0), "share": (counts.get(c, 0) / n if n else None)}
                           for c in CLASSES + ["unlabeled"]}}
    return {"overall": block(labels),
            "by_severity": {s: block([r for r in labels if r.get("severity") == s]) for s in ("H", "M")}}


# ------------------------------------------------------------------ recall

def _production_findings(recall_set: dict) -> set[str]:
    """finding_ids with at least one non-external site in a production .sol file."""
    out = set()
    for contest in recall_set.get("contests", []):
        for f in contest.get("findings", []):
            if any(not s.get("external") and is_production_sol(norm_path(s.get("path", "")))
                   for s in f.get("sites", [])):
                out.add(f["finding_id"])
    return out


def _hit_ids(same_defect: list[dict], closeness: dict, disagree: set, strict_only: bool) -> set[str]:
    hits = set()
    for r in same_defect:
        key = (r["finding_id"], r["index"])
        if r.get("verdict") != "same_defect" or key not in closeness or key in disagree:
            continue
        if strict_only and closeness[key] != "strict":
            continue
        hits.add(r["finding_id"])
    return hits


def _disagreements(spot_check) -> set:
    return {(s["finding_id"], s["candidate_index"]) for s in (spot_check or [])
            if s.get("verdict") == "disagree"}


def model_hit_ids(candidates: list[dict], same_defect: list[dict]) -> set[str]:
    """Findings with any same_defect candidate (any closeness); this is the `full` hit set."""
    closeness = {(c["finding_id"], c["index"]): c["closeness"] for c in candidates}
    return _hit_ids(same_defect, closeness, set(), False)


def recall(labels, recall_set, same_defect, spot_check=None, cls="in_class", candidates=None) -> dict:
    """Recall for findings of class `cls`.

    `recall_set` is the parsed recall_set.json (it supplies the production-site denominator).
    `candidates` supplies each candidate's closeness; a same_defect row without a candidate
    row is ignored. The spot-check can only remove hits, never add them.
    """
    prod = _production_findings(recall_set)
    denom = {r["finding_id"] for r in labels if _final(r).get("class") == cls and r["finding_id"] in prod}
    closeness = {(c["finding_id"], c["index"]): c["closeness"] for c in (candidates or [])}
    n = len(denom)
    strict = _hit_ids(same_defect, closeness, set(), True) & denom
    full = _hit_ids(same_defect, closeness, set(), False) & denom
    human = _hit_ids(same_defect, closeness, _disagreements(spot_check), False) & denom
    res = {"cls": cls, "n": n, "strict": _rate(len(strict), n), "full": _rate(len(full), n),
           "full_human": _rate(len(human), n), "hit_ids": sorted(full), "hit_ids_human": sorted(human)}
    res["ci"] = res["full_human"]["ci"] if spot_check is not None else res["full"]["ci"]
    return res


# ------------------------------------------------------------------ miss buckets / decision rule

def miss_buckets(labels: list[dict], hit_ids) -> dict:
    """in_class/adjacent findings with no model hit, bucketed by would_catch_with tag."""
    hit_ids = set(hit_ids)
    tags = defaultdict(lambda: {"H": 0, "M": 0, "solo": 0})
    empty = {"capability_split_true": {"H": 0, "M": 0}, "capability_split_false": {"H": 0, "M": 0}}
    n_misses = 0
    for r in labels:
        fin = _final(r)
        if fin.get("class") not in ("in_class", "adjacent") or r["finding_id"] in hit_ids:
            continue
        n_misses += 1
        sev = r.get("severity")
        wcw = sorted(set(fin.get("would_catch_with") or []))
        if not wcw:
            empty["capability_split_true" if fin.get("capability_split") else "capability_split_false"][sev] += 1
            continue
        for t in wcw:
            tags[t][sev] += 1
            if len(wcw) == 1:
                tags[t]["solo"] += 1
    out_tags = {t: {**v, "total": v["H"] + v["M"]} for t, v in sorted(tags.items())}
    return {"n_misses": n_misses, "tags": out_tags, "empty_tags": empty}


def next_build(buckets: dict) -> dict:
    """Top tag by H+M count excluding none_of_these; a gap under 3 is a tie (prefer no new parser).

    Accepts either miss_buckets() output or a flat {tag: count} mapping.
    """
    src = buckets.get("tags", buckets)
    counts = {}
    for t, v in src.items():
        if t == "none_of_these":
            continue
        counts[t] = v if isinstance(v, int) else v.get("H", 0) + v.get("M", 0)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if not ranked:
        return {"tie": False, "top": None, "count": 0}
    if len(ranked) >= 2 and ranked[0][1] - ranked[1][1] < TIE_MARGIN:
        cands = [ranked[0][0], ranked[1][0]]
        free = [c for c in cands if c in NO_NEW_PARSER]
        needs = [c for c in cands if c not in NO_NEW_PARSER]
        note = ("needs no new parser: " + (", ".join(free) or "neither")
                + "; needs a new parser: " + (", ".join(needs) or "neither"))
        return {"tie": True, "candidates": cands, "preference": "no_new_parser",
                "no_new_parser_candidates": free, "note": note}
    return {"tie": False, "top": ranked[0][0], "count": ranked[0][1]}


# ------------------------------------------------------------------ agreement

def kappa(a: list, b: list) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if pe == 1:
        return 1.0 if po == 1 else 0.0
    return (po - pe) / (1 - pe)


def _jaccard(x: set, y: set) -> float:
    return 1.0 if not x and not y else len(x & y) / len(x | y)


def _reader_data(row: dict, who: str) -> dict | None:
    for c in row.get("calls", []):
        if c.get("reader") == who:
            if c.get("status") == "ok" and isinstance(c.get("data"), dict) and c["data"].get("class") in CLASSES:
                return c["data"]
            return None
    return None


def agreement(labels: list[dict]) -> dict:
    pairs = []
    for r in labels:
        a, b = _reader_data(r, "A"), _reader_data(r, "B")
        if a is not None and b is not None:
            pairs.append((a, b))
    ca, cb = [a["class"] for a, _ in pairs], [b["class"] for _, b in pairs]
    n = len(pairs)
    same = [(a, b) for a, b in pairs if a["class"] == b["class"]]
    pa, pb = [a.get("pattern") for a, _ in same], [b.get("pattern") for _, b in same]
    jac = [_jaccard(set(a.get("would_catch_with") or []), set(b.get("would_catch_with") or []))
           for a, b in pairs]
    return {
        "n_both_ok": n,
        "class": {"n": n, "raw": (sum(x == y for x, y in zip(ca, cb)) / n if n else None),
                  "kappa": kappa(ca, cb)},
        "pattern": {"n": len(same),
                    "raw": (sum(x == y for x, y in zip(pa, pb)) / len(same) if same else None),
                    "kappa": kappa(pa, pb)},
        "would_catch_with": {"n": n, "mean_jaccard": (sum(jac) / n if n else None)},
    }


# ------------------------------------------------------------------ operational counts

def operational(labels: list[dict], same_defect: list[dict], hits: dict) -> dict:
    """Refusal/invalid/fallback counts are keyed by the requested model; tokens and cost by the billed
    model (`served_model` if priced, else requested), exactly as the ledger charges."""
    calls = [c for r in labels for c in r.get("calls", [])]
    calls += [r["call"] for r in same_defect if isinstance(r.get("call"), dict)]
    refusals: dict = defaultdict(Counter)
    fallback: Counter = Counter()
    invalid: Counter = Counter()
    tokens: dict = defaultdict(lambda: {"in_tok": 0, "out_tok": 0})
    unpriced = 0
    for c in calls:
        m = c.get("model", "?")
        if c.get("status") == "refused":
            refusals[m][c.get("refusal_category") or "unknown"] += 1
        if c.get("status") == "invalid":
            invalid[m] += 1
        if c.get("fallback_ran"):
            fallback[m] += 1
        served = c.get("served_model")
        billed = served if served in PRICES else m  # same rule as the ledger (llm.Reader.call)
        tokens[billed]["in_tok"] += c.get("in_tok") or 0
        tokens[billed]["out_tok"] += c.get("out_tok") or 0
        if billed not in PRICES:
            unpriced += 1
    cost = {}
    for m, t in sorted(tokens.items()):
        pi, po = PRICES.get(m, (0.0, 0.0))
        cost[m] = {**t, "est_usd": (t["in_tok"] * pi + t["out_tok"] * po) / 1e6, "priced": m in PRICES}
    finals = [_final(r) for r in labels]
    return {
        "n_calls": len(calls),
        "refusals_by_model": {m: dict(v) for m, v in sorted(refusals.items())},
        "fallback_ran_by_model": dict(sorted(fallback.items())),
        "invalid_by_model": dict(sorted(invalid.items())),
        "invalid_total": sum(invalid.values()),
        "low_confidence": sum(1 for f in finals if f.get("low_confidence")),
        "capability_split": sum(1 for f in finals if f.get("capability_split")),
        "unlabeled": sum(1 for f in finals if f.get("class") is None),
        "tokens_and_cost_by_model": cost,
        "unpriced_calls": unpriced,
        "total_est_usd": sum(v["est_usd"] for v in cost.values()),
        "analyzer_by_contest": [
            {"name": c.get("name"), "n_findings": len(c.get("findings", [])),
             "all_findings_count": c.get("all_findings_count"), "error": c.get("error")}
            for c in (hits or {}).get("contests", [])],
    }


# ------------------------------------------------------------------ main

def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_summary(which, labels_doc, cands_doc, sd_doc, hits, recall_set, spot_check=None) -> dict:
    labels, cands, sd = labels_doc["rows"], cands_doc["rows"], sd_doc["rows"]
    rset = {"contests": [c for c in recall_set["contests"] if c.get("set") == which]}
    rec = recall(labels, rset, sd, spot_check, "in_class", candidates=cands)
    used = "full_human" if spot_check is not None else "full"
    buckets = miss_buckets(labels, model_hit_ids(cands, sd))  # misses use `full`, never the spot-check
    lo, hi = rec[used]["ci"]
    return {
        "set": which,
        "partial": {"labels": labels_doc.get("partial"), "candidates": cands_doc.get("partial"),
                    "same_defect": sd_doc.get("partial")},
        "label_errors": labels_doc.get("errors", []),
        "n_findings": len(labels),
        "class_coverage": class_coverage(labels),
        "recall": rec,
        "recall_adjacent": recall(labels, rset, sd, spot_check, "adjacent", candidates=cands),
        "outcome": {"value": outcome((lo, hi)), "based_on": used, "spot_check_present": spot_check is not None},
        "small_sample_note": (f"n={rec['n']} in_class findings; Wilson 95% CI [{lo:.3f}, {hi:.3f}]. "
                              "Sample not topped up or altered (spec section 5)."),
        "miss_buckets": buckets,
        "next_build": next_build(buckets),
        "agreement": agreement(labels),
        "operational": operational(labels, sd, hits),
        "engine_commit": (hits or {}).get("engine_commit"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="which", required=True, choices=["dev", "measurement"])
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    args = ap.parse_args(argv)
    d = Path(args.dir)
    w = args.which
    spot_path = d / f"spot_check_{w}.json"
    spot = _load(spot_path) if spot_path.exists() else None
    summary = build_summary(w, _load(d / f"labels_{w}.json"), _load(d / f"candidates_{w}.json"),
                            _load(d / f"same_defect_{w}.json"), _load(d / f"hits_{w}.json"),
                            _load(d / "recall_set.json"), spot)
    out = d / f"summary_{w}.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
