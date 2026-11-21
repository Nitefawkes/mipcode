"""Blind class labels for judged Sherlock findings, plus same-defect checks on candidate hits.

Readers A (Sonnet, low) and B (Haiku, low) label every finding with the identical
class prompt; a blind tie-breaker (Opus, medium) reads only when they disagree or one
fails. Same-defect checks (Sonnet, medium) run only for findings whose final class is
``in_class`` or ``adjacent``. Readers never see PoC, Discussion or analyzer output.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import CostLedger, Reader, RunHalted, make_client  # noqa: E402
from match_hits import candidates_for, is_production_sol, norm_path  # noqa: E402
from run_recall import clone_at  # noqa: E402
from sherlock_report import JudgedFinding, Site, reader_sections  # noqa: E402

PROMPTS = Path(__file__).resolve().parent / "prompts"
DEFAULT_CACHE = ROOT / ".mipdelta" / "sherlock_cache"
CAP_USD = 15.0

READER_A = ("claude-sonnet-5-5", "low")
READER_B = ("claude-haiku-5-5", "low")
TIEBREAKER = ("claude-opus-5-5", "medium")
SAME_DEFECT_READER = ("claude-sonnet-5-5", "medium")

CLASSES = ["in_class", "adjacent", "out_of_class"]
PATTERNS = ["param_not_forwarded", "param_unread", "sibling_missing_guard", "override_mismatch",
            "dead_state", "paired_op_mismatch", "branch_zero_value", "hardcoded_safety_param", "other"]
CAPABILITIES = ["sibling_guard_asymmetry", "safety_param_weighting", "cross_file_or_dependency_visibility",
                "fork_upstream_diff", "existing_detector_gap", "none_of_these"]
VERDICTS = ["same_defect", "related", "unrelated"]
CHECKED_CLASSES = {"in_class", "adjacent"}

SITE_CONTEXT = 20
ANALYZER_CONTEXT = 10
UNANCHORED_LINES = 60

CLASS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "class": {"type": "string", "enum": CLASSES},
        "pattern": {"type": ["string", "null"], "enum": PATTERNS + [None]},
        "would_catch_with": {"type": "array", "items": {"type": "string", "enum": CAPABILITIES}},
        "obligation_site": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["class", "pattern", "would_catch_with", "obligation_site", "rationale"],
    "additionalProperties": False,
}

SAME_DEFECT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "rationale": {"type": "string"},
    },
    "required": ["verdict", "rationale"],
    "additionalProperties": False,
}

CLASS_PROMPT = (PROMPTS / "class_reader.md").read_text(encoding="utf-8")
SAME_DEFECT_PROMPT = (PROMPTS / "same_defect.md").read_text(encoding="utf-8")


def prompt_sha(prompt: str, schema: dict) -> str:
    """Cache-busting hash: editing the prompt or the schema invalidates cached answers."""
    blob = prompt + json.dumps(schema, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def input_sha16(user: str) -> str:
    """Ruling R15(b): cache keys carry a hash of the exact user text, so a changed input never hits."""
    return hashlib.sha256(user.encode("utf-8")).hexdigest()[:16]


CLASS_SHA = prompt_sha(CLASS_PROMPT, CLASS_SCHEMA)
SAME_DEFECT_SHA = prompt_sha(SAME_DEFECT_PROMPT, SAME_DEFECT_SCHEMA)


# ------------------------------------------------------------------ rendering


def _numbered(lines: list[str], lo: int, hi: int) -> str:
    width = len(str(hi))
    return "\n".join(f"{n:>{width}}  {lines[n - 1]}" for n in range(lo, hi + 1))


def _render_code(path: str, start: int | None, end: int | None, sources: dict[str, str], context: int) -> str:
    """`path#Lstart-Lend` header plus numbered source with `context` lines either side."""
    path = norm_path(path)
    header = f"{path}#L{start}-L{end if end is not None else start}" if start is not None else path
    src = sources.get(path)
    if src is None:
        return f"### {header}\n[source unavailable]"
    lines = src.splitlines()
    if not lines:
        return f"### {header}\n[empty file]"
    if start is None:
        lo, hi = 1, min(len(lines), UNANCHORED_LINES)
    else:
        stop = end if end is not None else start
        lo, hi = max(1, start - context), min(len(lines), stop + context)
        if lo > hi:  # anchor past end of file
            return f"### {header}\n[lines out of range: file has {len(lines)} lines]"
    return f"### {header}\n```solidity\n{_numbered(lines, lo, hi)}\n```"


def _companion_sites(f: JudgedFinding) -> list[Site]:
    out: list[Site] = []
    seen: set[tuple] = set()
    for s in f.sites:
        key = (norm_path(s.path), s.line_start, s.line_end)
        if s.external or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _finding_text(f: JudgedFinding) -> list[str]:
    parts = [f"# Finding\n\n{f.title}"]
    for heading, body in reader_sections(f).items():
        parts.append(f"## {heading}\n\n{body}")
    return parts


def build_class_input(f: JudgedFinding, sources: dict[str, str]) -> str:
    """Title, the reader-visible sections, and each companion site with ±20 lines. No analyzer output."""
    sources = {norm_path(k): v for k, v in sources.items()}
    parts = _finding_text(f)
    parts.append("# Cited code")
    sites = _companion_sites(f)
    if not sites:
        parts.append("[no cited code in the audited repository]")
    for s in sites:
        parts.append(_render_code(s.path, s.line_start, s.line_end, sources, SITE_CONTEXT))
    return "\n\n".join(parts) + "\n"


def build_same_defect_input(f: JudgedFinding, cand: dict, sources: dict[str, str]) -> str:
    sources = {norm_path(k): v for k, v in sources.items()}
    a = cand["analyzer"]
    parts = _finding_text(f)
    evidence = a.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    a_path = norm_path(a.get("path", ""))
    a_start, a_end = a.get("start_line"), a.get("end_line")
    parts.append("# Analyzer report\n\n" + "\n".join([
        f"- detector: {a.get('detector', '')}",
        f"- symbol: {a.get('symbol', '')}",
        f"- location: {a_path} lines {a_start}-{a_end if a_end is not None else a_start}",
        "- evidence:",
        *[f"  - {e}" for e in evidence],
    ]))
    parts.append("# Code the finding cites")
    site = cand.get("site")
    sites = [Site(**site)] if site else _companion_sites(f)
    if not sites:
        parts.append("[no cited code in the audited repository]")
    for s in sites:
        parts.append(_render_code(s.path, s.line_start, s.line_end, sources, SITE_CONTEXT))
    parts.append("# Code at the analyzer location")
    parts.append(_render_code(a_path, a_start, a_end, sources, ANALYZER_CONTEXT))
    return "\n\n".join(parts) + "\n"


# ------------------------------------------------------------------ consensus


def _valid_label(data: dict | None) -> dict | None:
    """A reader's answer, or None when it is missing or outside the frozen enums."""
    if not isinstance(data, dict):
        return None
    wcw = data.get("would_catch_with")
    if (data.get("class") not in CLASSES or data.get("pattern") not in PATTERNS + [None]
            or not isinstance(wcw, list) or any(t not in CAPABILITIES for t in wcw)):
        return None
    obl = data.get("obligation_site")
    if obl is not None and not isinstance(obl, str):
        return None
    return data


def _majority(values: list, tiebreak_value):
    if not values:
        return tiebreak_value
    value, count = Counter(values).most_common(1)[0]
    if count * 2 > len(values):
        return value
    return tiebreak_value


def consensus(labels: list[dict | None]) -> dict:
    """Final label from 2 (A, B) or 3 (A, B, tie-breaker) labels; None = refused or invalid reader.

    Ruling R16: `class` is the majority of the valid labels (else the tie-breaker's, which may
    be None = unlabeled). `pattern`, `would_catch_with` and `obligation_site` come only from
    readers whose class equals the final class (in order A, B, C).
    """
    valid = [x for x in labels if x is not None]
    third = labels[2] if len(labels) >= 3 and labels[2] is not None else None
    final_class = _majority([x["class"] for x in valid], third["class"] if third else None)
    matching = [x for x in valid if final_class is not None and x["class"] == final_class]

    final_pattern = None
    wcw: list[str] = []
    capability_split = False
    if matching and final_class != "out_of_class":
        fallback = third if third is not None and third["class"] == final_class else matching[0]
        final_pattern = _majority([x.get("pattern") for x in matching], fallback.get("pattern"))
        if len(matching) == 1:
            wcw = sorted(set(matching[0].get("would_catch_with") or []))
        else:
            tags = Counter(t for x in matching for t in set(x.get("would_catch_with") or []))
            wcw = sorted(t for t, n in tags.items() if n >= 2)
            capability_split = not wcw and bool(tags)
    obligation = None if final_class == "out_of_class" else next(
        (x.get("obligation_site") for x in matching if x.get("obligation_site")), None)
    a, b = (labels + [None, None])[:2]
    needs_tiebreak = (
        a is None or b is None
        or a["class"] != b["class"]
        or a.get("pattern") != b.get("pattern")
        or set(a.get("would_catch_with") or []) != set(b.get("would_catch_with") or [])
    )
    return {
        "class": final_class,
        "pattern": final_pattern,
        "would_catch_with": wcw,
        "obligation_site": obligation,
        "needs_tiebreak": needs_tiebreak,
        "n_readers": len(labels),
        "n_valid": len(valid),
        "low_confidence": len(valid) == 1,
        "capability_split": capability_split,
    }


# ------------------------------------------------------------------ labeling


def _contest_of(finding_id: str) -> str:
    return finding_id.rsplit(":", 1)[0]


def _call_class(reader, who: str, model_effort: tuple[str, str], f: JudgedFinding, user: str):
    model, effort = model_effort
    key = f"{model}:{f.finding_id}:{CLASS_SHA}:{input_sha16(user)}"
    res = reader.call(model, effort, CLASS_PROMPT, user, CLASS_SCHEMA, key)
    record = {"reader": who, "model": model, "effort": effort, **dataclasses.asdict(res)}
    return record, _valid_label(res.data if res.status == "ok" else None)


def label_finding(f: JudgedFinding, sources: dict[str, str], reader) -> dict:
    user = build_class_input(f, sources)
    rec_a, lab_a = _call_class(reader, "A", READER_A, f, user)
    rec_b, lab_b = _call_class(reader, "B", READER_B, f, user)
    calls, labels = [rec_a, rec_b], [lab_a, lab_b]
    if consensus(labels)["needs_tiebreak"]:
        rec_c, lab_c = _call_class(reader, "C", TIEBREAKER, f, user)
        calls.append(rec_c)
        labels.append(lab_c)
    return {"finding_id": f.finding_id, "contest": _contest_of(f.finding_id), "severity": f.severity,
            "final": consensus(labels), "calls": calls}


def label_findings(findings, sources: dict[str, str], reader, out: list | None = None) -> list[dict]:
    """Label each finding; rows are appended to `out` as they finish so a budget stop keeps them."""
    rows = out if out is not None else []
    for f in findings:
        rows.append(label_finding(f, sources, reader))
    return rows


# ------------------------------------------------------------------ candidates & same-defect


_PERMALINK_PATH_RE = re.compile(r"github\.com/[^/\s]+/[^/\s]+/blob/[^/\s]+/([^#?\s>)`]+)")
_LINE_SUFFIX_RE = re.compile(r"(?:[:\s]+L?\d+(?:\s*-\s*L?\d+)?)$")


def resolve_obligation_path(site: str | None, repo_files) -> tuple[str | None, str | None]:
    """Map a reader's `obligation_site` to a repo-relative path among `repo_files`.

    Accepts `path#Lnn`, a GitHub permalink (any ref), a bare or partial file name
    (resolved by a unique path-suffix match), and `path:Lnn`. Returns (path, None) or
    (None, reason) when the site can't be resolved to exactly one file.
    """
    if not site or not site.strip():
        return None, None
    raw = site.strip().strip("`<>\"' ")
    m = _PERMALINK_PATH_RE.search(raw)
    path = unquote(m.group(1)) if m else raw.split("#", 1)[0]
    path = norm_path(_LINE_SUFFIX_RE.sub("", path.strip().strip("`<>\"' ")))
    if not path:
        return None, f"unresolved: no path in {site!r}"
    files = {norm_path(p) for p in repo_files}
    if path in files:
        return path, None
    hits = sorted(p for p in files if p.endswith("/" + path))
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"unresolved: no file in the clone matches {path!r}"
    return None, f"ambiguous: {path!r} matches {len(hits)} files"


def build_candidates(label_rows: list[dict], findings, analyzer_findings: list[dict],
                     sources: dict[str, str], repo_files=None, errors: list | None = None) -> list[dict]:
    """Candidate rows for findings whose final class is in_class or adjacent (Ruling R12).

    `repo_files` lists the clone's `.sol` paths used to resolve obligation sites; it defaults
    to every path the sources, sites and analyzer findings mention. Unresolved obligation
    sites are appended to `errors`.
    """
    by_id = {f.finding_id: f for f in findings}
    prod_sources = {norm_path(k): v for k, v in sources.items() if is_production_sol(k)}
    if repo_files is None:
        repo_files = (set(sources) | {s.path for f in findings for s in f.sites if not s.external}
                      | {a.get("path", "") for a in analyzer_findings})
    rows: list[dict] = []
    for lr in label_rows:
        final = lr["final"]
        f = by_id.get(lr["finding_id"])
        if f is None or final.get("class") not in CHECKED_CLASSES:
            continue
        obl_path, why = resolve_obligation_path(final.get("obligation_site"), repo_files)
        if why is not None and errors is not None:
            errors.append({"contest": _contest_of(f.finding_id), "finding_id": f.finding_id,
                           "stage": "obligation_site", "obligation_site": final.get("obligation_site"),
                           "error": why})
        cands = candidates_for(f.finding_id, list(f.sites), obl_path, analyzer_findings, prod_sources)
        for i, c in enumerate(cands):
            rows.append({"finding_id": c.finding_id, "index": i, "closeness": c.closeness,
                         "site": dataclasses.asdict(c.site) if c.site is not None else None,
                         "analyzer": c.analyzer})
    return rows


def check_candidates(cands: list[dict], findings, sources: dict[str, str], reader,
                     out: list | None = None) -> list[dict]:
    """One blind same-defect call per candidate row; rows are appended to `out` as they finish."""
    by_id = {f.finding_id: f for f in findings}
    rows = out if out is not None else []
    model, effort = SAME_DEFECT_READER
    for cand in cands:
        f = by_id[cand["finding_id"]]
        user = build_same_defect_input(f, cand, sources)
        key = f"{model}:{f.finding_id}:{cand['index']}:{SAME_DEFECT_SHA}:{input_sha16(user)}"
        res = reader.call(model, effort, SAME_DEFECT_PROMPT, user, SAME_DEFECT_SCHEMA, key)
        data = res.data if res.status == "ok" and isinstance(res.data, dict) else {}
        verdict = data.get("verdict") if data.get("verdict") in VERDICTS else None
        rows.append({"finding_id": f.finding_id, "index": cand["index"], "verdict": verdict,
                     "rationale": data.get("rationale") if verdict else None,
                     "call": {"model": model, "effort": effort, **dataclasses.asdict(res)}})
    return rows


# ------------------------------------------------------------------ io


def finding_from_dict(d: dict) -> JudgedFinding:
    sites = tuple(Site(**s) for s in d.get("sites", ()))
    return JudgedFinding(**{**d, "sites": sites})


def load_sources(clone: Path, paths) -> dict[str, str]:
    """Read the `.sol` files `paths` names from a clone; never follows a path out of it."""
    root = Path(clone).resolve()
    out: dict[str, str] = {}
    for raw in paths:
        p = norm_path(raw)
        if not p.endswith(".sol") or p in out:
            continue
        full = (root / p).resolve()
        if not full.is_relative_to(root) or not full.is_file():
            continue
        out[p] = full.read_text(encoding="utf-8", errors="replace")
    return out


def list_sol_files(clone: Path) -> list[str]:
    """Repo-relative POSIX paths of every `.sol` file in a clone (outside `.git`)."""
    root = Path(clone)
    out = []
    for p in root.rglob("*.sol"):
        rel = p.relative_to(root).as_posix()
        if rel.split("/")[0] != ".git" and p.is_file():
            out.append(rel)
    return sorted(out)


def _scrub(msg: str) -> str:
    return re.sub(r"(?:[A-Za-z]:[/\\][^\s'\"]*|file://\S*)", "<path>", msg)


def _write(out_dir: Path, name: str, which: str, partial: bool, rows: list, extra: dict | None = None) -> None:
    payload = {"set": which, "partial": partial, "rows": rows, **(extra or {})}
    (out_dir / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None, reader=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set", dest="which", required=True, choices=["dev", "measurement"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--recall-set", default=None)
    ap.add_argument("--hits", default=None)
    ap.add_argument("--cache", default=None, help="base cache dir (clones/, llm/, ledger.json)")
    ap.add_argument("--transport", choices=["cli", "sdk"], default="cli")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache) if args.cache else DEFAULT_CACHE
    rs_path = Path(args.recall_set) if args.recall_set else out_dir / "recall_set.json"
    hits_path = Path(args.hits) if args.hits else out_dir / f"hits_{args.which}.json"

    contests = [c for c in json.loads(rs_path.read_text(encoding="utf-8"))["contests"]
                if c["set"] == args.which]
    hits = {c["name"]: c for c in json.loads(hits_path.read_text(encoding="utf-8"))["contests"]}
    if reader is None:
        reader = Reader(make_client(args.transport), CostLedger(cache / "ledger.json", cap_usd=CAP_USD), cache / "llm")

    work = []  # (contest, findings, analyzer findings, sources, repo .sol files)
    errors: list[dict] = []
    for c in contests:
        findings = [finding_from_dict(d) for d in c["findings"]]
        entry = hits.get(c["name"], {})
        if "error" in entry or not entry:
            errors.append({"contest": c["name"], "stage": "hits",
                           "error": entry.get("error", "no hits entry")})
        analyzer = [] if "error" in entry else list(entry.get("findings", []))
        paths = [s.path for f in findings for s in f.sites if not s.external] + [a.get("path", "") for a in analyzer]
        try:
            clone = clone_at(c["companion_repo"], c["resolved_sha"], cache / "clones")
            sources = load_sources(clone, paths)
            repo_files = list_sol_files(clone)
        except Exception as exc:  # Ruling R15(a): skip the contest; never pay for code-less labels
            errors.append({"contest": c["name"], "stage": "clone", "n_findings_skipped": len(findings),
                           "error": _scrub(f"{type(exc).__name__}: {exc}")})
            continue
        work.append((c["name"], findings, analyzer, sources, repo_files))

    label_rows: list[dict] = []
    cand_rows: list[dict] = []
    sd_rows: list[dict] = []
    partial = False
    try:
        for _, findings, _, sources, _ in work:
            label_findings(findings, sources, reader, out=label_rows)
        for _, findings, analyzer, sources, repo_files in work:
            ids = {f.finding_id for f in findings}
            cands = build_candidates([r for r in label_rows if r["finding_id"] in ids], findings, analyzer,
                                     sources, repo_files=repo_files, errors=errors)
            cand_rows.extend(cands)
            check_candidates(cands, findings, sources, reader, out=sd_rows)
    except RunHalted as exc:
        partial = True
        errors.append({"contest": None, "stage": "halt", "error": f"{type(exc).__name__}: {exc}"})

    _write(out_dir, f"labels_{args.which}.json", args.which, partial, label_rows, {"errors": errors})
    _write(out_dir, f"candidates_{args.which}.json", args.which, partial, cand_rows)
    _write(out_dir, f"same_defect_{args.which}.json", args.which, partial, sd_rows)
    return 2 if partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
