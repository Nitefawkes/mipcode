"""Blind class labeling, consensus, and same-defect checks (no network, no clones)."""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "sherlock_recall"))

label = importlib.import_module("label")
llm = importlib.import_module("llm")
from sherlock_report import JudgedFinding, Site  # noqa: E402

PROMPTS = ROOT / "experiments" / "sherlock_recall" / "prompts"
SPEC = ROOT / "docs" / "superpowers" / "specs" / "2026-11-18-sherlock-recall-set-design.md"
MITIGATION_LINE = (
    "Classify the root-cause defect described in the Summary and Root Cause (or Vulnerability Detail). "
    "Use the Mitigation or Recommendation section only to locate a peer or base site that already handles "
    "this correctly. Ignore extra hardening suggestions there that are unrelated to the root cause; they do "
    "not change the class, pattern, or capabilities."
)
CLASSES = ["in_class", "adjacent", "out_of_class"]
PATTERNS = ["param_not_forwarded", "param_unread", "sibling_missing_guard", "override_mismatch",
            "dead_state", "paired_op_mismatch", "branch_zero_value", "hardcoded_safety_param", "other"]
CAPABILITIES = ["sibling_guard_asymmetry", "safety_param_weighting", "cross_file_or_dependency_visibility",
                "fork_upstream_diff", "existing_detector_gap", "none_of_these"]
SONNET, HAIKU, OPUS = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"
REPO = "sherlock-audit/2025-01-demo"
SHA = "a" * 40


def h16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def src_lines(n: int, tag: str = "line") -> str:
    return "\n".join(f"{tag} {i}" for i in range(1, n + 1)) + "\n"


def make_finding(fid="2025-01-demo:H-1", sites=None, sections=None, fmt="new", title="Fee not forwarded"):
    if sites is None:
        sites = (Site(REPO, SHA, "src/Vault.sol", 50, 52, False),)
    if sections is None:
        sections = {
            "Summary": "SUMMARY_TEXT",
            "Root Cause": "ROOT_CAUSE_TEXT",
            "Attack Path": "ATTACK_TEXT",
            "Impact": "IMPACT_TEXT",
            "Mitigation": "MITIGATION_TEXT",
            "PoC": "POC_SECRET_TEXT",
            "Discussion": "DISCUSSION_SECRET_TEXT",
        }
    sev = fid.split(":")[1][0]
    return JudgedFinding(fid, sev, int(fid.split("-")[-1]), title, "https://x", fmt, sections, tuple(sites))


def lab(cls="in_class", pattern="param_unread", wcw=("safety_param_weighting",), obl=None):
    return {"class": cls, "pattern": pattern, "would_catch_with": list(wcw),
            "obligation_site": obl, "rationale": "r"}


def ok(data, model=SONNET):
    return llm.CallResult("ok", data, model, False, None, 100, 50)


def refused(model=SONNET):
    return llm.CallResult("refused", None, model, False, "cyber", 100, 0)


class FakeReader:
    """Scripted stand-in for llm.Reader: returns per-model results and records calls."""

    def __init__(self, by_model=None, same_defect=None, raise_after=None):
        self.by_model = by_model or {}
        self.same_defect = same_defect
        self.raise_after = raise_after
        self.calls: list[dict] = []

    def call(self, model, effort, system, user, schema, key):
        if self.raise_after is not None and len(self.calls) >= self.raise_after:
            raise llm.BudgetExceeded("cap")
        self.calls.append(dict(model=model, effort=effort, system=system, user=user, schema=schema, key=key))
        if schema is label.SAME_DEFECT_SCHEMA:
            res = self.same_defect
            return res(user) if callable(res) else res
        res = self.by_model[model]
        return res(user) if callable(res) else res


# ---------------------------------------------------------------- reader input


def test_class_input_has_no_poc_discussion_or_analyzer():
    f = make_finding()
    sources = {"src/Vault.sol": src_lines(100)}
    text = label.build_class_input(f, sources)
    assert "Fee not forwarded" in text
    for shown in ("SUMMARY_TEXT", "ROOT_CAUSE_TEXT", "ATTACK_TEXT", "IMPACT_TEXT", "MITIGATION_TEXT"):
        assert shown in text
    assert "POC_SECRET_TEXT" not in text and "DISCUSSION_SECRET_TEXT" not in text
    for analyzer_word in ("parameter_sink_review", "detector", "analyzer", "resolution_source"):
        assert analyzer_word not in text
    # build_class_input takes no analyzer argument at all
    assert list(label.build_class_input.__code__.co_varnames[:2]) == ["f", "sources"]


def test_class_input_context_is_plus_minus_20_lines():
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 50, 52, False),))
    text = label.build_class_input(f, {"src/Vault.sol": src_lines(100)})
    assert "src/Vault.sol#L50-L52" in text
    assert "line 30" in text and "line 72" in text
    assert "line 29\n" not in text and "line 73" not in text
    # line numbers are rendered alongside the code
    assert any(ln.strip().startswith("30") and ln.endswith("line 30") for ln in text.splitlines())


def test_class_input_clamps_context_at_file_edges():
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 3, 3, False),))
    text = label.build_class_input(f, {"src/Vault.sol": src_lines(10)})
    assert "line 1\n" in text and "line 10" in text


def test_class_input_unanchored_missing_and_external_sites():
    sites = (
        Site(REPO, SHA, "src/Whole.sol", None, None, False),
        Site(REPO, SHA, "src/Gone.sol", 5, 6, False),
        Site("OpenZeppelin/oz", "main", "contracts/ERC20.sol", 1, 2, True),
    )
    f = make_finding(sites=sites)
    text = label.build_class_input(f, {"src/Whole.sol": src_lines(100, "whole")})
    assert "whole 60" in text and "whole 61" not in text
    assert "src/Gone.sol#L5-L6" in text and "[source unavailable]" in text
    assert "contracts/ERC20.sol" not in text


def test_class_input_old_format_sections():
    sections = {"Summary": "S", "Vulnerability Detail": "VD_TEXT", "Impact": "I",
                "Code Snippet": "SNIPPET_LINKS", "Recommendation": "REC_TEXT", "Discussion": "DISC"}
    f = make_finding(sections=sections, fmt="old")
    text = label.build_class_input(f, {})
    assert "VD_TEXT" in text and "REC_TEXT" in text
    assert "SNIPPET_LINKS" not in text and "DISC" not in text


# ---------------------------------------------------------------- prompts / schemas


def test_prompt_contains_rubric_and_mitigation_line_verbatim():
    text = (PROMPTS / "class_reader.md").read_text(encoding="utf-8")
    assert MITIGATION_LINE in text and all(t in text for t in PATTERNS + CAPABILITIES)


def test_prompt_carries_spec_section3_definitions_verbatim():
    """Every rubric bullet of spec §3 (class, pattern, obligation_site, would_catch_with) appears verbatim."""
    spec = SPEC.read_text(encoding="utf-8").splitlines()
    start = spec.index("Each reader assigns:")
    block = []
    for line in spec[start + 1:]:
        if line.startswith("  Readers tag this blind"):
            break
        if line.strip():
            block.append(line.replace(" *(builder: may add tags on the dev set only, then freeze)*", ""))
    assert len(block) == 13
    text = (PROMPTS / "class_reader.md").read_text(encoding="utf-8")
    for line in block:
        assert line in text, line
    assert "builder" not in text


def test_schemas_enums_and_strictness():
    s = label.CLASS_SCHEMA
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"]) == {
        "class", "pattern", "would_catch_with", "obligation_site", "rationale"}
    assert s["properties"]["class"]["enum"] == CLASSES
    assert s["properties"]["pattern"]["enum"] == PATTERNS + [None]
    assert s["properties"]["pattern"]["type"] == ["string", "null"]
    assert s["properties"]["would_catch_with"]["items"]["enum"] == CAPABILITIES
    assert s["properties"]["obligation_site"]["type"] == ["string", "null"]
    d = label.SAME_DEFECT_SCHEMA
    assert d["additionalProperties"] is False
    assert set(d["required"]) == set(d["properties"]) == {"verdict", "rationale"}
    assert d["properties"]["verdict"]["enum"] == ["same_defect", "related", "unrelated"]


def test_prompt_sha_changes_with_prompt_or_schema():
    a = label.prompt_sha("prompt", {"x": 1})
    assert a == label.prompt_sha("prompt", {"x": 1})
    assert a != label.prompt_sha("prompt!", {"x": 1})
    assert a != label.prompt_sha("prompt", {"x": 2})


# ---------------------------------------------------------------- consensus


def test_consensus_majority_and_multiselect():
    a = {"class": "in_class", "pattern": "param_unread", "would_catch_with": ["safety_param_weighting"]}
    b = {"class": "in_class", "pattern": "param_unread", "would_catch_with": ["safety_param_weighting", "fork_upstream_diff"]}
    assert consensus_of([a, b])["needs_tiebreak"] is True
    c = {"class": "in_class", "pattern": "param_unread", "would_catch_with": ["fork_upstream_diff"]}
    out = consensus_of([a, b, c])
    assert out["would_catch_with"] == ["fork_upstream_diff", "safety_param_weighting"]
    assert out["class"] == "in_class" and out["pattern"] == "param_unread" and out["n_readers"] == 3


def consensus_of(labels):
    return label.consensus(labels)


def test_consensus_two_agreeing_readers():
    a = lab(wcw=("fork_upstream_diff", "safety_param_weighting"), obl="src/A.sol#L10")
    b = lab(wcw=("safety_param_weighting", "fork_upstream_diff"))
    out = label.consensus([a, b])
    assert out == {"class": "in_class", "pattern": "param_unread",
                   "would_catch_with": ["fork_upstream_diff", "safety_param_weighting"],
                   "obligation_site": "src/A.sol#L10", "needs_tiebreak": False, "n_readers": 2,
                   "n_valid": 2, "low_confidence": False, "capability_split": False}


def test_consensus_drops_tag_chosen_by_one_reader():
    a = lab(wcw=("safety_param_weighting",))
    b = lab(wcw=("safety_param_weighting", "fork_upstream_diff"))
    assert label.consensus([a, b])["would_catch_with"] == ["safety_param_weighting"]
    c = lab(wcw=("existing_detector_gap",))
    assert label.consensus([a, b, c])["would_catch_with"] == ["safety_param_weighting"]
    # with A refused, both remaining class-matching readers must choose a tag
    out = label.consensus([None, b, c])
    assert out["would_catch_with"] == [] and out["capability_split"] is True


# --- Ruling R16: pattern and tags are conditioned on the final class


def test_r16_tags_split_between_class_matching_readers():
    a = lab(cls="in_class", wcw=("sibling_guard_asymmetry",))
    b = lab(cls="out_of_class", pattern=None, wcw=())
    c = lab(cls="in_class", wcw=("fork_upstream_diff",))
    out = label.consensus([a, b, c])
    assert out["class"] == "in_class" and out["would_catch_with"] == [] and out["capability_split"] is True
    assert out["low_confidence"] is False


def test_r16_no_class_majority_out_of_class_tiebreaker_nulls_pattern():
    a = lab(cls="in_class", pattern="param_unread")
    b = lab(cls="adjacent", pattern="param_unread")
    c = lab(cls="out_of_class", pattern=None, wcw=())
    out = label.consensus([a, b, c])
    assert out["class"] == "out_of_class" and out["pattern"] is None and out["would_catch_with"] == []
    assert out["capability_split"] is False


def test_r16_out_of_class_final_forces_null_pattern_and_no_tags():
    a = lab(cls="out_of_class", pattern="other", wcw=("none_of_these",))
    b = lab(cls="out_of_class", pattern="other", wcw=("none_of_these",))
    out = label.consensus([a, b])
    assert out["class"] == "out_of_class" and out["pattern"] is None and out["would_catch_with"] == []


def test_r16_pattern_and_tags_only_from_class_matching_readers():
    # B's pattern/tags must not leak into the final label: B disagrees on class
    a = lab(cls="adjacent", pattern="hardcoded_safety_param", wcw=("safety_param_weighting",))
    b = lab(cls="in_class", pattern="param_unread", wcw=("existing_detector_gap",))
    c = lab(cls="adjacent", pattern="hardcoded_safety_param", wcw=("safety_param_weighting",))
    out = label.consensus([a, b, c])
    assert out["class"] == "adjacent" and out["pattern"] == "hardcoded_safety_param"
    assert out["would_catch_with"] == ["safety_param_weighting"]


def test_r16_pattern_tie_uses_tiebreaker_when_it_matches_class():
    a = lab(pattern="param_unread", wcw=("safety_param_weighting",))
    b = lab(pattern="dead_state", wcw=("safety_param_weighting",))
    c = lab(pattern="param_not_forwarded", wcw=("safety_param_weighting",))
    out = label.consensus([a, b, c])
    assert out["pattern"] == "param_not_forwarded"


def test_r16_pattern_tie_falls_back_to_first_matching_reader():
    a = lab(pattern="param_unread", wcw=("safety_param_weighting",))
    b = lab(pattern="dead_state", wcw=("safety_param_weighting",))
    c = lab(cls="out_of_class", pattern=None, wcw=())
    out = label.consensus([a, b, c])
    assert out["class"] == "in_class" and out["pattern"] == "param_unread"
    assert out["would_catch_with"] == ["safety_param_weighting"] and out["capability_split"] is False


def test_r16_single_class_matching_reader_supplies_its_tags():
    a = lab(cls="adjacent", pattern="other", wcw=("cross_file_or_dependency_visibility",))
    b = lab(cls="out_of_class", pattern=None, wcw=())
    c = lab(cls="adjacent", pattern="hardcoded_safety_param", wcw=())
    out = label.consensus([a, b, c])
    # two readers match adjacent; no tag reaches 2 votes -> split
    assert out["class"] == "adjacent" and out["would_catch_with"] == [] and out["capability_split"] is True
    # A refused, B and C split on class -> tie-breaker C decides; C is the only matching reader
    b2 = lab(cls="adjacent", pattern="other", wcw=("safety_param_weighting",))
    c2 = lab(cls="in_class", pattern="param_unread", wcw=("existing_detector_gap", "fork_upstream_diff"))
    out2 = label.consensus([None, b2, c2])
    assert out2["class"] == "in_class" and out2["pattern"] == "param_unread"
    assert out2["would_catch_with"] == ["existing_detector_gap", "fork_upstream_diff"]
    assert out2["capability_split"] is False and out2["low_confidence"] is False
    # two matching readers agree on one tag, extra single-vote tags dropped
    d = lab(cls="adjacent", pattern="other", wcw=("cross_file_or_dependency_visibility",))
    e = lab(cls="in_class", wcw=("sibling_guard_asymmetry",))
    g = lab(cls="adjacent", pattern="other", wcw=("cross_file_or_dependency_visibility", "none_of_these"))
    out4 = label.consensus([e, d, g])
    assert out4["would_catch_with"] == ["cross_file_or_dependency_visibility"]


def test_r16_exactly_one_matching_reader_tags_used():
    a = lab(cls="in_class", pattern="param_unread", wcw=("existing_detector_gap", "fork_upstream_diff"))
    b = lab(cls="adjacent", pattern="other", wcw=("safety_param_weighting",))
    c = lab(cls="in_class", pattern="dead_state", wcw=("safety_param_weighting",))
    # in_class by majority (A, C); now a 2-label case where only A is valid and matches
    out = label.consensus([a, None])
    assert out["class"] == "in_class"
    assert out["would_catch_with"] == ["existing_detector_gap", "fork_upstream_diff"]
    assert out["capability_split"] is False
    out3 = label.consensus([a, b, c])
    assert out3["pattern"] == "dead_state"  # pattern tie among A, C -> tie-breaker C matches class


def test_r16_one_valid_label_of_three_is_taken_in_full_with_low_confidence():
    b = lab(cls="adjacent", pattern="branch_zero_value", wcw=("existing_detector_gap", "none_of_these"),
            obl="src/P.sol#L3")
    out = label.consensus([None, b, None])
    assert out["class"] == "adjacent" and out["pattern"] == "branch_zero_value"
    assert out["would_catch_with"] == ["existing_detector_gap", "none_of_these"]
    assert out["obligation_site"] == "src/P.sol#L3"
    assert out["low_confidence"] is True and out["capability_split"] is False and out["n_valid"] == 1


def test_r16_tiebreaker_refused_while_a_and_b_disagree_on_class():
    a = lab(cls="in_class")
    b = lab(cls="adjacent", pattern="other")
    out = label.consensus([a, b, None])
    assert out["class"] is None and out["pattern"] is None and out["would_catch_with"] == []
    assert out["obligation_site"] is None
    assert out["low_confidence"] is False and out["n_valid"] == 2


def test_r16_both_matching_readers_choose_no_tags_is_not_a_split():
    out = label.consensus([lab(wcw=()), lab(wcw=())])
    assert out["would_catch_with"] == [] and out["capability_split"] is False


def test_consensus_two_readers_disagree_on_class_or_pattern():
    assert label.consensus([lab(), lab(cls="adjacent")])["needs_tiebreak"] is True
    assert label.consensus([lab(), lab(pattern="dead_state")])["needs_tiebreak"] is True


def test_refused_reader_forces_tiebreak():
    a = lab()
    assert label.consensus([a, None])["needs_tiebreak"]
    assert label.consensus([None, a])["needs_tiebreak"]
    assert label.consensus([None, None])["needs_tiebreak"]


def test_consensus_three_way_split_uses_tiebreaker():
    a, b, c = lab(cls="in_class"), lab(cls="adjacent", pattern="other"), lab(cls="out_of_class", pattern=None, wcw=())
    out = label.consensus([a, b, c])
    assert out["class"] == "out_of_class" and out["pattern"] is None


def test_consensus_majority_beats_tiebreaker():
    a = lab(cls="adjacent", pattern="hardcoded_safety_param")
    b = lab(cls="out_of_class", pattern=None, wcw=())
    c = lab(cls="adjacent", pattern="hardcoded_safety_param")
    out = label.consensus([a, b, c])
    assert out["class"] == "adjacent" and out["pattern"] == "hardcoded_safety_param"


def test_consensus_refused_a_with_agreeing_b_and_c():
    b = lab(cls="adjacent", pattern="hardcoded_safety_param", wcw=("safety_param_weighting",), obl=None)
    c = lab(cls="adjacent", pattern="hardcoded_safety_param", wcw=("safety_param_weighting",), obl="src/B.sol#L4")
    out = label.consensus([None, b, c])
    assert out["class"] == "adjacent" and out["pattern"] == "hardcoded_safety_param"
    assert out["would_catch_with"] == ["safety_param_weighting"]
    assert out["obligation_site"] == "src/B.sol#L4"
    assert out["n_readers"] == 3 and out["n_valid"] == 2


def test_consensus_obligation_site_only_from_readers_matching_final_class():
    a = lab(cls="out_of_class", pattern=None, wcw=(), obl="src/Wrong.sol#L1")
    b = lab(obl=None)
    c = lab(obl="src/Right.sol#L9")
    out = label.consensus([a, b, c])
    assert out["class"] == "in_class" and out["obligation_site"] == "src/Right.sol#L9"


def test_consensus_all_refused():
    out = label.consensus([None, None, None])
    assert out["class"] is None and out["pattern"] is None and out["would_catch_with"] == []
    assert out["n_valid"] == 0


# ---------------------------------------------------------------- label_findings


def test_tiebreak_only_called_on_disagreement():
    f1 = make_finding("2025-01-demo:H-1")
    f2 = make_finding("2025-01-demo:M-1")
    sources = {"src/Vault.sol": src_lines(100)}

    agree = FakeReader({SONNET: ok(lab()), HAIKU: ok(lab(), HAIKU)})
    rows = label.label_findings([f1], sources, agree)
    assert [c["model"] for c in agree.calls] == [SONNET, HAIKU]
    assert [c["effort"] for c in agree.calls] == ["low", "low"]
    assert rows[0]["final"]["class"] == "in_class" and len(rows[0]["calls"]) == 2

    disagree = FakeReader({SONNET: ok(lab()), HAIKU: ok(lab(cls="adjacent"), HAIKU),
                           OPUS: ok(lab(cls="adjacent"), OPUS)})
    rows = label.label_findings([f2], sources, disagree)
    assert [c["model"] for c in disagree.calls] == [SONNET, HAIKU, OPUS]
    assert disagree.calls[2]["effort"] == "medium"
    row = rows[0]
    assert row["finding_id"] == "2025-01-demo:M-1" and row["contest"] == "2025-01-demo"
    assert row["severity"] == "M" and row["final"]["class"] == "adjacent"
    assert [c["reader"] for c in row["calls"]] == ["A", "B", "C"]
    assert row["calls"][1]["data"]["class"] == "adjacent"
    # identical blind prompt for all three readers: the tie-breaker never sees other labels
    assert len({(c["system"], c["user"]) for c in disagree.calls}) == 1
    assert "in_class" not in disagree.calls[2]["user"]
    assert all(c["schema"] is label.CLASS_SCHEMA for c in disagree.calls)


def test_label_cache_keys_include_model_finding_and_prompt_sha():
    f = make_finding()
    r = FakeReader({SONNET: ok(lab()), HAIKU: ok(lab(), HAIKU)})
    label.label_findings([f], {}, r)
    sha = label.prompt_sha(label.CLASS_PROMPT, label.CLASS_SCHEMA)
    user = label.build_class_input(f, {})
    assert r.calls[0]["user"] == user
    assert [c["key"] for c in r.calls] == [f"{SONNET}:{f.finding_id}:{sha}:{h16(user)}",
                                          f"{HAIKU}:{f.finding_id}:{sha}:{h16(user)}"]


def test_label_cache_key_changes_when_reader_input_changes():
    f = make_finding()
    r1 = FakeReader({SONNET: ok(lab()), HAIKU: ok(lab(), HAIKU)})
    r2 = FakeReader({SONNET: ok(lab()), HAIKU: ok(lab(), HAIKU)})
    label.label_findings([f], {}, r1)
    label.label_findings([f], {"src/Vault.sol": src_lines(100)}, r2)
    assert r1.calls[0]["key"] != r2.calls[0]["key"]


def test_refused_reader_a_gets_label_from_b_and_c():
    f = make_finding()
    b = lab(cls="adjacent", pattern="hardcoded_safety_param")
    r = FakeReader({SONNET: refused(), HAIKU: ok(b, HAIKU), OPUS: ok(b, OPUS)})
    rows = label.label_findings([f], {}, r)
    assert [c["model"] for c in r.calls] == [SONNET, HAIKU, OPUS]
    final = rows[0]["final"]
    assert final["class"] == "adjacent" and final["pattern"] == "hardcoded_safety_param"
    assert rows[0]["calls"][0]["status"] == "refused" and rows[0]["calls"][0]["refusal_category"] == "cyber"


def test_out_of_enum_label_is_treated_as_invalid():
    f = make_finding()
    bad = lab(cls="bogus")
    r = FakeReader({SONNET: ok(bad), HAIKU: ok(lab(), HAIKU), OPUS: ok(lab(), OPUS)})
    rows = label.label_findings([f], {}, r)
    assert len(r.calls) == 3 and rows[0]["final"]["class"] == "in_class"


# ---------------------------------------------------------------- candidates & same-defect


VAULT = """pragma solidity ^0.8.20;

contract Vault {
    function deposit(uint256 a) external {
        uint256 x = a;
    }
}
"""


def analyzer(path, start, detector="parameter_sink_review", symbol="Vault.deposit"):
    return {"detector": detector, "symbol": symbol, "path": path, "start_line": start,
            "end_line": start, "resolution_source": "x", "evidence": ["param `a` unread"]}


def label_row(f, cls="in_class", obl=None):
    final = label.consensus([lab(cls=cls, obl=obl), lab(cls=cls, obl=obl)])
    return {"finding_id": f.finding_id, "contest": "2025-01-demo", "severity": f.severity,
            "final": final, "calls": []}


def test_candidates_use_final_obligation_site():
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    a_obl = analyzer("src/Base.sol", 4)
    a_other = analyzer("src/Unrelated.sol", 4)
    sources = {"src/Vault.sol": VAULT, "src/Base.sol": VAULT, "src/Unrelated.sol": VAULT}
    rows = [label_row(f, obl="src/Base.sol#L4")]
    cands = label.build_candidates(rows, [f], [a_obl, a_other], sources)
    assert cands == [{"finding_id": f.finding_id, "index": 0, "closeness": "file", "site": None,
                      "analyzer": a_obl}]

    reader = FakeReader(same_defect=ok({"verdict": "same_defect", "rationale": "same"}))
    checked = label.check_candidates(cands, [f], sources, reader)
    assert len(reader.calls) == 1
    call = reader.calls[0]
    assert call["model"] == SONNET and call["effort"] == "medium"
    sha = label.prompt_sha(label.SAME_DEFECT_PROMPT, label.SAME_DEFECT_SCHEMA)
    assert call["key"] == f"{SONNET}:{f.finding_id}:0:{sha}:{h16(call['user'])}"
    assert checked[0]["finding_id"] == f.finding_id and checked[0]["index"] == 0
    assert checked[0]["verdict"] == "same_defect" and checked[0]["rationale"] == "same"
    assert checked[0]["call"]["status"] == "ok"


REPO_FILES = ["src/Vault.sol", "src/Base.sol", "src/Unrelated.sol", "src/a/Twin.sol", "src/b/Twin.sol"]


@pytest.mark.parametrize("obl", [
    f"https://github.com/{REPO}/blob/{SHA}/src/Base.sol#L4",
    f"https://github.com/{REPO}/blob/main/src/Base.sol#L4-L9",
    "<https://github.com/sherlock-audit/2025-01-demo/blob/main/src%2FBase.sol#L4>",
    "Base.sol#L4",
    "Base.sol",
    "`Base.sol:L4`",
    "./src\\Base.sol#L4",
])
def test_obligation_site_forms_resolve_to_repo_path(obl):
    assert label.resolve_obligation_path(obl, REPO_FILES) == ("src/Base.sol", None)


def test_obligation_site_permalink_and_bare_name_yield_candidates():
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    a_obl = analyzer("src/Base.sol", 4)
    for obl in (f"https://github.com/{REPO}/blob/{SHA}/src/Base.sol#L4", "Base.sol#L4"):
        errors: list = []
        cands = label.build_candidates([label_row(f, obl=obl)], [f], [a_obl], {"src/Base.sol": VAULT},
                                       repo_files=REPO_FILES, errors=errors)
        assert [(c["closeness"], c["analyzer"]["path"]) for c in cands] == [("file", "src/Base.sol")], obl
        assert errors == []


def test_unresolved_or_ambiguous_obligation_site_is_recorded():
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    for obl, why in (("Missing.sol#L1", "unresolved"), ("Twin.sol#L2", "ambiguous")):
        errors: list = []
        label.build_candidates([label_row(f, obl=obl)], [f], [analyzer("src/a/Twin.sol", 2)], {},
                               repo_files=REPO_FILES, errors=errors)
        assert len(errors) == 1 and errors[0]["finding_id"] == f.finding_id
        assert errors[0]["stage"] == "obligation_site" and why in errors[0]["error"]
        assert errors[0]["obligation_site"] == obl


def test_candidates_without_obligation_site_skip_obligation_file():
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    rows = [label_row(f, obl=None)]
    cands = label.build_candidates(rows, [f], [analyzer("src/Base.sol", 4)], {"src/Base.sol": VAULT})
    assert cands == []


def test_candidates_strict_and_index_order_and_site_asdict():
    site = Site(REPO, SHA, "src/Vault.sol", 5, 5, False)
    f = make_finding(sites=(site,))
    a1, a2 = analyzer("src/Vault.sol", 5), analyzer("./src\\Vault.sol", 30)
    cands = label.build_candidates([label_row(f)], [f], [a1, a2], {"src/Vault.sol": VAULT})
    assert [c["index"] for c in cands] == [0, 1]
    assert cands[0]["closeness"] == "strict" and cands[0]["site"] == asdict(site)
    assert cands[1]["closeness"] == "file"
    json.dumps(cands)  # serializable


def test_same_defect_only_for_in_class_and_adjacent():
    f_in = make_finding("2025-01-demo:H-1", sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    f_adj = make_finding("2025-01-demo:H-2", sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    f_out = make_finding("2025-01-demo:H-3", sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    rows = [label_row(f_in), label_row(f_adj, cls="adjacent"), label_row(f_out, cls="out_of_class")]
    cands = label.build_candidates(rows, [f_in, f_adj, f_out], [analyzer("src/Vault.sol", 5)],
                                   {"src/Vault.sol": VAULT})
    assert [c["finding_id"] for c in cands] == ["2025-01-demo:H-1", "2025-01-demo:H-2"]


def test_same_defect_input_contents():
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    a = analyzer("src/Other.sol", 40, detector="state_sink_review", symbol="Other.total")
    cand = {"finding_id": f.finding_id, "index": 0, "closeness": "file",
            "site": asdict(f.sites[0]), "analyzer": a}
    sources = {"src/Vault.sol": src_lines(100, "vault"), "src/Other.sol": src_lines(100, "other")}
    text = label.build_same_defect_input(f, cand, sources)
    for s in ("ROOT_CAUSE_TEXT", "MITIGATION_TEXT", "state_sink_review", "Other.total",
              "src/Other.sol", "40", "param `a` unread", "vault 5", "other 30", "other 50"):
        assert s in text, s
    assert "other 29\n" not in text and "other 51" not in text
    assert "POC_SECRET_TEXT" not in text and "DISCUSSION_SECRET_TEXT" not in text


def test_same_defect_failed_call_records_null_verdict():
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    cand = {"finding_id": f.finding_id, "index": 2, "closeness": "strict",
            "site": asdict(f.sites[0]), "analyzer": analyzer("src/Vault.sol", 5)}
    reader = FakeReader(same_defect=refused())
    out = label.check_candidates([cand], [f], {"src/Vault.sol": VAULT}, reader)
    assert out[0]["index"] == 2 and out[0]["verdict"] is None and out[0]["call"]["status"] == "refused"


# ---------------------------------------------------------------- main


def _write_inputs(tmp_path: Path, findings, analyzer_findings):
    recall = {"contests": [{"name": "2025-01-demo", "set": "dev", "companion_repo": REPO,
                            "resolved_sha": SHA, "findings": [asdict(f) for f in findings]},
                           {"name": "2025-02-other", "set": "measurement", "companion_repo": REPO,
                            "resolved_sha": SHA, "findings": []}]}
    hits = {"set": "dev", "engine_commit": "3ebdabd", "contests": [
        {"name": "2025-01-demo", "companion_repo": REPO, "resolved_sha": SHA, "findings": analyzer_findings}]}
    (tmp_path / "recall_set.json").write_text(json.dumps(recall), encoding="utf-8")
    (tmp_path / "hits_dev.json").write_text(json.dumps(hits), encoding="utf-8")
    clone = tmp_path / "clone"
    (clone / "src").mkdir(parents=True)
    (clone / "src" / "Vault.sol").write_text(VAULT, encoding="utf-8")
    return clone


def test_main_writes_three_files(tmp_path, monkeypatch):
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    clone = _write_inputs(tmp_path, [f], [analyzer("src/Vault.sol", 5)])
    monkeypatch.setattr(label, "clone_at", lambda repo, sha, cache: clone)
    reader = FakeReader({SONNET: ok(lab()), HAIKU: ok(lab(), HAIKU)},
                        same_defect=ok({"verdict": "related", "rationale": "near"}))
    rc = label.main(["--set", "dev", "--out", str(tmp_path)], reader=reader)
    assert rc == 0
    labels = json.loads((tmp_path / "labels_dev.json").read_text(encoding="utf-8"))
    cands = json.loads((tmp_path / "candidates_dev.json").read_text(encoding="utf-8"))
    sd = json.loads((tmp_path / "same_defect_dev.json").read_text(encoding="utf-8"))
    assert labels["set"] == "dev" and labels["partial"] is False
    assert [r["finding_id"] for r in labels["rows"]] == [f.finding_id]
    assert "uint256 x = a;" in reader.calls[0]["user"]  # source came from the clone
    assert cands["rows"][0]["closeness"] == "strict" and cands["partial"] is False
    assert sd["rows"][0]["verdict"] == "related" and sd["rows"][0]["index"] == 0


def test_main_budget_exceeded_writes_partial_and_returns_2(tmp_path, monkeypatch):
    f1 = make_finding("2025-01-demo:H-1", sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    f2 = make_finding("2025-01-demo:H-2", sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    clone = _write_inputs(tmp_path, [f1, f2], [])
    monkeypatch.setattr(label, "clone_at", lambda repo, sha, cache: clone)
    reader = FakeReader({SONNET: ok(lab()), HAIKU: ok(lab(), HAIKU)}, raise_after=3)
    rc = label.main(["--set", "dev", "--out", str(tmp_path)], reader=reader)
    assert rc == 2
    for name in ("labels_dev.json", "candidates_dev.json", "same_defect_dev.json"):
        payload = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        assert payload["partial"] is True and payload["set"] == "dev"
    labels = json.loads((tmp_path / "labels_dev.json").read_text(encoding="utf-8"))
    assert [r["finding_id"] for r in labels["rows"]] == ["2025-01-demo:H-1"]


def test_main_clone_failure_skips_contest_and_records_error(tmp_path, monkeypatch):
    """Ruling R15(a): no code-less labels are paid for or cached."""
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    _write_inputs(tmp_path, [f], [])

    def boom(repo, sha, cache):
        raise RuntimeError("git fetch failed for C:\\secret\\path")

    monkeypatch.setattr(label, "clone_at", boom)
    reader = FakeReader({SONNET: ok(lab()), HAIKU: ok(lab(), HAIKU)})
    assert label.main(["--set", "dev", "--out", str(tmp_path)], reader=reader) == 0
    assert reader.calls == []
    labels = json.loads((tmp_path / "labels_dev.json").read_text(encoding="utf-8"))
    assert labels["rows"] == []
    clone_errors = [e for e in labels["errors"] if e["stage"] == "clone"]
    assert clone_errors and clone_errors[0]["contest"] == "2025-01-demo"
    assert "secret" not in json.dumps(labels)


def test_main_resolves_permalink_obligation_site_against_clone(tmp_path, monkeypatch):
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    a_obl = analyzer("pkg/core/Base.sol", 4)
    clone = _write_inputs(tmp_path, [f], [a_obl])
    (clone / "pkg" / "core").mkdir(parents=True)
    (clone / "pkg" / "core" / "Base.sol").write_text(VAULT, encoding="utf-8")
    monkeypatch.setattr(label, "clone_at", lambda repo, sha, cache: clone)
    obl = f"https://github.com/{REPO}/blob/{SHA}/core/Base.sol#L4"
    reader = FakeReader({SONNET: ok(lab(obl=obl)), HAIKU: ok(lab(obl=obl), HAIKU)},
                        same_defect=ok({"verdict": "same_defect", "rationale": "x"}))
    assert label.main(["--set", "dev", "--out", str(tmp_path)], reader=reader) == 0
    cands = json.loads((tmp_path / "candidates_dev.json").read_text(encoding="utf-8"))["rows"]
    assert [(c["closeness"], c["analyzer"]["path"]) for c in cands] == [("file", "pkg/core/Base.sol")]


def test_main_records_unresolved_obligation_site(tmp_path, monkeypatch):
    f = make_finding(sites=(Site(REPO, SHA, "src/Vault.sol", 5, 5, False),))
    clone = _write_inputs(tmp_path, [f], [])
    monkeypatch.setattr(label, "clone_at", lambda repo, sha, cache: clone)
    reader = FakeReader({SONNET: ok(lab(obl="Nowhere.sol#L1")), HAIKU: ok(lab(obl="Nowhere.sol#L1"), HAIKU)})
    assert label.main(["--set", "dev", "--out", str(tmp_path)], reader=reader) == 0
    labels = json.loads((tmp_path / "labels_dev.json").read_text(encoding="utf-8"))
    errs = [e for e in labels["errors"] if e["stage"] == "obligation_site"]
    assert len(errs) == 1 and errs[0]["finding_id"] == f.finding_id and errs[0]["contest"] == "2025-01-demo"


def test_load_sources_refuses_paths_outside_clone(tmp_path):
    clone = tmp_path / "clone"
    (clone / "src").mkdir(parents=True)
    (clone / "src" / "A.sol").write_text("a", encoding="utf-8")
    (tmp_path / "Outside.sol").write_text("x", encoding="utf-8")
    out = label.load_sources(clone, ["src/A.sol", "../Outside.sol", "src/Missing.sol", "./src\\A.sol"])
    assert out == {"src/A.sol": "a"}
