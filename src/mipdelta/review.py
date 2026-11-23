"""Zero-dependency Stage-2 LLM micro-triage for mip-delta findings.

Runs ONLY when deterministic Stage 1 emits candidate findings, using either:
1. Anthropic Messages API over stdlib urllib (`ANTHROPIC_API_KEY`), or
2. Local `claude -p` CLI fallback when `ANTHROPIC_API_KEY` is unset.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .cache import FactCache
from .delta import rank_findings
from .ledger import enclosing_function
from .model import Finding, FunctionFact

DEFAULT_API_MODEL = "claude-haiku-4-5"
DEFAULT_CLI_MODEL = "haiku"
MAX_DIFF_CHARS = 45_000
MAX_UNIQUE_SITES = 12
MAX_SNIPPETS = 8
MAX_CALLEE_SNIPPETS = 2
MAX_SNIPPET_LINES = 100
MAX_SNIPPET_CHARS = 3_200

REVIEW_SYSTEM_PROMPT = """You are a strict, concise Stage-2 triage reviewer for `mip-delta`, a Python cross-file contract checker.
Stage 1 flagged candidate cross-file or contract issues (unused newly added parameter, unforwarded optional parameter in a delegating wrapper, dead `self.<param>` attribute store, subclass/base override mismatch, removed callable still referenced, async/coroutine or return-arity mismatch at caller, or newly raised exception not caught by caller).
Given the commit/edit summary, canonical structural delta (deterministic AST ground truth when present), changed test filenames, production `.py` diff, Stage-1 advisories, and post-change code snippets at the flagged sites, classify into ONE verdict:
- `confirm_omission`: concrete evidence in the diff and snippets shows a real bug, missing caller/override update, unforwarded option across sibling wrappers, dead newly-added parameter/store, unhandled runtime break, or broken async call (unawaited, dropped, or execution-timing-shifted, e.g., eager-to-deferred coroutine/async execution).
- `review_worthy`: ambiguous contract seam with real risk (including uncertain execution-timing shifts, async coroutine deferral, or cross-file behavioral risk) that warrants a developer's quick look even if it might turn out intentional.
- `suppress_false_alarm`: Stage-1 alert is clearly benign, intentional, name-shadowed, dynamically handled (`*args`/`**kwargs`/`locals()`), compat-preserved, or otherwise false.

Treat the deterministic `CANONICAL STRUCTURAL DELTA` and actual code diff/snippets as ground truth over any commit subject or author summary claim.
Treat a newly async target whose calls changed from eager execution to deferred storage/teardown awaiting as at least `review_worthy` (or `confirm_omission` if batch/callback timing visibly regresses), never as benign merely because the coroutine is awaited later.
Also check carefully for common static-AST false positives before confirming:
- Local variable shadowing or unrelated same-named symbol in a different class/scope.
- Deliberate fixed-policy wrapper where a convenience helper is meant to pin an option rather than forward it.
- Compatible overrides where defaulted optional kwargs do not break callers, or override divergence is deliberate.
- Re-exported/aliased symbols still present under the same name, or parameters kept intentionally for interface compatibility."""

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["confirm_omission", "review_worthy", "suppress_false_alarm"],
        },
        "reason": {
            "type": "string",
            "description": "One or two concise sentences grounded in the diff and snippets.",
        },
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


class ReviewError(ValueError):
    """Stage-2 micro-triage could not complete."""


@dataclass(frozen=True)
class ReviewResult:
    """Outcome of Stage-2 triage for non-empty Stage-1 findings."""

    verdict: str
    reason: str
    backend: str
    model: str
    wall_ms: float
    cost_usd: float | None = None

    @property
    def suppressed(self) -> bool:
        return self.verdict == "suppress_false_alarm"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "verdict": self.verdict,
            "suppressed": self.suppressed,
            "reason": self.reason,
            "backend": self.backend,
            "model": self.model,
            "wall_ms": self.wall_ms,
        }
        if self.cost_usd is not None:
            payload["cost_usd"] = self.cost_usd
        return payload


def _is_test_or_support(path: str) -> bool:
    parts = path.replace("\\", "/").lower().split("/")
    leaf = parts[-1]
    if parts[0] in {"docs", "doc", "examples", "benchmarks", "scripts", "tasks"}:
        return True
    return (
        any(part in {"tests", "test", "testing"} for part in parts[:-1])
        or leaf.startswith("test_")
        or leaf.endswith("_test.py")
        or leaf in {"conftest.py", "tasks.py"}
    )


def _numbered_slice(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    start = max(1, start_line)
    end = min(len(lines), end_line)
    return "\n".join(f"{idx:4d}: {lines[idx - 1]}" for idx in range(start, end + 1))


def _extract_imports(text: str, limit: int = 25) -> str:
    picked = []
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if idx <= 120 and (stripped.startswith("import ") or stripped.startswith("from ")):
            picked.append(f"{idx:4d}: {line}")
            if len(picked) >= limit:
                break
    return "\n".join(picked)


def _extract_snippet(
    cache: FactCache,
    path: str,
    text: str,
    line: int,
    anchor: str,
    *,
    max_lines: int = MAX_SNIPPET_LINES,
    from_top: bool = False,
) -> dict[str, Any]:
    facts = cache.load(path, text)
    fn_key = anchor or enclosing_function(facts, line)
    fn = facts.functions.get(fn_key) if fn_key else None
    if fn is not None:
        span_start, span_end = fn.line, fn.end_line
        if span_end - span_start + 1 > max_lines:
            if from_top:
                span_start = fn.line
                span_end = min(fn.end_line, span_start + max_lines - 1)
            else:
                half = max_lines // 2
                span_start = max(fn.line, line - half)
                span_end = min(fn.end_line, span_start + max_lines - 1)
    else:
        span_start, span_end = max(1, line - 20), line + 20
    code = _numbered_slice(text, span_start, span_end)
    if len(code) > MAX_SNIPPET_CHARS:
        if from_top:
            code = code[:MAX_SNIPPET_CHARS] + "\n... [snippet truncated]"
        else:
            code = _trim_code_around_focus(code, span_start, line, MAX_SNIPPET_CHARS)
    return {
        "path": path,
        "anchor": fn_key or f"<module:{line}>",
        "focus_line": line,
        "span": [span_start, span_end],
        "imports": _extract_imports(text),
        "code": code,
    }


def _trim_code_around_focus(code: str, span_start: int, focus_line: int, max_chars: int) -> str:
    """Trim an oversized numbered snippet symmetrically around `focus_line` so the flagged line is never clipped."""
    lines = code.splitlines()
    if not lines:
        return code[:max_chars]
    focus_idx = max(0, min(len(lines) - 1, focus_line - span_start))
    prefix_note = "... [leading lines omitted]\n"
    suffix_note = "\n... [snippet truncated]"
    budget = max(80, max_chars - len(prefix_note) - len(suffix_note))

    lo = focus_idx
    hi = focus_idx
    focus_text = lines[focus_idx]
    if len(focus_text) > budget:
        focus_text = focus_text[:budget]
    used = len(focus_text)
    selected_focus = [focus_text]

    before: list[str] = []
    after: list[str] = []
    while lo > 0 or hi < len(lines) - 1:
        progressed = False
        if lo > 0:
            cand = lines[lo - 1]
            if used + len(cand) + 1 <= budget:
                before.append(cand)
                used += len(cand) + 1
                lo -= 1
                progressed = True
        if hi < len(lines) - 1:
            cand = lines[hi + 1]
            if used + len(cand) + 1 <= budget:
                after.append(cand)
                used += len(cand) + 1
                hi += 1
                progressed = True
        if not progressed:
            break

    body = "\n".join([*reversed(before), *selected_focus, *after])
    if lo > 0:
        body = prefix_note + body
    if hi < len(lines) - 1:
        body = body + suffix_note
    return body


_CALLEE_CONTRACT_RE = re.compile(r"\bcalls ([A-Za-z_][A-Za-z0-9_.]*) without forwarding\b")
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_DIFF_MINUS_HEADER_RE = re.compile(r"^--- (?:a/)?(.+)$")


def _find_function_in_sources(
    cache: FactCache, after_sources: Mapping[str, str], qualname: str
) -> tuple[str, FunctionFact] | None:
    for path in sorted(after_sources):
        facts = cache.load(path, after_sources[path])
        if facts.parse_error is not None:
            continue
        fn = facts.functions.get(qualname)
        if fn is not None:
            return path, fn
    return None


def _group_findings(
    findings: Iterable[Finding],
    anchor_for: Callable[[Finding], str],
) -> tuple[list[dict[str, Any]], int]:
    rows = rank_findings(findings)
    grouped: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for finding in rows:
        anchor = anchor_for(finding)
        key = (finding.detector, finding.symbol, finding.path, anchor)
        if key not in seen:
            item = {
                "detector": finding.detector,
                "symbol": finding.symbol,
                "path": finding.path,
                "line": finding.start_line,
                "anchor": anchor,
                "evidence": "; ".join(finding.evidence),
                "site_count_in_group": 1,
                "lines": [finding.start_line],
            }
            seen[key] = item
            grouped.append(item)
        else:
            seen[key]["site_count_in_group"] += 1
            if len(seen[key]["lines"]) < 6:
                seen[key]["lines"].append(finding.start_line)
    return grouped[:MAX_UNIQUE_SITES], len(rows)


def _split_unified_diff_sections(diff_text: str) -> list[tuple[str | None, str]]:
    """Split a unified diff into `(path, section_text)` pairs, preserving non-header text intact."""
    lines = diff_text.splitlines(keepends=True)
    if not lines:
        return []
    sections: list[tuple[str | None, list[str]]] = []
    current_path: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_lines:
            sections.append((current_path, current_lines[:]))
            current_lines.clear()

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        git_match = _DIFF_GIT_HEADER_RE.match(line.rstrip("\r\n"))
        if git_match:
            _flush()
            current_path = git_match.group(2).strip()
            current_lines.append(line)
            idx += 1
            continue
        if (
            line.startswith("--- ")
            and idx + 1 < len(lines)
            and lines[idx + 1].startswith("+++ ")
            and not any(existing.startswith("diff --git ") for existing in current_lines)
        ):
            _flush()
            plus_line = lines[idx + 1].rstrip("\r\n")[4:].strip()
            if plus_line.startswith("b/"):
                plus_line = plus_line[2:]
            minus_match = _DIFF_MINUS_HEADER_RE.match(line.rstrip("\r\n"))
            minus_path = minus_match.group(1).strip() if minus_match else ""
            current_path = plus_line if plus_line and plus_line != "/dev/null" else minus_path
            current_lines.append(line)
            current_lines.append(lines[idx + 1])
            idx += 2
            continue
        current_lines.append(line)
        idx += 1
    _flush()
    return [(path, "".join(buf)) for path, buf in sections]


def _cap_diff(diff_text: str, *, priority_paths: Iterable[str] = ()) -> str:
    if len(diff_text) <= MAX_DIFF_CHARS:
        return diff_text
    flagged = {p.replace("\\", "/") for p in priority_paths if p}
    if flagged:
        sections = _split_unified_diff_sections(diff_text)
        if len(sections) > 1:
            prioritized = [text for path, text in sections if path and path.replace("\\", "/") in flagged]
            others = [text for path, text in sections if not path or path.replace("\\", "/") not in flagged]
            if prioritized:
                diff_text = "".join([*prioritized, *others])
    if len(diff_text) <= MAX_DIFF_CHARS:
        return diff_text
    return diff_text[:MAX_DIFF_CHARS] + f"\n... [truncated after {MAX_DIFF_CHARS} of {len(diff_text)} chars] ...\n"


def _format_actual_delta_block(actual_delta: Mapping[str, Any]) -> list[str]:
    sigs = [str(s) for s in actual_delta.get("mutated_signatures") or ()]
    raises = [str(r) for r in actual_delta.get("new_raises") or ()]
    opened = [str(x) for x in actual_delta.get("opened_obligations") or ()]
    discharged = [str(x) for x in actual_delta.get("discharged_obligations") or ()]
    open_count = actual_delta.get("open_count")
    lines = ["", "=== CANONICAL STRUCTURAL DELTA (GROUND TRUTH) ==="]
    lines.append("Mutated signatures: " + (", ".join(sigs[:12]) + (f" (+{len(sigs) - 12})" if len(sigs) > 12 else "") if sigs else "(none)"))
    lines.append("New raises: " + (", ".join(raises[:10]) + (f" (+{len(raises) - 10})" if len(raises) > 10 else "") if raises else "(none)"))
    lines.append(
        f"Obligations: open_count={open_count if open_count is not None else 0}, "
        f"opened={len(opened)}, discharged={len(discharged)}"
    )
    return lines


def format_review_prompt(
    *,
    findings: Iterable[Finding],
    anchor_for: Callable[[Finding], str],
    after_sources: Mapping[str, str],
    diff_text: str,
    changed_test_files: Iterable[str] = (),
    subject: str = "(uncommitted workspace edit)",
    cache: FactCache | None = None,
    actual_delta: Mapping[str, Any] | None = None,
) -> str:
    """Build a bounded, self-contained prompt bundle for Stage-2 triage."""
    fact_cache = cache if cache is not None else FactCache()
    grouped, total_raw = _group_findings(findings, anchor_for)
    tests = sorted(changed_test_files)
    tests_text = ", ".join(tests) if tests else "(none)"
    parts = [
        f"Subject: {subject}",
        f"Changed test/support .py files: {tests_text}",
    ]
    if actual_delta is not None:
        parts.extend(_format_actual_delta_block(actual_delta))
    parts.extend(
        [
            "",
            f"=== STAGE-1 FINDINGS ({total_raw} raw sites, showing {len(grouped)} grouped) ===",
        ]
    )
    for idx, row in enumerate(grouped, 1):
        lines_str = ",".join(str(x) for x in row["lines"])
        parts.append(
            f"{idx}. [{row['detector']}] {row['path']}:{lines_str} "
            f"(anchor={row['anchor'] or '<module>'}, symbol={row['symbol']}, sites={row['site_count_in_group']}) "
            f"-- {row['evidence']}"
        )

    # Pre-resolve callee contracts so flagged-site snippets cannot starve them out of MAX_SNIPPETS.
    callee_contracts: list[tuple[str, str, FunctionFact]] = []
    seen_callees: set[tuple[str, str]] = set()
    for row in grouped:
        match = _CALLEE_CONTRACT_RE.search(row["evidence"])
        if match is None:
            continue
        target_qualname = match.group(1)
        located = _find_function_in_sources(fact_cache, after_sources, target_qualname)
        if located is None:
            continue
        target_path, target_fn = located
        target_key = (target_path, target_qualname)
        if target_key not in seen_callees:
            seen_callees.add(target_key)
            callee_contracts.append((target_path, target_qualname, target_fn))
            if len(callee_contracts) >= MAX_CALLEE_SNIPPETS:
                break

    site_budget = max(1, MAX_SNIPPETS - len(callee_contracts))
    parts.append("")
    parts.append("=== POST-CHANGE SNIPPETS AT FLAGGED SITES ===")
    seen_targets: set[tuple[str, str]] = set()
    snippet_count = 0
    for row in grouped:
        if snippet_count >= site_budget:
            break
        target = (row["path"], row["anchor"])
        if target in seen_targets:
            continue
        seen_targets.add(target)
        text = after_sources.get(row["path"])
        if text is None:
            continue
        snip = _extract_snippet(fact_cache, row["path"], text, row["line"], row["anchor"])
        parts.append(f"--- {snip['path']} :: {snip['anchor']} (focus line {snip['focus_line']}) ---")
        if snip["imports"]:
            parts.append("Top imports:\n" + snip["imports"])
        parts.append(f"Lines {snip['span'][0]}-{snip['span'][1]}:\n" + snip["code"])
        snippet_count += 1
    for target_path, target_qualname, target_fn in callee_contracts:
        if snippet_count >= MAX_SNIPPETS:
            break
        target_key = (target_path, target_qualname)
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)
        snip = _extract_snippet(
            fact_cache,
            target_path,
            after_sources[target_path],
            target_fn.line,
            target_qualname,
            max_lines=min(45, MAX_SNIPPET_LINES),
            from_top=True,
        )
        parts.append(f"--- {snip['path']} :: {target_qualname} [callee contract] (focus line {target_fn.line}) ---")
        if snip["imports"]:
            parts.append("Top imports:\n" + snip["imports"])
        parts.append(f"Lines {snip['span'][0]}-{snip['span'][1]}:\n" + snip["code"])
        snippet_count += 1
    if snippet_count == 0:
        parts.append("(no post-change site snippets available)")

    parts.append("")
    parts.append("=== PRODUCTION .PY DIFF ===")
    priority_paths = [row["path"] for row in grouped]
    parts.append(_cap_diff(diff_text.strip(), priority_paths=priority_paths) or "(empty diff)")
    return "\n".join(parts)


def unified_diff_for_pair(path: str, before_text: str | None, after_text: str | None) -> str:
    """Compute a unified diff for a single candidate file without invoking Git."""
    before_lines = (before_text or "").splitlines(keepends=True)
    after_lines = (after_text or "").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


def _validate_structured(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict):
        raise ReviewError(f"Expected JSON object from reviewer, got {type(data).__name__}")
    verdict = data.get("verdict")
    reason = data.get("reason")
    if verdict not in {"confirm_omission", "review_worthy", "suppress_false_alarm"}:
        raise ReviewError(f"Invalid review verdict: {verdict!r}")
    if not isinstance(reason, str):
        raise ReviewError("Review response missing string 'reason'")
    return verdict, reason.strip()


def _estimate_haiku_cost(usage: Mapping[str, Any]) -> float | None:
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if not isinstance(inp, int) or not isinstance(out, int):
        return None
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    # Anthropic Haiku list rates ($0.80/MTok in, $4/MTok out, $1/MTok cache write, $0.08/MTok read;
    # or Haiku 4.5 $1/$5/$1.25/$0.10 -- keep conservative estimate using $1/$5).
    return round((inp * 1.0 + cache_create * 1.25 + cache_read * 0.10 + out * 5.0) / 1_000_000, 6)


def _call_anthropic_api(prompt: str, *, api_key: str, model: str, timeout_s: float) -> ReviewResult:
    t0 = time.perf_counter()
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 350,
            "system": REVIEW_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": "submit_review",
                    "description": "Record Stage-2 micro-triage verdict and brief grounded reason.",
                    "input_schema": REVIEW_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": "submit_review"},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        os.environ.get("MIPDELTA_ANTHROPIC_URL", "https://api.anthropic.com/v1/messages"),
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ReviewError(f"Anthropic review API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReviewError(f"Anthropic review API request failed: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"Invalid JSON from Anthropic review API: {exc}") from exc

    tool_input = None
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "submit_review":
            tool_input = block.get("input")
            break
    if tool_input is None:
        raise ReviewError("Anthropic review API response did not include submit_review tool output")
    verdict, reason = _validate_structured(tool_input)
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    cost = _estimate_haiku_cost(payload.get("usage") or {})
    return ReviewResult(
        verdict=verdict,
        reason=reason,
        backend="anthropic_api",
        model=str(payload.get("model") or model),
        wall_ms=wall_ms,
        cost_usd=cost,
    )


def _call_claude_cli(prompt: str, *, claude_exe: str, model: str, timeout_s: float) -> ReviewResult:
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as td:
        try:
            proc = subprocess.run(
                [
                    claude_exe,
                    "-p",
                    "--model",
                    model,
                    "--tools",
                    "",
                    "--disable-slash-commands",
                    "--no-session-persistence",
                    "--setting-sources",
                    "",
                    "--system-prompt",
                    REVIEW_SYSTEM_PROMPT,
                    "--output-format",
                    "json",
                    "--json-schema",
                    json.dumps(REVIEW_SCHEMA),
                ],
                input=prompt,
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=td,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReviewError(f"claude CLI review failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:300]
        raise ReviewError(f"claude CLI exited with status {proc.returncode}: {detail}")
    try:
        payload = json.loads(proc.stdout)
        structured = payload.get("structured_output")
        if not isinstance(structured, dict):
            structured = json.loads(payload["result"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReviewError(f"Cannot parse claude CLI review output: {exc}") from exc

    verdict, reason = _validate_structured(structured)
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    cost_val = payload.get("total_cost_usd")
    cost = round(float(cost_val), 6) if isinstance(cost_val, (int, float)) else None
    used_model = next(iter((payload.get("modelUsage") or {}).keys()), model)
    return ReviewResult(
        verdict=verdict,
        reason=reason,
        backend="claude_cli",
        model=used_model,
        wall_ms=wall_ms,
        cost_usd=cost,
    )


def review_findings(
    *,
    findings: Iterable[Finding],
    anchor_for: Callable[[Finding], str],
    after_sources: Mapping[str, str],
    diff_text: str,
    changed_test_files: Iterable[str] = (),
    subject: str = "(uncommitted workspace edit)",
    model: str | None = None,
    timeout_s: float = 60.0,
    cache: FactCache | None = None,
    transport: Callable[[str, str], ReviewResult] | None = None,
    actual_delta: Mapping[str, Any] | None = None,
) -> ReviewResult:
    """Run Stage-2 triage on non-empty Stage-1 findings."""
    rows = list(findings)
    if not rows:
        raise ValueError("review_findings requires non-empty Stage-1 findings")
    prompt = format_review_prompt(
        findings=rows,
        anchor_for=anchor_for,
        after_sources=after_sources,
        diff_text=diff_text,
        changed_test_files=changed_test_files,
        subject=subject,
        cache=cache,
        actual_delta=actual_delta,
    )
    chosen_model = model or os.environ.get("MIPDELTA_REVIEW_MODEL")
    if transport is not None:
        return transport(prompt, chosen_model or DEFAULT_API_MODEL)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        return _call_anthropic_api(
            prompt,
            api_key=api_key,
            model=chosen_model or DEFAULT_API_MODEL,
            timeout_s=timeout_s,
        )
    claude_exe = shutil.which("claude")
    if claude_exe:
        return _call_claude_cli(
            prompt,
            claude_exe=claude_exe,
            model=chosen_model or DEFAULT_CLI_MODEL,
            timeout_s=timeout_s,
        )
    raise ReviewError(
        "Stage-2 --review requires ANTHROPIC_API_KEY in the environment or an authenticated `claude` CLI on PATH."
    )


def format_review_advisory(advisory: str, review: ReviewResult) -> str:
    """Append the compact Stage-2 verdict line below the Stage-1 advisory."""
    if not advisory:
        return f"[mip-delta:review {review.verdict}] {review.reason}"
    return f"{advisory}\n[mip-delta:review {review.verdict}] {review.reason}"
