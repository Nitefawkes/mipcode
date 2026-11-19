"""Parse Sherlock final-report markdown into judged findings and code sites."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

ISSUE_RE = re.compile(r"^# Issue (H|M)-(\d+): (.*)$", re.MULTILINE)
HEADING_RE = re.compile(r"^#{2,3} +(.+?)\s*$")
SOURCE_RE = re.compile(r"^Source:\s*(\S+)", re.MULTILINE)
URL_RE = re.compile(r"https://github\.com/[^\s)\]>`]+")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

READER_SECTIONS = {
    "new": ("Summary", "Root Cause", "Attack Path", "Impact", "Mitigation"),
    "old": ("Summary", "Vulnerability Detail", "Impact", "Recommendation"),
}


@dataclass(frozen=True)
class Site:
    repo: str  # owner/name
    ref: str  # SHA or branch name
    path: str  # POSIX path relative to the repo root
    line_start: int | None
    line_end: int | None
    external: bool


@dataclass(frozen=True)
class JudgedFinding:
    finding_id: str  # "<contest>:H-1"
    severity: str  # "H" or "M"
    number: int
    title: str
    source_url: str
    fmt: str  # "old" or "new"
    sections: dict[str, str]
    sites: tuple[Site, ...]


def parse_permalink(url: str, companion_repo: str) -> Site | None:
    """Return a Site for a GitHub blob link, or None for any other link."""
    url = url.strip().rstrip(".,;:")
    m = re.match(r"https://github\.com/([^/\s]+)/([^/\s]+)/blob/([^/\s]+)/([^#?\s]+)(?:\?[^#\s]*)?(?:#(\S*))?$", url)
    if not m:
        return None
    owner, name, ref, raw_path, frag = m.groups()
    path = unquote(raw_path).replace("\\", "/").lstrip("/")
    start = end = None
    if frag:
        lm = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", frag)
        if lm:
            start = int(lm.group(1))
            end = int(lm.group(2)) if lm.group(2) else start
    repo = f"{owner}/{name}"
    return Site(repo, unquote(ref), path, start, end, repo.lower() != companion_repo.lower())


def _split_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for line in body.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _sites(text: str, companion_repo: str) -> tuple[Site, ...]:
    out: list[Site] = []
    for url in URL_RE.findall(text):
        site = parse_permalink(url, companion_repo)
        if site is not None and site not in out:
            out.append(site)
    return tuple(out)


def parse_report(markdown: str, contest: str, companion_repo: str) -> list[JudgedFinding]:
    matches = list(ISSUE_RE.finditer(markdown))
    findings: list[JudgedFinding] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[m.end():end]
        sections = _split_sections(body)
        fmt = "new" if "Root Cause" in sections else "old"
        if fmt == "new":
            site_text = sections["Root Cause"]
        elif "Code Snippet" in sections:
            site_text = sections["Code Snippet"]
        else:
            site_text = sections.get("Vulnerability Detail", "")
        src = SOURCE_RE.search(body)
        severity, number = m.group(1), int(m.group(2))
        findings.append(
            JudgedFinding(
                finding_id=f"{contest}:{severity}-{number}",
                severity=severity,
                number=number,
                title=m.group(3).strip(),
                source_url=src.group(1) if src else "",
                fmt=fmt,
                sections=sections,
                sites=_sites(site_text, companion_repo),
            )
        )
    return findings


def reader_sections(f: JudgedFinding) -> dict[str, str]:
    """Only the sections readers may see; PoC, Discussion and the rest are withheld."""
    return {k: f.sections[k] for k in READER_SECTIONS[f.fmt] if k in f.sections}
