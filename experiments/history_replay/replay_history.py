"""Replay a repository's commit history through mip-delta without running any model.

For every non-merge commit that changes Python, the parent tree is the baseline and the
commit tree is the edit. The replay records:

- how many commits change a contract that other files can depend on,
- how many of those the check stays silent on (upstream considered the commit complete),
- for each reported site, whether a later commit resolved it by editing that site.

A report that disappears when upstream later edits the enclosing function is evidence
that it pointed at work the original commit left undone. A site still reported at the
horizon is either a false report or a latent problem, and needs reading to tell which.

The target repository is read through Git plumbing only. Nothing is checked out, imported,
or executed. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mipdelta.cache import FactCache  # noqa: E402
from mipdelta.delta import analyze_changed_only, analyze_delta  # noqa: E402
from mipdelta.ledger import enclosing_function  # noqa: E402
from mipdelta.model import Finding  # noqa: E402


warnings.simplefilter("ignore", SyntaxWarning)  # historical sources, never executed


class GitReader:
    """Reads commits, trees, and blobs from one repository."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._batch = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        self._texts: dict[str, str | None] = {}
        self._trees: dict[str, dict[str, str]] = {}

    def close(self) -> None:
        self._batch.stdin.close()
        self._batch.wait(timeout=30)

    def _run(self, *args: str) -> bytes:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, check=True).stdout

    def commits(self) -> list[tuple[str, str, str]]:
        """Non-merge commits oldest first as (sha, parent, subject), where the parent tree is present."""
        raw = self._run("log", "--no-merges", "--reverse", "--format=%H%x00%P%x00%s").decode("utf-8", "replace")
        rows = []
        for line in raw.splitlines():
            sha, parents, subject = (line.split("\x00") + ["", ""])[:3]
            parent = parents.split(" ")[0] if parents else ""
            if parent and self._exists(parent):
                rows.append((sha, parent, subject))
        return rows

    def _exists(self, sha: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "-e", f"{sha}^{{tree}}"], capture_output=True, check=False
        )
        return proc.returncode == 0

    def _blob_text(self, blob: str) -> str | None:
        if blob not in self._texts:
            self._batch.stdin.write(blob.encode("ascii") + b"\n")
            self._batch.stdin.flush()
            header = self._batch.stdout.readline().split()
            if len(header) != 3:
                self._texts[blob] = None
            else:
                data = self._batch.stdout.read(int(header[2]))
                self._batch.stdout.read(1)
                try:
                    self._texts[blob] = data.decode("utf-8")
                except UnicodeDecodeError:
                    self._texts[blob] = None
        return self._texts[blob]

    def python_blobs(self, commit: str) -> dict[str, str]:
        """Path to blob id for every Python file in a commit."""
        if commit not in self._trees:
            rows: dict[str, str] = {}
            for entry in self._run("ls-tree", "-r", "-z", commit).split(b"\0"):
                if not entry:
                    continue
                meta, _, path = entry.partition(b"\t")
                name = path.decode("utf-8", "replace")
                if name.endswith(".py") and meta.split()[1] == b"blob":
                    rows[name] = meta.split()[2].decode("ascii")
            self._trees[commit] = rows
            if len(self._trees) > 64:
                self._trees.pop(next(iter(self._trees)))
        return self._trees[commit]

    def texts(self, blobs: dict[str, str], paths: Any = None) -> dict[str, str]:
        out = {}
        for path in blobs if paths is None else paths:
            if path in blobs:
                text = self._blob_text(blobs[path])
                if text is not None:
                    out[path] = text
        return out


def is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    leaf = parts[-1]
    return (
        any(part in {"tests", "test", "testing"} for part in parts[:-1])
        or leaf.startswith("test_")
        or leaf.endswith("_test.py")
        or leaf == "conftest.py"
    )


def is_support_path(path: str) -> bool:
    return path.lower().split("/")[0] in {"docs", "doc", "examples", "benchmarks", "scripts", "tasks"}


def site_key(finding: Finding, anchor: str) -> tuple[str, str, str, str]:
    return (finding.detector, finding.symbol, finding.path, anchor)


def analyze(reader: GitReader, cache: FactCache, base: str, commit: str) -> tuple[bool, list[tuple[Finding, str]]]:
    """Findings for base -> commit with the enclosing function of each site."""
    before_blobs, after_blobs = reader.python_blobs(base), reader.python_blobs(commit)
    changed = sorted(p for p in before_blobs.keys() | after_blobs.keys() if before_blobs.get(p) != after_blobs.get(p))
    if not changed:
        return False, []
    before_changed, after_changed = reader.texts(before_blobs, changed), reader.texts(after_blobs, changed)
    findings, cross_file = analyze_changed_only(before_changed, after_changed, cache=cache)
    after = after_changed
    if cross_file:
        before, after = reader.texts(before_blobs), reader.texts(after_blobs)
        findings = analyze_delta(before, after, cache=cache, stub_files={})
    rows = []
    for finding in findings:
        text = after.get(finding.path)
        facts = cache.load(finding.path, text) if text is not None else None
        rows.append((finding, enclosing_function(facts, finding.start_line)))
    return cross_file, rows


def resolution(
    reader: GitReader, cache: FactCache, key: tuple[str, str, str, str], prior: dict[str, str], later: dict[str, str]
) -> str:
    """How a later commit made a reported site disappear."""
    path, anchor = key[2], key[3]
    if prior.get(path) == later.get(path):
        return "target_side_change"
    if not anchor:
        return "file_edit"
    slices = []
    for blobs in (prior, later):
        text = reader.texts(blobs, [path]).get(path)
        function = cache.load(path, text).functions.get(anchor) if text is not None else None
        slices.append(function.source_slice if function else None)
    return "site_function_edit" if slices[0] != slices[1] else "same_file_other_edit"


def replay_repository(repo: Path, horizon: int, limit: int | None) -> dict[str, Any]:
    reader = GitReader(repo)
    cache = FactCache()
    started = time.perf_counter()
    try:
        commits = reader.commits()
        if limit:
            commits = commits[-limit:]
        counts: Counter[str] = Counter()
        flagged: list[dict[str, Any]] = []
        parses_at_last_clear = 0
        for position, (sha, parent, subject) in enumerate(commits):
            before_blobs, after_blobs = reader.python_blobs(parent), reader.python_blobs(sha)
            changed = [
                p for p in before_blobs.keys() | after_blobs.keys() if before_blobs.get(p) != after_blobs.get(p)
            ]
            if not any(not is_test_path(p) and not is_support_path(p) for p in changed):
                continue
            counts["commits_changing_production_python"] += 1
            if cache.stats.parses - parses_at_last_clear > 4000:
                # Parsed files keep their syntax trees; bound memory on long histories.
                cache.clear()
                parses_at_last_clear = cache.stats.parses
            try:
                cross_file, rows = analyze(reader, cache, parent, sha)
            except (RecursionError, ValueError):
                counts["analysis_errors"] += 1
                continue
            if cross_file:
                counts["contract_changing_commits"] += 1
            production = [(f, a) for f, a in rows if not is_test_path(f.path) and not is_support_path(f.path)]
            if not production:
                if cross_file:
                    counts["contract_changing_commits_silent"] += 1
                continue
            if cross_file:
                counts["contract_changing_commits_flagged"] += 1
            else:
                counts["local_commits_flagged"] += 1

            open_sites = Counter(site_key(f, a) for f, a in production)
            outcome: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            previous = sha
            for step, (later, _, later_subject) in enumerate(commits[position + 1: position + 1 + horizon], 1):
                if not open_sites:
                    break
                try:
                    _, later_rows = analyze(reader, cache, parent, later)
                except (RecursionError, ValueError):
                    break
                still = Counter(site_key(f, a) for f, a in later_rows)
                prior_blobs, later_blobs = reader.python_blobs(previous), reader.python_blobs(later)
                for key in [k for k in open_sites if still.get(k, 0) < open_sites[k]]:
                    outcome[key] = {
                        "resolved_after_commits": step,
                        "resolved_by": resolution(reader, cache, key, prior_blobs, later_blobs),
                        "resolving_commit": later[:12],
                        "resolving_subject": later_subject[:100],
                    }
                    del open_sites[key]
                previous = later
            sites = []
            for finding, anchor in production:
                key = site_key(finding, anchor)
                result = outcome.get(key, {"resolved_after_commits": None, "resolved_by": "open_at_horizon"})
                sites.append(
                    {
                        "detector": finding.detector,
                        "symbol": finding.symbol,
                        "path": finding.path,
                        "line": finding.start_line,
                        "anchor": anchor,
                        "provenance": finding.resolution_source,
                        "evidence": finding.evidence[0] if finding.evidence else "",
                        **result,
                    }
                )
            flagged.append(
                {"commit": sha[:12], "subject": subject[:100], "contract_changing": cross_file, "sites": sites}
            )
        return {
            "repository": repo.name,
            "head": commits[-1][0][:12] if commits else None,
            "commits_replayed": len(commits),
            "horizon_commits": horizon,
            "wall_seconds": round(time.perf_counter() - started, 1),
            **{name: counts[name] for name in (
                "commits_changing_production_python",
                "contract_changing_commits",
                "contract_changing_commits_silent",
                "contract_changing_commits_flagged",
                "local_commits_flagged",
                "analysis_errors",
            )},
            "flagged_commits": flagged,
        }
    finally:
        reader.close()


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    by_detector: dict[str, Counter[str]] = {}
    for report in reports:
        for commit in report["flagged_commits"]:
            for site in commit["sites"]:
                by_detector.setdefault(site["detector"], Counter())[site["resolved_by"]] += 1
    totals = Counter()
    for report in reports:
        for name, value in report.items():
            if isinstance(value, int) and name != "horizon_commits":
                totals[name] += value
    return {
        "totals": dict(totals),
        "sites_by_detector": {
            detector: {"sites": sum(rows.values()), **dict(sorted(rows.items()))}
            for detector, rows in sorted(by_detector.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, action="append", required=True, help="Git repository to replay (repeatable)")
    parser.add_argument("--horizon", type=int, default=25, help="Later commits examined for each reported site")
    parser.add_argument("--limit", type=int, default=None, help="Replay only the most recent N commits")
    parser.add_argument("--out", type=Path, required=True, help="Directory for one JSON report per repository")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    reports = []
    for repo in args.repo:
        report = replay_repository(repo.resolve(), args.horizon, args.limit)
        (args.out / f"{report['repository']}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        reports.append(report)
        print(
            f"{report['repository']}: {report['commits_changing_production_python']} commits, "
            f"{report['contract_changing_commits']} contract-changing, "
            f"{report['contract_changing_commits_flagged']} flagged, {report['wall_seconds']} s",
            file=sys.stderr,
        )
    summary = summarize(reports)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
