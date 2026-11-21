"""Analyzer-finding to judged-site hit matching."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "sherlock_recall"))

match_hits = importlib.import_module("match_hits")
sherlock_report = importlib.import_module("sherlock_report")
norm_path = match_hits.norm_path
is_production_sol = match_hits.is_production_sol
candidates_for = match_hits.candidates_for
Site = sherlock_report.Site

REPO = "sherlock-audit/2024-08-x"
# Line map: 4 state var; 6-9 modifier onlyOwner; 11-21 function f; 23-25 function g.
SRC = "\n".join([
    "pragma solidity ^0.8.0;",
    "",
    "contract A {",
    "    uint256 public x;",
    "",
    "    modifier onlyOwner() {",
    "        require(msg.sender != address(0));",
    "        _;",
    "    }",
    "",
    "    function f(uint256 a) external onlyOwner {",
    "        x = a;",
    "        x += 1;",
    "        x += 2;",
    "        x += 3;",
    "        x += 4;",
    "        x += 5;",
    "        x += 6;",
    "        x += 7;",
    "        x += 8;",
    "    }",
    "",
    "    function g() external {",
    "        x = 0;",
    "    }",
    "}",
]) + "\n"


def site(path="src/A.sol", a=14, b=14, external=False):
    return Site(REPO, "abc", path, a, b, external)


def finding(start, end=None, path="src/A.sol", detector="d"):
    return {"detector": detector, "symbol": "s", "path": path, "start_line": start,
            "end_line": end if end is not None else start, "resolution_source": "x", "evidence": ""}


def run(findings, sites, obligation=None):
    return candidates_for("c:H-1", sites, obligation, findings, {"src/A.sol": SRC})


def test_strict_within_three_lines():
    s = site(a=14, b=14)
    near = run([finding(17)], [s])
    assert [c.closeness for c in near] == ["strict"] and near[0].site == s
    assert run([finding(11)], [s])[0].closeness == "strict"
    far = run([finding(18)], [s])
    assert far[0].closeness == "function" and far[0].site == s


def test_declaration_line_counts_as_strict():
    c = run([finding(4)], [site(a=4, b=4)])
    assert c[0].closeness == "strict"
    assert run([finding(4)], [site(a=12, b=12)])[0].closeness == "file"


def test_function_hit_uses_parsed_bodies():
    s = site(a=12, b=12)
    assert run([finding(20)], [s])[0].closeness == "function"
    assert run([finding(24)], [s])[0].closeness == "file"
    assert run([finding(8)], [site(a=12, b=12)])[0].closeness == "file"
    long_mod = "contract B {\n modifier m() {\n  a();\n  b();\n  c();\n  d();\n  e();\n  _;\n }\n}\n"
    out = candidates_for("c:H-1", [site("src/B.sol", 3, 3)], None, [finding(8, path="src/B.sol")],
                         {"src/B.sol": long_mod})
    assert out[0].closeness == "function"


def test_file_level_in_obligation_file():
    out = run([finding(5)], [], obligation="./src/A.sol")
    assert len(out) == 1 and out[0].closeness == "file" and out[0].site is None
    assert run([finding(5, path="src/Other.sol")], [site()], obligation="src/A.sol") == []


def test_paths_normalize_before_matching():
    assert norm_path(".\\src\\A.sol") == "src/A.sol" == norm_path("./src/A.sol")
    assert norm_path("././src/A.sol") == "src/A.sol"
    assert norm_path("/src/A.sol") == "src/A.sol"
    out = run([finding(14, path=".\\src\\A.sol")], [site(path="./src/A.sol", a=14, b=14)])
    assert out[0].closeness == "strict"


def test_non_production_and_external_ignored():
    assert not is_production_sol("test/A.t.sol") and not is_production_sol("lib/oz/ERC20.sol")
    assert not is_production_sol("src/Foo.s.sol") and not is_production_sol("src/Mocks/M.sol")
    assert not is_production_sol("src/A.txt")
    assert is_production_sol("src/A.sol")
    assert run([finding(14, path="test/A.t.sol")], [site(path="test/A.t.sol")]) == []
    assert run([finding(14)], [site(external=True)]) == []


def test_unanchored_site_yields_only_file_closeness():
    s = site(a=None, b=None)
    out = run([finding(14)], [s])
    assert out[0].closeness == "file" and out[0].site == s


def test_one_candidate_per_finding_picks_matching_site():
    s1, s2 = site(a=40, b=41), site(a=14, b=14)
    out = run([finding(15), finding(100)], [s1, s2])
    assert [(c.closeness, c.site) for c in out] == [("strict", s2), ("file", s1)]
