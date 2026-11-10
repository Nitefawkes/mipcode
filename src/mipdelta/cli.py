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

from .delta import analyze_delta, format_advisory, rank_findings
from .model import ObservedCallEdge
from .session import DeltaSession

_SKIP_DIRS = {"__pycache__", "venv", ".venv", "build", "dist", "node_modules"}


def _load_python_tree(root: Path, *, include_stubs: bool = True) -> dict[str, str]:
    files: dict[str, str] = {}
    patterns = ("*.py", "*.pyi") if include_stubs else ("*.py",)
    for pat in patterns:
        for path in sorted(root.rglob(pat)):
            rel = path.relative_to(root).as_posix()
            parts = rel.split("/")
            if any(p.startswith(".") or p in _SKIP_DIRS for p in parts):
                continue
            try:
                files[rel] = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
    return files


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


def _reconstruct_git_baseline(root: Path, after_files: dict[str, str], base_ref: str) -> tuple[dict[str, str], list[str]]:
    """
    Reconstruct the baseline file mapping at `base_ref` by querying git only for paths
    that differ between `base_ref` and the current working tree.
    """
    before_files = dict(after_files)
    changed_paths: set[str] = set()

    diff_proc = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "--", "*.py", "*.pyi"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if diff_proc.returncode == 0:
        for line in diff_proc.stdout.splitlines():
            rel = line.strip().replace("\\", "/")
            if rel:
                changed_paths.add(rel)

    untracked_proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py", "*.pyi"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if untracked_proc.returncode == 0:
        for line in untracked_proc.stdout.splitlines():
            rel = line.strip().replace("\\", "/")
            if rel:
                changed_paths.add(rel)

    for rel in sorted(changed_paths):
        show_proc = subprocess.run(
            ["git", "show", f"{base_ref}:{rel}"],
            cwd=str(root),
            capture_output=True,
            check=False,
        )
        if show_proc.returncode == 0:
            try:
                before_files[rel] = show_proc.stdout.decode("utf-8")
            except UnicodeDecodeError:
                before_files.pop(rel, None)
        else:
            before_files.pop(rel, None)

    return before_files, sorted(changed_paths)


def _run_git_check(
    root: Path,
    base_ref: str,
    runtime_dir: Path,
    max_bytes: int,
    as_json: bool,
    fail_on_findings: bool,
) -> int:
    t0 = time.perf_counter()
    after_files = _load_python_tree(root, include_stubs=True)
    before_files, changed_paths = _reconstruct_git_baseline(root, after_files, base_ref)
    runtime_edges = _load_runtime_edges(runtime_dir)

    if not changed_paths:
        if as_json:
            print(
                json.dumps(
                    {
                        "base_ref": base_ref,
                        "changed_paths": [],
                        "runtime_edges_loaded": len(runtime_edges),
                        "advisory_text": "",
                        "advisory_utf8_bytes": 0,
                        "wall_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                        "findings": [],
                    },
                    indent=2,
                )
            )
        return 0

    raw_findings = analyze_delta(
        before_files,
        after_files,
        runtime_edges=runtime_edges if runtime_edges else None,
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
            "include_roots": [str(root)],
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

    check_rc = _run_git_check(
        root=root,
        base_ref=base_ref,
        runtime_dir=shard_dir,
        max_bytes=max_bytes,
        as_json=as_json,
        fail_on_findings=fail_on_findings,
    )
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

    root = args.root.resolve()
    runtime_dir = args.runtime_dir.resolve() if args.runtime_dir else (root / ".mipdelta" / "runtime")

    if args.run_pytest:
        return _run_monitored_pytest(
            root=root,
            pytest_args=args.pytest_args,
            base_ref=args.base_ref,
            runtime_dir=runtime_dir,
            max_bytes=args.max_bytes,
            as_json=args.json,
            fail_on_findings=args.fail_on_findings,
        )

    if args.check_git:
        return _run_git_check(
            root=root,
            base_ref=args.base_ref,
            runtime_dir=runtime_dir,
            max_bytes=args.max_bytes,
            as_json=args.json,
            fail_on_findings=args.fail_on_findings,
        )

    if not args.write_path or args.candidate_file is None:
        parser.error("Either pass --check-git, --run-pytest, or both --write-path and --candidate-file.")

    files = _load_python_tree(root, include_stubs=False)
    stub_files = {
        k: v for k, v in _load_python_tree(root, include_stubs=True).items() if k.endswith(".pyi")
    }
    session = DeltaSession.from_workspace(
        files, stub_texts=stub_files, max_advisory_bytes=args.max_bytes
    )
    runtime_edges = _load_runtime_edges(runtime_dir)
    if runtime_edges:
        session.add_runtime_edges(runtime_edges)

    new_text = args.candidate_file.read_bytes().decode("utf-8")
    res = session.apply_write(args.write_path, new_text)

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
