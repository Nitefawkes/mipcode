"""Summarize mip-delta event logs (*.events.jsonl) recorded by the hooks and ledger checks.

Usage:
    python experiments/dogfood/summarize_events.py REPO_OR_LOG [REPO_OR_LOG ...] [--json]

A directory argument is searched recursively for *.events.jsonl, including .mipdelta/.
Each argument is reported as one repository; all arguments are also pooled.

One event log belongs to one ledger, so it is counted as one session. A hook ledger
holds one Claude Code session; a shared --check-git ledger is one long-running stream.

A discharge is classified from the discharging invocation's edited_path:
  site_file_edited   the edited file is the file that held the site
  other_file_edited  another file was edited, for example the contract change was reverted
  unknown            no edited file, as at a stop hook or a --check-git run
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

DISCHARGE_KINDS = ("site_file_edited", "other_file_edited", "unknown")


def find_logs(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(path for path in target.rglob("*.events.jsonl") if path.is_file())


def read_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    bad = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1  # a write cut short by a crash leaves a partial last line
                continue
            if isinstance(row, dict) and "event" in row:
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def _invocations(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group consecutive rows of one invocation; each invocation is appended in one write."""
    groups: list[list[dict[str, Any]]] = []
    last_key: object = object()
    for index, row in enumerate(rows):
        key = row.get("invocation") or ("line", index)
        if not groups or key != last_key:
            groups.append([])
        groups[-1].append(row)
        last_key = key
    return groups


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(round(fraction * len(ordered), 9)))  # nearest-rank percentile
    return ordered[min(rank, len(ordered)) - 1]


def _latency(values: list[float]) -> dict[str, Any]:
    return {
        "runs": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


class Stats:
    def __init__(self) -> None:
        self.sessions = 0
        self.session_ids: set[str] = set()
        self.bad_lines = 0
        self.runs_by_source: Counter[str] = Counter()
        self.opened = 0
        self.discharged: Counter[str] = Counter({kind: 0 for kind in DISCHARGE_KINDS})
        self.waived = 0
        self.still_open = 0
        self.stop_blocks = 0
        self.edits_to_discharge: list[int] = []
        self.by_detector: dict[str, Counter[str]] = defaultdict(Counter)
        self.wall_ms: dict[bool, list[float]] = {True: [], False: []}

    def add_log(self, rows: list[dict[str, Any]], bad: int) -> None:
        self.sessions += 1
        self.bad_lines += bad
        open_sites: dict[str, tuple[str, int]] = {}  # id -> (detector, post-edit count at open)
        post_edits = 0
        for group in _invocations(rows):
            run = next((row for row in group if row["event"] == "run"), None)
            source = run.get("source") if run else ("waive" if group[0]["event"] == "waived" else None)
            if source == "claude-post-edit":
                post_edits += 1
            for row in group:
                if row.get("session"):
                    self.session_ids.add(str(row["session"]))
                kind = row["event"]
                if kind == "opened":
                    self.opened += 1
                    detector = str(row.get("detector"))
                    self.by_detector[detector]["opened"] += 1
                    open_sites[str(row.get("id"))] = (detector, post_edits)
                elif kind == "discharged":
                    edited = row.get("edited_path")
                    if not edited:
                        how = "unknown"
                    elif edited == row.get("path"):
                        how = "site_file_edited"
                    else:
                        how = "other_file_edited"
                    self.discharged[how] += 1
                    self.by_detector[str(row.get("detector"))]["discharged"] += 1
                    site = open_sites.pop(str(row.get("id")), None)
                    if site is not None:
                        self.edits_to_discharge.append(post_edits - site[1])
                elif kind == "waived":
                    self.waived += 1
                    site = open_sites.pop(str(row.get("id")), None)
                    self.by_detector[site[0] if site else "unknown"]["waived"] += 1
                elif kind == "stop_blocked":
                    self.stop_blocks += 1
                elif kind == "run":
                    self.runs_by_source[str(row.get("source"))] += 1
                    wall = row.get("wall_ms")
                    if isinstance(wall, (int, float)):
                        self.wall_ms[bool(row.get("workspace_parsed"))].append(float(wall))
        self.still_open += len(open_sites)
        for detector, _ in open_sites.values():
            self.by_detector[detector]["still_open"] += 1

    def merge(self, other: "Stats") -> None:
        self.sessions += other.sessions
        self.session_ids |= other.session_ids
        self.bad_lines += other.bad_lines
        self.runs_by_source.update(other.runs_by_source)
        self.opened += other.opened
        self.discharged.update(other.discharged)
        self.waived += other.waived
        self.still_open += other.still_open
        self.stop_blocks += other.stop_blocks
        self.edits_to_discharge += other.edits_to_discharge
        for detector, counts in other.by_detector.items():
            self.by_detector[detector].update(counts)
        for key in (True, False):
            self.wall_ms[key] += other.wall_ms[key]

    def to_dict(self) -> dict[str, Any]:
        edits = self.edits_to_discharge
        return {
            "sessions": self.sessions,
            "distinct_session_ids": len(self.session_ids),
            "runs_by_source": dict(sorted(self.runs_by_source.items())),
            "opened": self.opened,
            "discharged": {"total": sum(self.discharged.values()), **{k: self.discharged[k] for k in DISCHARGE_KINDS}},
            "waived": self.waived,
            "still_open_at_last_event": self.still_open,
            "stop_blocks": self.stop_blocks,
            "edits_open_to_discharge": {
                "count": len(edits),
                "median": statistics.median(edits) if edits else None,
                "max": max(edits) if edits else None,
            },
            "by_detector": {name: dict(sorted(counts.items())) for name, counts in sorted(self.by_detector.items())},
            "latency_ms": {
                "workspace_parsed": _latency(self.wall_ms[True]),
                "changed_files_only": _latency(self.wall_ms[False]),
            },
            "unreadable_lines": self.bad_lines,
        }


def summarize(targets: Iterable[Path]) -> dict[str, Any]:
    pooled = Stats()
    repositories: dict[str, Any] = {}
    for target in targets:
        stats = Stats()
        for log in find_logs(target):
            stats.add_log(*read_events(log))
        repositories[str(target)] = stats.to_dict()
        pooled.merge(stats)
    return {"repositories": repositories, "pooled": pooled.to_dict()}


def _fmt(value: Any) -> str:
    return "-" if value is None else (f"{value:.1f}" if isinstance(value, float) else str(value))


def render(name: str, data: dict[str, Any]) -> str:
    d = data["discharged"]
    e = data["edits_open_to_discharge"]
    runs = ", ".join(f"{k} {v}" for k, v in data["runs_by_source"].items()) or "none"
    lines = [
        f"== {name}",
        f"sessions: {data['sessions']} ({data['distinct_session_ids']} distinct session ids)",
        f"runs: {runs}",
        f"opened: {data['opened']}",
        f"discharged: {d['total']} (site file edited {d['site_file_edited']}, "
        f"other file edited {d['other_file_edited']}, unknown {d['unknown']})",
        f"waived: {data['waived']}",
        f"still open at last event: {data['still_open_at_last_event']}",
        f"stop blocks: {data['stop_blocks']}",
        f"edits from open to discharge: median {_fmt(e['median'])}, max {_fmt(e['max'])} (n={e['count']})",
    ]
    for label, key in (("whole workspace", "workspace_parsed"), ("changed files only", "changed_files_only")):
        lat = data["latency_ms"][key]
        lines.append(
            f"latency {label}: n={lat['runs']} p50 {_fmt(lat['p50'])} ms, "
            f"p95 {_fmt(lat['p95'])} ms, max {_fmt(lat['max'])} ms"
        )
    for detector, counts in data["by_detector"].items():
        lines.append("  " + detector + ": " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if data["unreadable_lines"]:
        lines.append(f"unreadable lines skipped: {data['unreadable_lines']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("targets", nargs="+", type=Path, help="Repository roots or *.events.jsonl files")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args(argv)
    missing = [str(path) for path in args.targets if not path.exists()]
    if missing:
        parser.error("not found: " + ", ".join(missing))
    result = summarize(args.targets)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        blocks = [render(name, data) for name, data in result["repositories"].items()]
        if len(result["repositories"]) > 1:
            blocks.append(render("pooled", result["pooled"]))
        print("\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
