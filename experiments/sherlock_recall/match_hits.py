"""Match analyzer findings to judged Sherlock bug sites by closeness."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mipdelta.solidity import (  # noqa: E402
    _MODIFIER_HDR_RE,
    _find_matching,
    _line_of,
    parse_solidity_file,
)
from sherlock_report import Site  # noqa: E402

_EXCLUDED_SEGMENTS = {"test", "tests", "script", "scripts", "mock", "mocks", "lib", "node_modules"}
STRICT_MARGIN = 3


def norm_path(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p[1:] if p.startswith("/") else p


def is_production_sol(path: str) -> bool:
    p = norm_path(path)
    if not p.endswith(".sol") or p.endswith(".t.sol") or p.endswith(".s.sol"):
        return False
    return not any(seg.lower() in _EXCLUDED_SEGMENTS for seg in p.split("/"))


@dataclass(frozen=True)
class Candidate:
    finding_id: str
    analyzer: dict
    site: Site | None
    closeness: str  # strict | function | file


def _body_ranges(path: str, source: str) -> list[tuple[int, int]]:
    """Line ranges of every function and modifier body in a file."""
    facts = parse_solidity_file(path, source)
    ranges: list[tuple[int, int]] = []
    for contract in facts.contracts:
        for fn in contract.functions:
            if fn.has_body:
                ranges.append((fn.start_line, fn.end_line))
    masked = facts.masked_source
    for m in _MODIFIER_HDR_RE.finditer(masked):
        nxt = re.search(r"[{;]", masked[m.end():])
        if not nxt or nxt.group() == ";":
            continue
        open_pos = m.end() + nxt.start()
        close = _find_matching(masked, open_pos, "{", "}")
        if close < 0:
            continue
        ranges.append((_line_of(masked, m.start()), _line_of(masked, close)))
    return ranges


def candidates_for(
    finding_id: str,
    sites: list[Site],
    obligation_path: str | None,
    analyzer_findings: list[dict],
    sources: dict[str, str],
) -> list[Candidate]:
    norm_sources = {norm_path(k): v for k, v in sources.items()}
    live = [(norm_path(s.path), s) for s in sites if not s.external]
    cited = {p for p, _ in live}
    obl = norm_path(obligation_path) if obligation_path else None
    range_cache: dict[str, list[tuple[int, int]]] = {}

    def ranges(path: str) -> list[tuple[int, int]]:
        if path not in range_cache:
            src = norm_sources.get(path)
            range_cache[path] = _body_ranges(path, src) if src is not None else []
        return range_cache[path]

    out: list[Candidate] = []
    for f in analyzer_findings:
        path = norm_path(f.get("path", ""))
        if not is_production_sol(path):
            continue
        in_site_file = path in cited
        if not in_site_file and path != obl:
            continue
        a_start = f.get("start_line")
        a_end = f.get("end_line")
        if a_end is None:
            a_end = a_start
        best: Candidate | None = None
        if in_site_file and a_start is not None:
            for sp, site in live:
                if sp != path or site.line_start is None:
                    continue
                s_end = site.line_end if site.line_end is not None else site.line_start
                if a_start <= s_end + STRICT_MARGIN and a_end >= site.line_start - STRICT_MARGIN:
                    best = Candidate(finding_id, f, site, "strict")
                    break
                if best is None:
                    for lo, hi in ranges(path):
                        if lo <= a_start <= hi and lo <= s_end and hi >= site.line_start:
                            best = Candidate(finding_id, f, site, "function")
                            break
        if best is None:
            first = next((s for sp, s in live if sp == path), None)
            best = Candidate(finding_id, f, first, "file")
        out.append(best)
    return out
