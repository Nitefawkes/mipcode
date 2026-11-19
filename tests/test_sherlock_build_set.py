"""Contest eligibility, seeded draw, and ref resolution (no network)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "sherlock_recall"))

bs = importlib.import_module("build_set")


class FakeGh:
    def __init__(self, repos=(), readmes=None, first_dates=None, commits=None, judging=()):
        self.repos = set(repos)
        self.readmes = readmes or {}
        self.first_dates = first_dates or {}
        self.commits = commits or {}
        self.judging = list(judging)

    def list_judging_repos(self):
        return self.judging

    def readme(self, repo):
        return self.readmes.get(repo)

    def repo_exists(self, repo):
        return repo in self.repos

    def commit_before(self, repo, iso_date):
        return self.commits.get((repo, iso_date))

    def first_issue_date(self, repo):
        return self.first_dates.get(repo)


def report(contest_repo, ref, path="src/A.sol", sev="H", num=1):
    return (
        f"# Issue {sev}-{num}: Bug\n\nSource: https://github.com/x/y/issues/1\n\n"
        f"## Summary\ns\n\n## Root Cause\n"
        f"https://github.com/{contest_repo}/blob/{ref}/{path}#L10-L12\n\n## Impact\ni\n"
    )


def fake(name, ref="main", path="src/A.sol", readme="auto", repo_exists=True):
    comp = f"sherlock-audit/{name}"
    jr = f"{comp}-judging"
    text = report(comp, ref, path) if readme == "auto" else readme
    return FakeGh(
        repos=[comp] if repo_exists else [],
        readmes={jr: text},
        first_dates={jr: "2024-08-02T00:00:00Z"},
        commits={(comp, "2024-08-02T00:00:00Z"): "abc123", (comp, ""): "headsha"},
    )


def test_window_and_suffix():
    assert bs.contest_month("2024-08-cork-protocol") == "2024-08"
    assert bs.contest_month("cork") is None


def test_exclusion_is_own_code_only():
    assert bs.is_excluded("2024-03-morpho-blue") and bs.is_excluded("2025-01-uniswap-v4-hooks")
    assert bs.is_excluded("2024-03-morpho-blue-judging")
    assert bs.is_excluded("2024-06-1inch-foo")
    assert not bs.is_excluded("2024-10-morpho-x-index")
    assert not bs.is_excluded("2024-08-cork-protocol")


def test_ineligible_reasons():
    n = "2024-05-gone"
    assert bs.assess(n, fake(n, repo_exists=False))[0].reason == "companion_repo_missing"
    n = "2024-05-empty"
    assert bs.assess(n, fake(n, readme="# nothing here"))[0].reason == "no_high_or_medium"
    n = "2024-05-nil"
    assert bs.assess(n, fake(n, readme=None))[0].reason == "no_high_or_medium"
    n = "2024-05-rust"
    c, _ = bs.assess(n, fake(n, path="src/lib.rs"))
    assert c.reason == "non_solidity" and c.sol_share == 0.0 and not c.eligible
    assert bs.assess("2023-12-old", fake("2023-12-old"))[0].reason == "out_of_window"
    assert bs.assess("2024-03-morpho-blue", fake("2024-03-morpho-blue"))[0].reason == "excluded_protocol"


def test_zero_sites_is_non_solidity():
    n = "2024-05-nosites"
    txt = "# Issue H-1: t\n\n## Root Cause\nno links\n"
    c, fs = bs.assess(n, fake(n, readme=txt))
    assert c.reason == "non_solidity" and c.sol_share == 0.0 and c.n_findings == 1 and len(fs) == 1


def test_draw_is_deterministic_and_sorted_input():
    names = [f"2024-{i:02d}-p{i}" for i in range(1, 13)] + [f"2025-{i:02d}-q{i}" for i in range(1, 13)]
    dev, meas = bs.draw(list(reversed(names)))
    assert (dev, meas) == bs.draw(names)
    assert len(dev) == 4 and len(meas) == 16 and not set(dev) & set(meas)


def test_main_ref_resolves_to_commit_before_first_issue():
    n = "2024-08-x"
    c, fs = bs.assess(n, fake(n, ref="main"))
    assert c.eligible and c.reason == "eligible" and c.resolved_sha == "abc123"
    assert all(s.ref == "abc123" for f in fs for s in f.sites if not s.external)


def test_pinned_sha_is_kept():
    n = "2024-08-x"
    c, fs = bs.assess(n, fake(n, ref="db23bf6"))
    assert c.resolved_sha == "db23bf6" and fs[0].sites[0].ref == "db23bf6"


def test_external_site_ref_untouched_and_head_fallback():
    n = "2024-08-x"
    comp = f"sherlock-audit/{n}"
    txt = report(comp, "main") + "\n" + report("Uniswap/v3-core", "main", sev="M", num=2)
    g = fake(n, readme=txt)
    g.first_dates.clear()
    c, fs = bs.assess(n, g)
    assert c.resolved_sha == "headsha" and c.reason == "eligible|ref_fallback_head"
    ext = [s for f in fs for s in f.sites if s.external]
    assert ext and all(s.ref == "main" for s in ext)


def test_main_writes_outputs(tmp_path):
    names = [f"2024-{i:02d}-p{i}" for i in range(1, 13)]
    repos, readmes, dates, commits = [], {}, {}, {}
    for n in names:
        g = fake(n)
        repos += list(g.repos)
        readmes.update(g.readmes)
        dates.update(g.first_dates)
        commits.update(g.commits)
    src = FakeGh(repos, readmes, dates, commits, judging=[f"{n}-judging" for n in names] + ["2023-01-old-judging"])
    assert bs.main(["--out", str(tmp_path)], src=src) == 0
    cj = json.loads((tmp_path / "contests.json").read_text(encoding="utf-8"))
    rs = json.loads((tmp_path / "recall_set.json").read_text(encoding="utf-8"))
    assert len(cj["dev"]) == 4 and len(cj["measurement"]) == 8 and len(cj["contests"]) == 12
    assert [c["name"] for c in rs["contests"]] == cj["draw_order"]
    assert rs["contests"][0]["set"] == "dev" and rs["contests"][-1]["set"] == "measurement"
    assert rs["contests"][0]["findings"][0]["sites"][0]["ref"] == "abc123"
    dry = tmp_path / "dry"
    assert bs.main(["--out", str(dry), "--dry-list"], src=src) == 0
    assert (dry / "contests.json").exists() and not (dry / "recall_set.json").exists()
