"""Select eligible Sherlock contests, draw a seeded sample, and resolve refs."""
from __future__ import annotations

import argparse
import dataclasses
import json
import random
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sherlock_report import JudgedFinding, parse_report  # noqa: E402

ORG = "sherlock-audit"
WINDOW = ("2024-01", "2025-12")
SOL_THRESHOLD = 0.80
SEED = 20261118
K = 20
N_DEV = 4
EXCLUDED = frozenset({
    "twyne", "veda", "lombard", "strata", "origin", "termmax", "dhedge", "openzeppelin",
    "aqua", "uniswap", "morpho", "permit2", "solmate", "1inch", "sablier",
})
# Spec own-code rule: a contest is excluded only when it audits the protocol's own code.
# morpho-x-index is a different team's product that merely integrates Morpho, so it stays eligible.
ELIGIBLE_OVERRIDES = frozenset({"2024-10-morpho-x-index"})

MONTH_RE = re.compile(r"^(\d{4})-(\d{2})-")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
CACHE_DIR = Path(".mipdelta") / "sherlock_cache" / "gh"
_NONE = "\0NONE"


class GhSource(Protocol):
    def list_judging_repos(self) -> list[str]: ...
    def readme(self, repo: str) -> str | None: ...
    def repo_exists(self, repo: str) -> bool: ...
    def commit_before(self, repo: str, iso_date: str) -> str | None: ...  # "" means default-branch HEAD
    def first_issue_date(self, repo: str) -> str | None: ...


class GhError(RuntimeError):
    """A gh invocation failed for a reason other than a definitive 404."""


class CliGhSource:
    """GhSource backed by the `gh` CLI; raw responses are cached on disk (manual use only)."""

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _run(self, key: str, args: list[str]) -> str | None:
        """Return stdout, or None for empty output or a definitive 404. Other failures raise GhError."""
        path = self.cache_dir / (re.sub(r"[^A-Za-z0-9._-]+", "_", key) + ".txt")
        if path.exists():
            text = path.read_text(encoding="utf-8")
            return text if text != _NONE and text.strip() else None
        try:
            proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
        except FileNotFoundError as exc:
            raise GhError("gh executable not found") from exc
        if proc.returncode != 0:
            err = proc.stderr or ""
            if "HTTP 404" in err or "Not Found" in err:
                path.write_text(_NONE, encoding="utf-8")
                return None
            raise GhError(f"gh {' '.join(args[:2])} failed ({proc.returncode}): {err.strip()[:200]}")
        out = proc.stdout or ""
        path.write_text(out, encoding="utf-8")
        return out if out.strip() else None

    def list_judging_repos(self) -> list[str]:
        out = self._run("search_judging", [
            "api", "-X", "GET", "search/repositories", "-f", f"q=org:{ORG} judging in:name",
            "--paginate", "--jq", ".items[].name"])
        names = sorted({n for n in (out or "").split() if n.endswith("-judging")})
        if not names:
            (self.cache_dir / "search_judging.txt").unlink(missing_ok=True)
            raise GhError("judging repo search returned no repositories")
        return names


    def readme(self, repo: str) -> str | None:
        return self._run(f"readme_{repo}", [
            "api", f"repos/{repo}/readme", "-H", "Accept: application/vnd.github.raw"])

    def repo_exists(self, repo: str) -> bool:
        return self._run(f"exists_{repo}", ["api", f"repos/{repo}", "--jq", ".full_name"]) is not None

    def commit_before(self, repo: str, iso_date: str) -> str | None:
        args = ["api", "-X", "GET", f"repos/{repo}/commits", "-f", "per_page=1", "--jq", ".[0].sha"]
        if iso_date:
            args += ["-f", f"until={iso_date}"]
        out = self._run(f"commit_{repo}_{iso_date}", args)
        return (out.strip() or None) if out else None

    def first_issue_date(self, repo: str) -> str | None:
        out = self._run(f"firstissue_{repo}", [
            "api", "-X", "GET", f"repos/{repo}/issues", "-f", "state=all", "-f", "sort=created",
            "-f", "direction=asc", "-f", "per_page=1", "--jq", ".[0].created_at"])
        return (out.strip() or None) if out else None


@dataclass
class Contest:
    name: str
    judging_repo: str
    companion_repo: str
    eligible: bool
    reason: str
    sol_share: float
    n_findings: int
    resolved_sha: str | None


def contest_month(name: str) -> str | None:
    m = MONTH_RE.match(name)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _strip(name: str) -> str:
    name = name.rsplit("/", 1)[-1]
    return name[: -len("-judging")] if name.endswith("-judging") else name


def is_excluded(name: str) -> bool:
    name = _strip(name)
    if name in ELIGIBLE_OVERRIDES:
        return False
    tokens = MONTH_RE.sub("", name, count=1).split("-")
    return tokens[0] in EXCLUDED


def _sol_share(findings: list[JudgedFinding]) -> float:
    sites = [s for f in findings for s in f.sites]
    if not sites:
        return 0.0
    return sum(1 for s in sites if s.path.lower().endswith(".sol")) / len(sites)


def assess(name: str, src: GhSource) -> tuple[Contest, list[JudgedFinding]]:
    name = _strip(name)
    judging = f"{ORG}/{name}-judging"
    companion = f"{ORG}/{name}"

    def out(eligible, reason, share=0.0, findings=(), sha=None):
        return Contest(name, judging, companion, eligible, reason, share, len(findings), sha), list(findings)

    month = contest_month(name)
    if month is None or not (WINDOW[0] <= month <= WINDOW[1]):
        return out(False, "out_of_window")
    if is_excluded(name):
        return out(False, "excluded_protocol")
    if not src.repo_exists(companion):
        return out(False, "companion_repo_missing")
    readme = src.readme(judging)
    findings = parse_report(readme, name, companion) if readme else []
    if not findings:
        return out(False, "no_high_or_medium")
    share = _sol_share(findings)
    if share < SOL_THRESHOLD:
        return out(False, "non_solidity", share, findings)

    reason = "eligible"
    pins = {s.ref for f in findings for s in f.sites if not s.external and SHA_RE.match(s.ref)}
    if len(pins) == 1:
        sha = next(iter(pins))
    else:
        if len(pins) > 1:
            reason += "|mixed_pins"
        date = src.first_issue_date(judging)
        sha = src.commit_before(companion, date) if date else None
        if sha is None:
            sha = src.commit_before(companion, "")
            reason += "|ref_fallback_head"
    if sha is None:
        return out(False, "ref_unresolved", share, findings)

    def fix(site):
        if site.external or SHA_RE.match(site.ref):
            return site
        return dataclasses.replace(site, ref=sha)

    findings = [dataclasses.replace(f, sites=tuple(fix(s) for s in f.sites)) for f in findings]
    return out(True, reason, share, findings, sha)


def draw(eligible_names: list[str], seed: int = SEED, k: int = K) -> tuple[list[str], list[str]]:
    drawn = random.Random(seed).sample(sorted(eligible_names), min(k, len(eligible_names)))
    return drawn[:N_DEV], drawn[N_DEV:]


def _write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def main(argv=None, src: GhSource | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-list", action="store_true")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = src or CliGhSource()

    names = sorted({_strip(r) for r in src.list_judging_repos() if r.endswith("-judging")})
    names = [n for n in names if WINDOW[0] <= (contest_month(n) or "") <= WINDOW[1]]
    assessed = {}
    for n in names:
        try:
            assessed[n] = assess(n, src)
        except Exception as exc:  # per-contest isolation: record and continue
            assessed[n] = (Contest(n, f"{ORG}/{n}-judging", f"{ORG}/{n}", False,
                                   f"assess_error:{type(exc).__name__}", 0.0, 0, None), [])
    contests = [dataclasses.asdict(c) for c, _ in assessed.values()]
    eligible = [n for n, (c, _) in assessed.items() if c.eligible]

    if args.dry_list:
        _write(out_dir / "contests.json", {"contests": contests, "n_eligible": len(eligible)})
        return 0

    dev, meas = draw(eligible)
    _write(out_dir / "contests.json", {
        "contests": contests, "n_eligible": len(eligible), "seed": SEED,
        "draw_order": dev + meas, "dev": dev, "measurement": meas})
    _write(out_dir / "recall_set.json", {"contests": [
        {"name": n, "set": "dev" if n in dev else "measurement",
         "companion_repo": assessed[n][0].companion_repo, "resolved_sha": assessed[n][0].resolved_sha,
         "findings": [dataclasses.asdict(f) for f in assessed[n][1]]}
        for n in dev + meas]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
