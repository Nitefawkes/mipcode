"""
mipdelta.cli — Command-line interface for MipCode v2 Post-Write & Post-Test Contract Delta Guard.

Supports three modes:
1. Single-file candidate check:
   mip-delta --root . --write-path pkg/mod.py --candidate-file /tmp/new_mod.py
2. Drop-in Git / Agent Post-Edit Hook (zero output when clean, <=250B advisory on contract drift):
   mip-delta --check-git [--base-ref HEAD] [--root .] [--runtime-dir .mipdelta/runtime]
3. Monitored Pytest Runner + Post-Test [rt-obs] Delta Check (Python 3.12 sys.monitoring):
   mip-delta --run-pytest [--pytest-args "-q"] [--base-ref HEAD] [--root .]
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

from .cache import FactCache
from .delta import analyze_delta, format_advisory, rank_findings
from .model import FileFacts, ObservedCallEdge, SourceFile
from .session import DeltaSession

_SKIP_DIRS = {"__pycache__", "venv", ".venv", "build", "dist", "node_modules"}


class AnalysisError(ValueError):
    """The CLI could not complete the requested comparison."""


class _CheckingCache(FactCache):
    def load_source(self, source: SourceFile, *, prior_facts: FileFacts | None = None) -> FileFacts:
        facts = super().load_source(source, prior_facts=prior_facts)
        if facts.parse_error is not None:
            raise AnalysisError(f"Cannot parse {source.path}: {facts.parse_error}")
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


def _report_error(error: Exception, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"status": "error", "error": str(error)}))
    else:
        print(f"mip-delta: {error}", file=sys.stderr)
    return 2


def _run_git_check(
    root: Path,
    base_ref: str,
    baseline: tuple[Path, str],
    runtime_dir: Path,
    max_bytes: int,
    as_json: bool,
    fail_on_findings: bool,
) -> int:
    t0 = time.perf_counter()
    top, commit = baseline
    untracked = _git_python_paths(root, top, "ls-files", "--others", "--exclude-standard", "-z")
    changed = _git_python_paths(root, top, "diff", "--name-only", "-z", "--no-renames", commit)
    changed_paths = sorted(changed | untracked)
    runtime_edges = _load_runtime_edges(runtime_dir)

    raw_findings = []
    if changed_paths:
        tracked = _git_python_paths(root, top, "ls-files", "--cached", "-z")
        after_files = _read_sources(top, tracked | untracked)
        before_files = _reconstruct_git_baseline(top, commit, after_files, changed_paths)
        for path in changed_paths:
            for files in (before_files, after_files):
                if path in files:
                    _check_source_syntax(path, files[path])
        cache = _CheckingCache()
        _check_stubs(before_files, cache)
        stubs = _check_stubs(after_files, cache)
        raw_findings = analyze_delta(
            {p: text for p, text in before_files.items() if not p.endswith(".pyi")},
            {p: text for p, text in after_files.items() if not p.endswith(".pyi")},
            cache=cache,
            stub_files=stubs,
            runtime_edges=runtime_edges,
        )
    ranked = tuple(rank_findings(raw_findings))
    advisory = format_advisory(ranked, max_bytes=max_bytes) if ranked else ""
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)

    if as_json:
        payload = {
            "base_ref": base_ref,
            "changed_paths": changed_paths,
            "runtime_edges_loaded": len(runtime_edges),
            "advisory_text": advisory,
            "advisory_utf8_bytes": len(advisory.encode("utf-8")),
            "wall_ms": wall_ms,
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
                for f in ranked
            ],
        }
        print(json.dumps(payload, indent=2))
    elif advisory:
        print(advisory)

    if fail_on_findings and ranked:
        return 1
    return 0


def _run_monitored_pytest(
    root: Path,
    pytest_args: str,
    base_ref: str,
    baseline: tuple[Path, str],
    runtime_dir: Path,
    max_bytes: int,
    as_json: bool,
    fail_on_findings: bool,
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
        )
    except (AnalysisError, OSError) as exc:
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
    parser.add_argument("--max-bytes", type=int, default=250, help="Maximum UTF-8 byte budget for post-write advisory")
    parser.add_argument("--json", action="store_true", help="Emit full JSON readout")
    args = parser.parse_args(argv)

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
            }
            if args.run_pytest:
                return _run_monitored_pytest(pytest_args=args.pytest_args, **kwargs)
            return _run_git_check(**kwargs)

        new_text = _decode_source(args.write_path, args.candidate_file.read_bytes())
        _check_source_syntax(args.write_path, new_text)
        files = _load_python_tree(root)
        cache = _CheckingCache()
        stubs = _check_stubs(files, cache)
        session = DeltaSession.from_workspace(
            {p: text for p, text in files.items() if not p.endswith(".pyi")},
            cache=cache,
            stub_texts=stubs,
            max_advisory_bytes=args.max_bytes,
        )
        session.add_runtime_edges(_load_runtime_edges(runtime_dir))
        res = session.apply_write(args.write_path, new_text)
    except (AnalysisError, OSError) as exc:
        return _report_error(exc, args.json)

    if args.json:
        payload = {
            "path": res.path,
            "before_sha256": res.before_sha256,
            "after_sha256": res.after_sha256,
            "content_changed": res.content_changed,
            "cross_file_triggered": res.cross_file_triggered,
            "parses_executed": res.parses_executed,
            "candidate_calls_inspected": res.candidate_calls_inspected,
            "advisory_text": res.advisory_text,
            "advisory_utf8_bytes": res.advisory_utf8_bytes,
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
        print(json.dumps(payload, indent=2))
    elif res.advisory_text:
        print(res.advisory_text)
    return 1 if (args.fail_on_findings and res.ranked_findings) else 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
