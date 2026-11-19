"""Scoring: Wilson CI, outcome bands, recall, miss buckets, decision rule, agreement (in-memory fixtures)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "sherlock_recall"))

score = importlib.import_module("score")
llm = importlib.import_module("llm")
wilson, outcome, recall = score.wilson, score.outcome, score.recall
next_build, miss_buckets, agreement = score.next_build, score.miss_buckets, score.agreement


def site(path="src/A.sol", external=False):
    return {"path": path, "external": external}


def lrow(fid, cls, sev="H", wcw=(), split=False, calls=()):
    return {"finding_id": fid, "severity": sev, "calls": list(calls),
            "final": {"class": cls, "pattern": "other", "would_catch_with": list(wcw),
                      "capability_split": split, "low_confidence": False}}


def call(who, data, model="claude-sonnet-5-5", status="ok", **kw):
    base = {"reader": who, "model": model, "status": status, "data": data, "fallback_ran": False,
            "refusal_category": None, "in_tok": 1000, "out_tok": 100, "served_model": model}
    return {**base, **kw}


def fixture():
    recall_set = {"contests": [{"name": "c", "set": "dev", "findings": [
        {"finding_id": "c:H-1", "sites": [site()]},
        {"finding_id": "c:H-2", "sites": [site("src/B.sol")]},
        {"finding_id": "c:H-3", "sites": [site("test/T.sol")]},  # test path only: not in denominator
        {"finding_id": "c:H-4", "sites": [site("src/C.sol", external=True)]},  # external only
        {"finding_id": "c:M-1", "sites": [site("src/D.sol")]},
    ]}]}
    labels = [lrow("c:H-1", "in_class"), lrow("c:H-2", "in_class"), lrow("c:H-3", "in_class"),
              lrow("c:H-4", "in_class"), lrow("c:M-1", "in_class", "M")]
    cands = [{"finding_id": "c:H-1", "index": 0, "closeness": "strict"},
             {"finding_id": "c:H-2", "index": 0, "closeness": "file"},
             {"finding_id": "c:M-1", "index": 0, "closeness": "function"},
             {"finding_id": "c:H-3", "index": 0, "closeness": "strict"}]
    sd = [{"finding_id": "c:H-1", "index": 0, "verdict": "same_defect"},
          {"finding_id": "c:H-2", "index": 0, "verdict": "same_defect"},
          {"finding_id": "c:M-1", "index": 0, "verdict": "related"},
          {"finding_id": "c:H-3", "index": 0, "verdict": "same_defect"}]
    return labels, recall_set, cands, sd


def test_wilson_known_value():
    assert wilson(5, 20) == pytest.approx((0.1119, 0.4687), abs=1e-3)
    assert wilson(0, 0) == (0.0, 1.0)


def test_outcome_inconclusive_when_ci_straddles():
    assert outcome((0.10, 0.30)) == "inconclusive:low|partial"
    assert outcome((0.20, 0.50)) == "inconclusive:partial|meaningful"
    assert outcome((0.10, 0.50)) == "inconclusive:low|meaningful"


def test_outcome_bands():
    assert outcome((0.42, 0.70)) == "meaningful"
    assert outcome((0.16, 0.39)) == "partial"
    assert outcome((0.0, 0.10)) == "low"


def test_recall_counts_only_same_defect_and_production_sites():
    labels, rset, cands, sd = fixture()
    r = recall(labels, rset, sd, None, candidates=cands)
    assert r["n"] == 3  # H-1, H-2, M-1; H-3 (test path) and H-4 (external) excluded
    assert r["full"]["k"] == 2  # related and non-production hits do not count
    assert r["strict"]["k"] == 1  # only H-1 is strict
    assert r["full"]["rate"] == pytest.approx(2 / 3)
    assert r["full"]["ci"] == list(wilson(2, 3))
    assert r["hit_ids"] == ["c:H-1", "c:H-2"]


def test_human_disagree_removes_hit_and_never_adds():
    labels, rset, cands, sd = fixture()
    base = recall(labels, rset, sd, None, candidates=cands)
    spot = [{"finding_id": "c:H-1", "candidate_index": 0, "verdict": "disagree", "reason": "x"},
            {"finding_id": "c:M-1", "candidate_index": 0, "verdict": "agree", "reason": "y"}]
    r = recall(labels, rset, sd, spot, candidates=cands)
    assert r["full_human"]["k"] == r["full"]["k"] - 1
    assert r["full"]["k"] == base["full"]["k"]
    assert "c:M-1" not in r["hit_ids_human"]  # 'agree' on a non-hit cannot add one


def test_human_disagree_on_one_of_two_candidates_keeps_hit():
    labels, rset, cands, sd = fixture()
    cands.append({"finding_id": "c:H-1", "index": 1, "closeness": "file"})
    sd.append({"finding_id": "c:H-1", "index": 1, "verdict": "same_defect"})
    spot = [{"finding_id": "c:H-1", "candidate_index": 0, "verdict": "disagree", "reason": "x"}]
    assert recall(labels, rset, sd, spot, candidates=cands)["full_human"]["k"] == 2


def test_miss_buckets_split_solo_and_empty():
    labels = [lrow("a", "in_class", "H", ["fork_upstream_diff"]),
              lrow("b", "adjacent", "M", ["fork_upstream_diff", "none_of_these"]),
              lrow("c", "in_class", "H", []),
              lrow("d", "in_class", "M", [], split=True),
              lrow("e", "out_of_class", "H", ["existing_detector_gap"]),
              lrow("f", "in_class", "H", ["existing_detector_gap"])]
    b = miss_buckets(labels, {"f"})
    t = b["tags"]
    assert t["fork_upstream_diff"] == {"H": 1, "M": 1, "solo": 1, "total": 2}
    assert t["none_of_these"]["M"] == 1 and t["none_of_these"]["solo"] == 0
    assert "existing_detector_gap" not in t  # out_of_class and hit rows excluded
    assert b["empty_tags"]["capability_split_false"] == {"H": 1, "M": 0}
    assert b["empty_tags"]["capability_split_true"] == {"H": 0, "M": 1}
    assert b["n_misses"] == 4
    assert next_build(b) == {"tie": False, "top": "fork_upstream_diff", "count": 2}


def test_next_build_tie_rule():
    r = next_build({"sibling_guard_asymmetry": 9, "fork_upstream_diff": 7})
    assert r["tie"] is True and r["preference"] == "no_new_parser"
    assert r["candidates"] == ["sibling_guard_asymmetry", "fork_upstream_diff"]
    r = next_build({"cross_file_or_dependency_visibility": 9, "fork_upstream_diff": 8})
    assert r["no_new_parser_candidates"] == ["fork_upstream_diff"]
    assert next_build({"sibling_guard_asymmetry": 10, "fork_upstream_diff": 7}) == \
        {"tie": False, "top": "sibling_guard_asymmetry", "count": 10}
    # none_of_these is excluded from ranking
    assert next_build({"none_of_these": 50, "fork_upstream_diff": 7,
                       "existing_detector_gap": 2})["top"] == "fork_upstream_diff"


def _agree_rows():
    def d(cls, pat, w):
        return {"class": cls, "pattern": pat, "would_catch_with": w}
    return [
        {"finding_id": "1", "calls": [call("A", d("in_class", "other", ["x"])),
                                      call("B", d("in_class", "other", ["x"]))]},
        {"finding_id": "2", "calls": [call("A", d("adjacent", "dead_state", [])),
                                      call("B", d("adjacent", "dead_state", []))]},
        {"finding_id": "3", "calls": [call("A", d("out_of_class", None, [])),
                                      call("B", d("out_of_class", None, []))]},
    ]


def test_kappa_perfect_agreement_is_one():
    a = agreement(_agree_rows())
    assert a["n_both_ok"] == 3
    assert a["class"] == {"n": 3, "raw": 1.0, "kappa": 1.0}
    assert a["pattern"]["kappa"] == 1.0 and a["pattern"]["raw"] == 1.0
    assert a["would_catch_with"]["mean_jaccard"] == 1.0  # empty vs empty counts as 1.0


def test_agreement_excludes_failed_readers_and_computes_kappa():
    def d(cls, w=()):
        return {"class": cls, "pattern": "other", "would_catch_with": list(w)}
    rows = [{"calls": [call("A", d("in_class", ["x"])), call("B", d("adjacent", ["x", "y"]))]},
            {"calls": [call("A", d("in_class")), call("B", d("in_class"))]},
            {"calls": [call("A", d("adjacent")), call("B", d("adjacent"))]},
            {"calls": [call("A", d("in_class")), call("B", None, status="refused")]}]
    a = agreement(rows)
    assert a["n_both_ok"] == 3
    assert a["class"]["raw"] == pytest.approx(2 / 3)
    # po=2/3; pa: in 2/3, adj 1/3; pb: in 1/3, adj 2/3; pe=2/9+2/9=4/9; kappa=(2/3-4/9)/(5/9)=0.4
    assert a["class"]["kappa"] == pytest.approx(0.4)
    assert a["would_catch_with"]["mean_jaccard"] == pytest.approx((0.5 + 1 + 1) / 3)
    assert a["pattern"]["n"] == 2


def test_class_coverage_counts_and_unlabeled():
    labels = [lrow("a", "in_class", "H"), lrow("b", "in_class", "M"), lrow("c", None, "H"),
              lrow("d", "out_of_class", "H")]
    cc = score.class_coverage(labels)
    assert cc["overall"]["in_class"] == {"count": 2, "share": 0.5}
    assert cc["overall"]["unlabeled"]["count"] == 1
    assert cc["by_severity"]["H"]["n"] == 3
    assert cc["by_severity"]["M"]["in_class"]["share"] == 1.0


def test_operational_counts_and_cost_use_llm_prices():
    labels = [{"finding_id": "1", "severity": "H",
               "final": {"class": None, "low_confidence": True, "capability_split": False},
               "calls": [call("A", None, status="refused", refusal_category="policy", fallback_ran=True),
                         call("B", None, model="claude-haiku-5-5", status="invalid")]}]
    sd = [{"finding_id": "1", "index": 0, "verdict": "unrelated",
           "call": call("S", {"verdict": "unrelated"})}]
    hits = {"contests": [{"name": "c", "findings": [1, 2], "all_findings_count": 5},
                         {"name": "d", "error": "boom"}]}
    op = score.operational(labels, sd, hits)
    assert op["refusals_by_model"] == {"claude-sonnet-5-5": {"policy": 1}}
    assert op["fallback_ran_by_model"] == {"claude-sonnet-5-5": 1}
    assert op["invalid_total"] == 1 and op["unlabeled"] == 1 and op["low_confidence"] == 1
    sonnet = op["tokens_and_cost_by_model"]["claude-sonnet-5-5"]
    pi, po = llm.PRICES["claude-sonnet-5-5"]
    assert sonnet["in_tok"] == 2000 and sonnet["est_usd"] == pytest.approx((2000 * pi + 200 * po) / 1e6)
    assert op["analyzer_by_contest"][0]["n_findings"] == 2 and op["analyzer_by_contest"][1]["error"] == "boom"


def test_main_writes_summary_and_prints_one_line(tmp_path, capsys):
    labels, rset, cands, sd = fixture()

    def w(name, obj):
        (tmp_path / name).write_text(json.dumps(obj), encoding="utf-8")

    w("labels_dev.json", {"set": "dev", "partial": False, "rows": labels, "errors": []})
    w("candidates_dev.json", {"set": "dev", "partial": False, "rows": cands})
    w("same_defect_dev.json", {"set": "dev", "partial": True, "rows": sd})
    w("hits_dev.json", {"set": "dev", "engine_commit": "abc", "contests": []})
    w("recall_set.json", rset)
    assert score.main(["--set", "dev", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.strip().count("\n") == 0 and "summary_dev.json" in out
    s = json.loads((tmp_path / "summary_dev.json").read_text(encoding="utf-8"))
    assert s["recall"]["full"]["k"] == 2 and s["outcome"]["based_on"] == "full"
    assert s["partial"]["same_defect"] is True
    assert s["outcome"]["value"] == "inconclusive:partial|meaningful"
    w("spot_check_dev.json", [{"finding_id": "c:H-1", "candidate_index": 0, "verdict": "disagree", "reason": "x"}])
    score.main(["--set", "dev", "--dir", str(tmp_path)])
    s = json.loads((tmp_path / "summary_dev.json").read_text(encoding="utf-8"))
    assert s["outcome"]["based_on"] == "full_human" and s["recall"]["full_human"]["k"] == 1
