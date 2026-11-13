"""
mipdelta.cli — Command-line interface for MipCode v2 Post-Write & Post-Test Contract Delta Guard.

Supports these modes:
1. Single-file candidate check:
   mip-delta --root . --write-path pkg/mod.py --candidate-file /tmp/new_mod.py
2. Drop-in Git / Agent Post-Edit Hook (zero output when clean, <=250B advisory on contract drift):
   mip-delta --check-git [--base-ref HEAD] [--root .] [--runtime-dir .mipdelta/runtime]
3. Monitored Pytest Runner + Post-Test [rt-obs] Delta Check (Python 3.12 sys.monitoring):
   mip-delta --run-pytest [--pytest-args "-q"] [--base-ref HEAD] [--root .]
4. Obligation ledger across repeated checks (report new sites once, list what is still open):
   mip-delta --check-git --ledger .mipdelta/ledger.json [--open]
   mip-delta --waive ID --reason TEXT --ledger .mipdelta/ledger.json
5. Agent hooks reading a Claude Code hook event on stdin:
   mip-delta --hook claude-post-edit | claude-stop
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .cache import FactCache
from .delta import OPT_IN_DETECTORS, analyze_changed_only, analyze_delta, format_advisory, rank_findings
from .ledger import LedgerUpdate, Obligation, ObligationLedger, enclosing_function, format_open_obligations
from .model import FileFacts, Finding, ObservedCallEdge, SourceFile
from .review import (
    ReviewError,
    ReviewResult,
    _is_test_or_support,
    format_review_advisory,
    review_findings,
    unified_diff_for_pair,
)
from .session import DeltaSession

_SKIP_DIRS = {"__pycache__", "venv", ".venv", "build", "dist", "node_modules"}


class AnalysisError(ValueError):
    """The CLI could not complete the requested comparison."""


class _CheckingCache(FactCache):
    """Fail on unparsable sources under review; record and skip other unparsable files.

    Repositories carry templates, legacy scripts, and deliberately invalid fixtures.
    Those files contribute no facts, and they must not hide a result for the change.
    """

    def __init__(self, required_paths: Iterable[str] = ()) -> None:
        super().__init__()
        self._required = {self.normalize_path(path) for path in required_paths}
        self.skipped: set[str] = set()

    def load_source(self, source: SourceFile, *, prior_facts: FileFacts | None = None) -> FileFacts:
        facts = super().load_source(source, prior_facts=prior_facts)
        if facts.parse_error is not None:
            path = self.normalize_path(source.path)
            if path in self._required:
                raise AnalysisError(f"Cannot parse {source.path}: {facts.parse_error}")
            self.skipped.add(path)
        return facts


def _is_python_source(relative: str) -> bool:
    return relative.endswith((".py", ".pyi")) and not any(
        part.startswith(".") or part in _SKIP_DIRS for part in relative.split("/")
    )


def _decode_source(path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnalysisError(f"Cannot decode {path} as UTF-8 at byte {exc.start}") from exc


def _check_source_syntax(path: str, text: str) -> None:
    # Compilation also catches invalid constructs accepted by ast.parse, such as
    # duplicate parameter names. It does not execute the source.
    try:
        compile(text, path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        raise AnalysisError(f"Cannot parse {path}: {exc.msg} at line {exc.lineno}") from exc


def _read_sources(root: Path, paths: Iterable[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for name in sorted(paths):
        path = root / name
        if path.is_file():
            files[name] = _decode_source(name, path.read_bytes())
    return files


def _load_python_tree(root: Path) -> dict[str, str]:
    paths = (path.relative_to(root).as_posix() for path in root.rglob("*.py*"))
    return _read_sources(root, (path for path in paths if _is_python_source(path)))


def _check_stubs(files: dict[str, str], cache: FactCache) -> dict[str, str]:
    stubs = {path: text for path, text in files.items() if path.endswith(".pyi")}
    for path, text in stubs.items():
        cache.load(path, text)
    return stubs


def _load_runtime_edges(runtime_dir: Path) -> list[ObservedCallEdge]:
    if not runtime_dir.exists():
        return []
    seen: set[tuple[str, str, int, str, str, str | None]] = set()
    edges: list[ObservedCallEdge] = []
    for sf in sorted(runtime_dir.glob("edges-*.json")):
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for e in data.get("edges", []):
            key = (
                e["caller_path"],
                e["caller_qualname"],
                int(e["instruction_offset"]),
                e["callee_qualname"],
                e["callee_name"],
                e.get("receiver_type"),
            )
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                ObservedCallEdge(
                    caller_path=e["caller_path"],
                    caller_qualname=e["caller_qualname"],
                    instruction_offset=int(e["instruction_offset"]),
                    callee_qualname=e["callee_qualname"],
                    callee_name=e["callee_name"],
                    receiver_type=e.get("receiver_type"),
                    pid=int(e.get("pid", 0)),
                    edge_label=e.get("edge_label", "observed_runtime"),
                )
            )
    return edges


def _git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "--literal-pathspecs", *args], cwd=root, capture_output=True, check=False
    )
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise AnalysisError(f"Git {args[0]} failed in {root}: {detail or proc.returncode}")
    return proc.stdout


def _git_baseline(root: Path, base_ref: str) -> tuple[Path, str]:
    top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").rstrip(b"\r\n"))).resolve()
    if not root.is_relative_to(top):
        raise AnalysisError(f"Git worktree {top} does not contain {root}")
    try:
        commit = _git(top, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}")
    except AnalysisError as exc:
        raise AnalysisError(f"Invalid Git baseline {base_ref!r}: {exc}") from exc
    return top, commit.decode("ascii").strip()


def _git_paths(output: bytes) -> set[str]:
    return {os.fsdecode(path) for path in output.split(b"\0") if path}


def _git_python_paths(root: Path, top: Path, *args: str) -> set[str]:
    scope = root.relative_to(top)
    pathspec = [scope.as_posix()] if root != top else []
    paths = _git_paths(_git(top, *args, "--", *pathspec))
    return {
        path for path in paths if _is_python_source(Path(path).relative_to(scope).as_posix())
    }


def _reconstruct_git_baseline(
    top: Path, commit: str, after_files: dict[str, str], changed_paths: list[str]
) -> dict[str, str]:
    before_files = dict(after_files)
    # Determine absence from the tree separately from a failure to read a known blob.
    baseline_paths = _git_paths(_git(top, "ls-tree", "-r", "--name-only", "-z", commit))
    for path in changed_paths:
        if path in baseline_paths:
            before_files[path] = _decode_source(path, _git(top, "show", f"{commit}:{path}"))
        else:
            before_files.pop(path, None)
    return before_files


def _note_skipped(skipped: list[str], as_json: bool) -> None:
    # JSON callers read the list from the payload. Text mode keeps stdout for the advisory.
    if skipped and not as_json:
        shown = ", ".join(skipped[:3]) + (f" (+{len(skipped) - 3})" if len(skipped) > 3 else "")
        print(f"mip-delta: skipped {len(skipped)} unparsable unchanged file(s): {shown}", file=sys.stderr)


def _report_error(error: Exception, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"status": "error", "error": str(error)}))
    else:
        print(f"mip-delta: {error}", file=sys.stderr)
    return 2


@dataclass
class _GitCheck:
    changed_paths: list[str]
    findings: list[Finding]
    skipped: list[str]
    runtime_edges: list[ObservedCallEdge]
    anchor_for: Callable[[Finding], str]
    # True when the whole workspace was read; False when the changed files were enough.
    workspace_parsed: bool = False
    after_files: Mapping[str, str] = field(default_factory=dict)
    cache: FactCache | None = None


def _collect_git_findings(
    root: Path,
    baseline: tuple[Path, str],
    runtime_dir: Path,
    *,
    local_first: bool = False,
    enable: Iterable[str] = (),
) -> _GitCheck:
    """Compare the working tree with the baseline commit.

    With local_first, only the changed files are read unless one of them changes a
    contract that other files can depend on. Unchanged unparsable files are then not
    visited, so they are not listed as skipped.
    """
    top, commit = baseline
    untracked = _git_python_paths(root, top, "ls-files", "--others", "--exclude-standard", "-z")
    changed = _git_python_paths(root, top, "diff", "--name-only", "-z", "--no-renames", commit)
    changed_paths = sorted(changed | untracked)
    runtime_edges = _load_runtime_edges(runtime_dir)
    if not changed_paths:
        return _GitCheck(changed_paths, [], [], runtime_edges, lambda finding: "")

    def checked(before: dict[str, str], after: dict[str, str]) -> None:
        for path in changed_paths:
            for files in (before, after):
                if path in files:
                    _check_source_syntax(path, files[path])

    def anchors(cache: FactCache, after: dict[str, str]) -> Callable[[Finding], str]:
        def anchor_for(finding: Finding) -> str:
            text = after.get(finding.path)
            facts = cache.load(finding.path, text) if text is not None else None
            return enclosing_function(facts, finding.start_line)

        return anchor_for

    if local_first:
        after_changed = _read_sources(top, changed_paths)
        before_changed = _reconstruct_git_baseline(top, commit, after_changed, changed_paths)
        checked(before_changed, after_changed)
        cache = _CheckingCache(changed_paths)
        findings, needs_workspace = analyze_changed_only(
            {p: text for p, text in before_changed.items() if not p.endswith(".pyi")},
            {p: text for p, text in after_changed.items() if not p.endswith(".pyi")},
            cache=cache,
            enable=enable,
        )
        if not needs_workspace:
            return _GitCheck(
                changed_paths,
                findings,
                [],
                runtime_edges,
                anchors(cache, after_changed),
                False,
                after_changed,
                cache,
            )

    tracked = _git_python_paths(root, top, "ls-files", "--cached", "-z")
    after_files = _read_sources(top, tracked | untracked)
    before_files = _reconstruct_git_baseline(top, commit, after_files, changed_paths)
    checked(before_files, after_files)
    cache = _CheckingCache(changed_paths)
    _check_stubs(before_files, cache)
    stubs = _check_stubs(after_files, cache)
    findings = analyze_delta(
        {p: text for p, text in before_files.items() if not p.endswith(".pyi")},
        {p: text for p, text in after_files.items() if not p.endswith(".pyi")},
        cache=cache,
        stub_files=stubs,
        runtime_edges=runtime_edges,
        enable=enable,
    )
    return _GitCheck(
        changed_paths,
        findings,
        sorted(cache.skipped),
        runtime_edges,
        anchors(cache, after_files),
        True,
        after_files,
        cache,
    )


def _finding_json(finding: Finding) -> dict[str, Any]:
    return {
        "detector": finding.detector,
        "symbol": finding.symbol,
        "path": finding.path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "resolution_source": finding.resolution_source,
        "evidence": list(finding.evidence),
    }


def _obligation_json(item: Obligation) -> dict[str, Any]:
    row = {"id": item.id, **_finding_json(item.finding)}
    if item.waived is not None:
        row["waived"] = item.waived
    return row


def _load_ledger_file(path: Path) -> tuple[ObligationLedger, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ObligationLedger(), None
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"Cannot read ledger {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnalysisError(f"Cannot read ledger {path}: expected a JSON object")
    base = data.get("base_commit")
    return ObligationLedger.from_dict(data.get("ledger") or {}), base if isinstance(base, str) else None


def _prepare_state_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if directory.name == ".mipdelta":
        ignore = directory / ".gitignore"
        if not ignore.exists():
            # Keep session state out of the user's Git status.
            ignore.write_text("*\n", encoding="utf-8")


def _save_ledger_file(path: Path, ledger: ObligationLedger, base_commit: str | None) -> None:
    _prepare_state_dir(path.parent)
    payload = {"schema": "mipdelta.ledger-file.v1", "base_commit": base_commit, "ledger": ledger.to_dict()}
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(scratch, path)


def _events_path(ledger_path: Path) -> Path:
    """The append-only event log kept next to a ledger: X.json -> X.events.jsonl."""
    return ledger_path.with_name(ledger_path.stem + ".events.jsonl")


class _EventLog:
    """Buffer the events of one invocation and append them to the log in one write.

    Events carry names, paths and line numbers only, never source text or evidence.
    A failure to write is swallowed: the log must not change what a check returns.
    """

    def __init__(self, ledger_path: Path, *, session: str | None, base_commit: str | None, **common: Any) -> None:
        self.path = _events_path(ledger_path)
        self._common = {"invocation": uuid.uuid4().hex[:12], "session": session, "base_commit": base_commit, **common}
        self._rows: list[dict[str, Any]] = []

    def add(self, event: str, **fields: Any) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self._rows.append({"event": event, "ts": stamp, **self._common, **fields})

    def obligations(self, event: str, items: Iterable[Obligation]) -> None:
        for item in items:
            finding = item.finding
            self.add(
                event,
                id=item.id,
                detector=finding.detector,
                symbol=finding.symbol,
                path=finding.path,
                line=finding.start_line,
                provenance=finding.resolution_source,
            )

    def run(self, source: str, wall_ms: float, update: LedgerUpdate, changed_paths: int, workspace_parsed: bool) -> None:
        self.add(
            "run",
            source=source,
            wall_ms=wall_ms,
            opened=len(update.opened),
            discharged=len(update.discharged),
            open=len(update.open),
            changed_paths=changed_paths,
            workspace_parsed=workspace_parsed,
        )

    def flush(self) -> None:
        rows, self._rows = self._rows, []
        if not rows:
            return
        try:
            text = "".join(json.dumps(row) + "\n" for row in rows)
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except Exception as exc:  # noqa: BLE001 - logging must never change the outcome
            _log_failure(exc)


def _log_failure(exc: BaseException) -> None:
    if os.environ.get("MIPDELTA_HOOK_DEBUG"):
        print(f"mip-delta event log: {exc}", file=sys.stderr)


def _relative_edit(edited: str, root: Path, top: Path) -> str | None:
    """The edited file relative to the top of the repository, or None outside it."""
    if not edited:
        return None
    path = Path(edited)
    try:
        return (path if path.is_absolute() else root / path).resolve().relative_to(top).as_posix()
    except (ValueError, OSError):
        return None


def _ledger_session(ledger_path: Path) -> str | None:
    match = re.fullmatch(r"ledger-(.+)\.json", ledger_path.name)
    return match.group(1) if match and ledger_path.parent.name == ".mipdelta" else None


def _git_prod_diff(
    top: Path, commit: str, prod_paths: list[str], after_files: Mapping[str, str]
) -> str:
    if not prod_paths:
        return ""
    tracked_set = _git_paths(_git(top, "ls-tree", "-r", "--name-only", "-z", commit))
    tracked_prod = [p for p in prod_paths if p in tracked_set]
    untracked_prod = [p for p in prod_paths if p not in tracked_set]
    chunks: list[str] = []
    if tracked_prod:
        raw = _git(top, "diff", "--no-ext-diff", "-U3", commit, "--", *tracked_prod).decode(
            "utf-8", errors="replace"
        )
        if raw.strip():
            chunks.append(raw.strip())
    for path in untracked_prod:
        diff = unified_diff_for_pair(path, None, after_files.get(path))
        if diff.strip():
            chunks.append(diff.strip())
    return "\n\n".join(chunks)


def _apply_review_outcome(
    advisory: str,
    failing: bool,
    review_result: ReviewResult | None,
    *,
    show_open: bool = False,
) -> tuple[str, bool]:
    if review_result is None:
        return advisory, failing
    if review_result.suppressed:
        return (advisory if show_open else ""), False
    return format_review_advisory(advisory, review_result), failing


def _run_git_check(
    root: Path,
    base_ref: str,
    baseline: tuple[Path, str],
    runtime_dir: Path,
    max_bytes: int | None,
    as_json: bool,
    fail_on_findings: bool,
    ledger_path: Path | None = None,
    show_open: bool = False,
    enable: Iterable[str] = (),
    source: str = "check-git",
    review: bool = False,
    review_model: str | None = None,
) -> int:
    t0 = time.perf_counter()
    check = _collect_git_findings(root, baseline, runtime_dir, enable=enable)
    ranked = tuple(rank_findings(check.findings))
    advisory_budget = 250 if max_bytes is None else max_bytes
    reported = ranked
    ledger_rows: dict[str, list[dict[str, Any]]] = {}
    open_text = ""
    failing = bool(ranked)
    update: LedgerUpdate | None = None
    if ledger_path is not None:
        ledger, _ = _load_ledger_file(ledger_path)
        update = ledger.update(check.findings, check.anchor_for)
        _save_ledger_file(ledger_path, ledger, baseline[1])
        reported = tuple(rank_findings(item.finding for item in update.opened))
        ledger_rows = {
            "opened": [_obligation_json(item) for item in update.opened],
            "discharged": [_obligation_json(item) for item in update.discharged],
            "open": [_obligation_json(item) for item in update.open],
            "waived": [_obligation_json(item) for item in ledger.waived()],
        }
        open_text = format_open_obligations(
            update.open, max_bytes=600 if max_bytes is None else max_bytes, show_ids=True
        )
        failing = bool(update.open)
    advisory = format_advisory(reported, max_bytes=advisory_budget) if reported else ""
    if show_open:
        advisory = open_text
    review_result: ReviewResult | None = None
    if review and reported:
        prod_paths = [p for p in check.changed_paths if not _is_test_or_support(p)]
        test_paths = [p for p in check.changed_paths if _is_test_or_support(p)]
        after_sources = dict(check.after_files)
        for item in reported:
            if item.path not in after_sources and (baseline[0] / item.path).is_file():
                after_sources[item.path] = _decode_source(item.path, (baseline[0] / item.path).read_bytes())
        diff_text = _git_prod_diff(baseline[0], baseline[1], prod_paths, after_sources)
        review_result = review_findings(
            findings=reported,
            anchor_for=check.anchor_for,
            after_sources=after_sources,
            diff_text=diff_text,
            changed_test_files=test_paths,
            subject=f"working tree vs {base_ref}",
            model=review_model,
            cache=check.cache,
        )
        advisory, failing = _apply_review_outcome(advisory, failing, review_result, show_open=show_open)
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    if as_json:
        payload = {
            "base_ref": base_ref,
            "changed_paths": check.changed_paths,
            "skipped_unparsed_paths": check.skipped,
            "runtime_edges_loaded": len(check.runtime_edges),
            "advisory_text": advisory,
            "advisory_utf8_bytes": len(advisory.encode("utf-8")),
            "wall_ms": wall_ms,
            "findings": [_finding_json(f) for f in ranked],
            **ledger_rows,
        }
        if review_result is not None:
            payload["review"] = review_result.to_dict()
        print(json.dumps(payload, indent=2))
    elif advisory:
        print(advisory)
    _note_skipped(check.skipped, as_json)
    if ledger_path is not None and update is not None:
        try:
            log = _EventLog(ledger_path, session=_ledger_session(ledger_path), base_commit=baseline[1])
            log.obligations("opened", update.opened)
            log.obligations("discharged", update.discharged)
            log.run(source, wall_ms, update, len(check.changed_paths), check.workspace_parsed)
            log.flush()
        except Exception as exc:  # noqa: BLE001 - logging must never change the exit status
            _log_failure(exc)

    if fail_on_findings and failing:
        return 1
    return 0


def _commit_exists(top: Path, commit: str) -> bool:
    try:
        _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{commit}}")
    except AnalysisError:
        return False
    return True


def _run_hook(kind: str, raw_event: str, enable: Iterable[str] = ()) -> int:
    """Adapt a Claude Code hook event. A hook never fails the agent's tool call or turn.

    claude-post-edit adds newly opened obligations as context after an Edit or Write.
    claude-stop blocks completion once while obligations it has not yet listed are open.
    State is kept per session in .mipdelta/ at the top of the repository, with the
    baseline pinned to the commit that was current when the session's first obligation opened.
    """
    try:
        event = json.loads(raw_event) if raw_event.strip() else {}
    except json.JSONDecodeError:
        return 0
    if not isinstance(event, dict):
        return 0
    if kind == "claude-stop" and event.get("stop_hook_active"):
        return 0
    edited = ""
    if kind == "claude-post-edit":
        tool_input = event.get("tool_input")
        edited = str(tool_input.get("file_path") or "") if isinstance(tool_input, dict) else ""
        if edited and not edited.endswith((".py", ".pyi")):
            return 0
    try:
        t0 = time.perf_counter()
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".").resolve()
        top, head = _git_baseline(root, "HEAD")
        session = re.sub(r"[^A-Za-z0-9_-]", "_", str(event.get("session_id") or "default"))[:80]
        ledger_path = top / ".mipdelta" / f"ledger-{session}.json"
        ledger, pinned = _load_ledger_file(ledger_path)
        commit = pinned if pinned and _commit_exists(top, pinned) else head
        before = ledger.to_dict()
        check = _collect_git_findings(
            root, (top, commit), root / ".mipdelta" / "runtime", local_first=True, enable=enable
        )
        update = ledger.update(check.findings, check.anchor_for)
        output: dict[str, Any] | None = None
        pending: tuple[Obligation, ...] = ()
        if kind == "claude-post-edit":
            if update.opened:
                text = format_advisory(rank_findings(item.finding for item in update.opened), max_bytes=250)
                output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}
        else:
            pending = ledger.unreported_at_stop()
            if pending:
                ledger.mark_reported_at_stop(pending)
                listed = format_open_obligations(update.open, max_bytes=600, show_ids=True)
                relative = ledger_path.relative_to(top).as_posix()
                output = {
                    "decision": "block",
                    "reason": (
                        f"{listed} Review these sites before finishing. If one is intentional, say so, "
                        f"or record it with: mip-delta --waive ID --ledger {relative}"
                    ),
                }
        if ledger.to_dict() != before:
            _save_ledger_file(ledger_path, ledger, commit)
        if output is not None:
            print(json.dumps(output))
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        # A quiet invocation in a session with no ledger leaves no state behind,
        # unless MIPDELTA_LOG_ALL_RUNS asks for every run to be recorded.
        if ledger_path.exists() or os.environ.get("MIPDELTA_LOG_ALL_RUNS"):
            try:
                _prepare_state_dir(ledger_path.parent)
                extra = {"edited_path": _relative_edit(edited, root, top)} if kind == "claude-post-edit" else {}
                raw_session = event.get("session_id")
                log = _EventLog(
                    ledger_path, session=None if raw_session is None else str(raw_session), base_commit=commit, **extra
                )
                log.obligations("opened", update.opened)
                log.obligations("discharged", update.discharged)
                if pending:
                    log.add("stop_blocked", ids=[item.id for item in update.open], new_ids=[item.id for item in pending])
                log.run(kind, wall_ms, update, len(check.changed_paths), check.workspace_parsed)
                log.flush()
            except Exception as exc:  # noqa: BLE001 - a hook never fails the tool call or the turn
                _log_failure(exc)
    except (AnalysisError, OSError) as exc:
        # An edit in progress may not parse, and the directory may not be a repository.
        if os.environ.get("MIPDELTA_HOOK_DEBUG"):
            print(f"mip-delta hook: {exc}", file=sys.stderr)
    return 0


def _run_monitored_pytest(
    root: Path,
    pytest_args: str,
    base_ref: str,
    baseline: tuple[Path, str],
    runtime_dir: Path,
    max_bytes: int | None,
    as_json: bool,
    fail_on_findings: bool,
    ledger_path: Path | None = None,
    show_open: bool = False,
    enable: Iterable[str] = (),
    review: bool = False,
    review_model: str | None = None,
) -> int:
    shard_dir = runtime_dir.resolve()
    shard_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_dir = shard_dir / "_bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "sitecustomize.py").write_text(
        "import os\n"
        "if os.environ.get('MIPDELTA_MONITOR_CONFIG'):\n"
        "    from mipdelta.monitoring import install_process_collector_from_env\n"
        "    install_process_collector_from_env()\n",
        encoding="utf-8",
    )
    src_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MIPDELTA_MONITOR_CONFIG"] = json.dumps(
        {
            "include_roots": [str(baseline[0])],
            "max_events_per_site": 8,
            "shard_dir": str(shard_dir),
        }
    )
    py_paths = [str(bootstrap_dir), str(src_root), str(root)]
    if env.get("PYTHONPATH"):
        py_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_paths)

    extra_args = shlex.split(pytest_args) if pytest_args else []
    cmd = [sys.executable, "-m", "pytest", *extra_args]
    proc = subprocess.run(cmd, cwd=str(root), env=env, check=False)

    try:
        check_rc = _run_git_check(
            root=root,
            base_ref=base_ref,
            baseline=baseline,
            runtime_dir=shard_dir,
            max_bytes=max_bytes,
            as_json=as_json,
            fail_on_findings=fail_on_findings,
            ledger_path=ledger_path,
            show_open=show_open,
            enable=enable,
            source="run-pytest",
            review=review,
            review_model=review_model,
        )
    except (AnalysisError, ReviewError, OSError) as exc:
        check_rc = _report_error(exc, as_json)
    return proc.returncode if proc.returncode != 0 else check_rc


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mip-delta",
        description="MipCode v0.6 research preview for Python contract changes.",
    )
    parser.add_argument("--root", type=Path, default=Path("."), help="Workspace root directory")
    parser.add_argument("--write-path", type=str, default=None, help="Relative path of the modified Python file")
    parser.add_argument("--candidate-file", type=Path, default=None, help="Path to file containing proposed new UTF-8 source")
    parser.add_argument(
        "--check-git",
        action="store_true",
        help="Compare git working tree against --base-ref (default HEAD) + any runtime edges in --runtime-dir",
    )
    parser.add_argument(
        "--run-pytest",
        action="store_true",
        help="Run pytest under Python 3.12 sys.monitoring and evaluate post-test contract deltas against --base-ref",
    )
    parser.add_argument("--pytest-args", type=str, default="", help="Arguments forwarded to pytest when --run-pytest is used")
    parser.add_argument("--base-ref", type=str, default="HEAD", help="Git ref to compare working tree against (default: HEAD)")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="Directory containing sys.monitoring edges-*.json shards (default: <root>/.mipdelta/runtime)",
    )
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit with status 1 if any contract delta findings are emitted (for strict pre-commit hooks)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="Maximum UTF-8 bytes of advisory text (default: 250, or 600 for the --open list)",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="JSON file that keeps open obligations between checks; text output then names only new ones",
    )
    parser.add_argument("--open", action="store_true", help="With --ledger, list every obligation still open")
    parser.add_argument("--waive", metavar="ID", default=None, help="Mark an open obligation in --ledger as intentional")
    parser.add_argument("--reason", default="", help="Note stored with --waive")
    parser.add_argument(
        "--hook",
        choices=("claude-post-edit", "claude-stop"),
        default=None,
        help="Read a Claude Code hook event on stdin and answer in that hook's JSON format",
    )
    parser.add_argument(
        "--enable",
        action="append",
        choices=sorted(OPT_IN_DETECTORS),
        default=[],
        metavar="DETECTOR",
        help="Also run an opt-in detector (repeatable): " + ", ".join(sorted(OPT_IN_DETECTORS)),
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Run Stage-2 LLM micro-triage (ANTHROPIC_API_KEY or claude CLI) only when Stage 1 emits findings",
    )
    parser.add_argument(
        "--review-model",
        type=str,
        default=None,
        help="Model override for --review (default: claude-haiku-4-5 for API, haiku for claude CLI)",
    )
    parser.add_argument("--json", action="store_true", help="Emit full JSON readout")
    args = parser.parse_args(argv)

    if args.hook:
        return _run_hook(args.hook, sys.stdin.buffer.read().decode("utf-8", errors="replace"), args.enable)
    if args.waive is not None:
        if args.ledger is None:
            parser.error("--waive needs --ledger.")
        try:
            ledger, base_commit = _load_ledger_file(args.ledger)
            if not ledger.waive(args.waive, args.reason):
                raise AnalysisError(f"No open obligation {args.waive} in {args.ledger}")
            _save_ledger_file(args.ledger, ledger, base_commit)
        except (AnalysisError, OSError) as exc:
            return _report_error(exc, args.json)
        try:
            log = _EventLog(args.ledger, session=_ledger_session(args.ledger.resolve()), base_commit=base_commit)
            log.add("waived", id=args.waive, reason=args.reason or "waived")
            log.flush()
        except Exception as exc:  # noqa: BLE001 - logging must never change the exit status
            _log_failure(exc)
        return 0
    if args.open and args.ledger is None:
        parser.error("--open needs --ledger.")
    if args.ledger is not None and not (args.check_git or args.run_pytest):
        parser.error("--ledger applies to --check-git and --run-pytest.")
    if not (args.check_git or args.run_pytest or (args.write_path and args.candidate_file is not None)):
        parser.error("Either pass --check-git, --run-pytest, or both --write-path and --candidate-file.")

    try:
        root = args.root.resolve()
        if not root.is_dir():
            raise AnalysisError(f"Workspace root is not a directory: {root}")
        runtime_dir = args.runtime_dir.resolve() if args.runtime_dir else (root / ".mipdelta" / "runtime")

        if args.run_pytest or args.check_git:
            baseline = _git_baseline(root, args.base_ref)
            kwargs = {
                "root": root,
                "base_ref": args.base_ref,
                "baseline": baseline,
                "runtime_dir": runtime_dir,
                "max_bytes": args.max_bytes,
                "as_json": args.json,
                "fail_on_findings": args.fail_on_findings,
                "ledger_path": args.ledger.resolve() if args.ledger else None,
                "show_open": args.open,
                "enable": args.enable,
                "review": args.review,
                "review_model": args.review_model,
            }
            if args.run_pytest:
                return _run_monitored_pytest(pytest_args=args.pytest_args, **kwargs)
            return _run_git_check(**kwargs)

        new_text = _decode_source(args.write_path, args.candidate_file.read_bytes())
        _check_source_syntax(args.write_path, new_text)
        files = _load_python_tree(root)
        cache = _CheckingCache([args.write_path])
        stubs = _check_stubs(files, cache)
        session = DeltaSession.from_workspace(
            {p: text for p, text in files.items() if not p.endswith(".pyi")},
            cache=cache,
            stub_texts=stubs,
            max_advisory_bytes=250 if args.max_bytes is None else args.max_bytes,
            enable=args.enable,
        )
        session.add_runtime_edges(_load_runtime_edges(runtime_dir))
        res = session.apply_write(args.write_path, new_text)
        review_result: ReviewResult | None = None
        advisory_text = res.advisory_text
        failing = bool(res.ranked_findings)
        if args.review and res.ranked_findings:
            after_sources = {**files, args.write_path: new_text}

            def _candidate_anchor(finding: Finding) -> str:
                text = after_sources.get(finding.path)
                facts = cache.load(finding.path, text) if text is not None else None
                return enclosing_function(facts, finding.start_line)

            review_result = review_findings(
                findings=res.ranked_findings,
                anchor_for=_candidate_anchor,
                after_sources=after_sources,
                diff_text=unified_diff_for_pair(args.write_path, files.get(args.write_path), new_text),
                subject=f"candidate edit {args.write_path}",
                model=args.review_model,
                cache=cache,
            )
            advisory_text, failing = _apply_review_outcome(advisory_text, failing, review_result)
    except (AnalysisError, ReviewError, OSError) as exc:
        return _report_error(exc, args.json)

    if args.json:
        payload = {
            "path": res.path,
            "before_sha256": res.before_sha256,
            "after_sha256": res.after_sha256,
            "content_changed": res.content_changed,
            "skipped_unparsed_paths": sorted(cache.skipped),
            "cross_file_triggered": res.cross_file_triggered,
            "parses_executed": res.parses_executed,
            "candidate_calls_inspected": res.candidate_calls_inspected,
            "advisory_text": advisory_text,
            "advisory_utf8_bytes": len(advisory_text.encode("utf-8")),
            "wall_ms": round(res.wall_seconds * 1000.0, 3),
            "findings": [
                {
                    "detector": f.detector,
                    "symbol": f.symbol,
                    "path": f.path,
                    "start_line": f.start_line,
                    "end_line": f.end_line,
                    "resolution_source": f.resolution_source,
                    "evidence": list(f.evidence),
                }
                for f in res.ranked_findings
            ],
        }
        if review_result is not None:
            payload["review"] = review_result.to_dict()
        print(json.dumps(payload, indent=2))
    elif advisory_text:
        print(advisory_text)
    _note_skipped(sorted(cache.skipped), args.json)
    return 1 if (args.fail_on_findings and failing) else 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
