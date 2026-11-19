"""Sherlock final-report parser: findings, sections, and code sites."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "sherlock_recall"))

sherlock_report = importlib.import_module("sherlock_report")
parse_permalink = sherlock_report.parse_permalink
parse_report = sherlock_report.parse_report
reader_sections = sherlock_report.reader_sections

FIX = ROOT / "tests" / "fixtures" / "sherlock"
OLD = (FIX / "old_format.md").read_text(encoding="utf-8")
NEW = (FIX / "new_format.md").read_text(encoding="utf-8")
MIXED = (FIX / "mixed_format.md").read_text(encoding="utf-8")
REPO = "sherlock-audit/2024-08-x"


def test_parse_permalink_variants():
    s = parse_permalink("https://github.com/sherlock-audit/2024-08-x/blob/db23bf6/src/A.sol#L10-L20", REPO)
    assert (s.path, s.line_start, s.line_end, s.ref, s.external) == ("src/A.sol", 10, 20, "db23bf6", False)
    assert s.repo == REPO
    assert parse_permalink("https://github.com/sherlock-audit/2024-08-x/blob/main/src/A.sol#L7", REPO).line_end == 7
    assert parse_permalink("https://github.com/sherlock-audit/2024-08-x/blob/main/src/A.sol", REPO).line_start is None
    assert parse_permalink("https://github.com/Uniswap/v3-core/blob/main/x.sol#L1", REPO).external is True
    assert parse_permalink("https://example.com/foo", REPO) is None
    assert parse_permalink("https://github.com/sherlock-audit/2024-08-x-judging/issues/66", REPO) is None


def _one_issue(root_cause: str) -> str:
    return (
        "# Issue H-1: t\n\nSource: https://github.com/sherlock-audit/2024-08-x-judging/issues/1\n\n"
        f"### Summary\ns\n\n### Root Cause\n{root_cause}\n\n### Impact\ni\n"
    )


def test_markdown_wrapped_links_are_cleaned():  # Review Focus 1
    md = "see [here](https://github.com/sherlock-audit/2024-08-x/blob/abc/src/My%20A.sol#L3)."
    sites = parse_report(_one_issue(md), "2024-08-x", REPO)[0].sites
    assert len(sites) == 1
    assert sites[0].path == "src/My A.sol" and sites[0].line_start == 3

    md = (
        "<https://github.com/sherlock-audit/2024-08-x/blob/abc/src/B.sol#L4-L6>, "
        "and `https://github.com/sherlock-audit/2024-08-x/blob/abc/src/C.sol#L9`."
    )
    sites = parse_report(_one_issue(md), "2024-08-x", REPO)[0].sites
    assert [(s.path, s.line_start, s.line_end) for s in sites] == [("src/B.sol", 4, 6), ("src/C.sol", 9, 9)]


def test_old_format_sites_from_code_snippet_only():
    fs = parse_report(OLD, "2024-08-x", REPO)
    assert [f.finding_id for f in fs] == ["2024-08-x:H-1", "2024-08-x:M-1"]
    assert all(f.fmt == "old" for f in fs)
    assert fs[0].severity == "H" and fs[0].number == 1
    assert fs[0].title == "Vault withdraw ignores slippage parameter"
    assert fs[0].source_url == "https://github.com/sherlock-audit/2024-08-x-judging/issues/66"
    assert [(s.path, s.line_start, s.line_end) for s in fs[0].sites] == [
        ("src/Vault.sol", 282, 282),
        ("src/lib/Math.sol", 10, 20),
    ]
    assert all(s.path.endswith(".sol") for s in fs[0].sites)
    assert not any("test/" in s.path for s in fs[0].sites)   # PoC/Recommendation links excluded
    assert not any(s.line_start == 50 for s in fs[0].sites)  # Vulnerability Detail unused when Code Snippet exists


def test_new_format_sites_from_root_cause_only():
    fs = parse_report(NEW, "2025-07-y", "sherlock-audit/2025-07-y")
    assert fs[0].fmt == "new" and len(fs[0].sites) == 2      # Mitigation + Discussion links excluded
    assert [s.path for s in fs[0].sites] == ["contracts/Router.sol", "contracts/Pool.sol"]
    assert "Discussion" in fs[0].sections


def test_old_format_without_code_snippet_falls_back_to_vulnerability_detail():
    fs = parse_report(OLD, "2024-08-x", REPO)
    assert "Code Snippet" not in fs[1].sections
    assert [(s.path, s.line_start) for s in fs[1].sites] == [("src/Config.sol", 7)]


def test_external_only_root_cause_keeps_finding_with_no_companion_sites():  # Review Focus 2
    fs = parse_report(NEW, "2025-07-y", "sherlock-audit/2025-07-y")
    assert len(fs) == 2
    assert fs[1].sites and all(s.external for s in fs[1].sites)


def test_mixed_formats_in_one_report():
    fs = parse_report(MIXED, "2024-09-z", "sherlock-audit/2024-09-z")
    assert [(f.finding_id, f.fmt) for f in fs] == [("2024-09-z:H-1", "old"), ("2024-09-z:M-2", "new")]
    assert [s.path for s in fs[0].sites] == ["src/A.sol"]
    assert [s.path for s in fs[1].sites] == ["src/B.sol"]


def test_reader_sections_exclude_poc_and_discussion():
    fs = parse_report(NEW, "2025-07-y", "sherlock-audit/2025-07-y")
    assert set(reader_sections(fs[0])) == {"Summary", "Root Cause", "Attack Path", "Impact", "Mitigation"}
    old = parse_report(OLD, "2024-08-x", REPO)
    assert set(reader_sections(old[0])) == {"Summary", "Vulnerability Detail", "Impact", "Recommendation"}
