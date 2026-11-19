"""Clone audited Sherlock repos at their pinned commits and run the frozen analyzer.

Never executes anything inside a clone except `git` itself.
Candidates are NOT computed here (they need labeled obligation sites; Task 6).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from match_hits import is_production_sol, norm_path  # noqa: E402

DEFAULT_CACHE = ROOT / ".mipdelta" / "sherlock_cache" / "clones"
_LOCAL_PATH_RE = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[/\\]|file://)", re.ASCII)


def _assert_no_local_paths(obj) -> None:
    match = _LOCAL_PATH_RE.search(json.dumps(obj, ensure_ascii=False))
    if match is not None:
        raise ValueError(f"Output contains local path pattern {match.group(0)!r}")


def remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _git_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_ASKPASS"] = ""
    return env


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill the child and all its descendants (git spawns git-remote-https)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        p.kill()


def _run_with_tree_kill(argv, *, cwd=None, env=None, timeout, text=True, input=None) -> subprocess.CompletedProcess:
    """Like subprocess.run(capture_output, text) but a timeout kills the whole process tree
    and always re-raises TimeoutExpired instead of blocking on inherited pipes."""
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, stdin=subprocess.PIPE if input is not None else None,
                         text=text,
                         encoding="utf-8" if text else None)
    try:
        out, err = (p.communicate(timeout=timeout) if input is None
                     else p.communicate(input, timeout=timeout))
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def _git(d: Path, *args: str, text: bool = True, input=None) -> subprocess.CompletedProcess:
    timeout = 600 if args and args[0] in ("fetch",) else 120
    return _run_with_tree_kill(["git", "-c", "core.longpaths=true", "-C", str(d), *args],
                               env=_git_env(), timeout=timeout, text=text, input=input)


_WIN_FORBIDDEN_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
LAST_PLUMBING_DROPPED = 0  # entries dropped by the most recent plumbing checkout fallback


def _safe_tree_path(path: str) -> bool:
    if not path or path.startswith("/") or _WIN_FORBIDDEN_RE.search(path):
        return False
    for comp in path.split("/"):
        if comp in ("", ".", "..") or comp.lower() == ".git" or comp[-1] in ". ":
            return False
    return True


def _plumbing_checkout(d: Path, run) -> int:
    """Write FETCH_HEAD's regular files into d without touching the index (so git's NTFS
    path check never runs). `run(args, input=None)` runs a git subcommand returning bytes.
    Returns the number of dropped entries. Never executes anything from the tree."""
    ls = run(["ls-tree", "-r", "-z", "FETCH_HEAD"])
    if ls.returncode != 0:
        raise RuntimeError(f"git ls-tree failed: {ls.stderr.decode('utf-8', 'replace').strip()[:300]}")
    dropped = 0
    keep: list[tuple[str, str]] = []  # (oid, relpath)
    for entry in ls.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, raw = entry.partition(b"\t")
        parts = meta.split(b" ")
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            dropped += 1
            continue
        if (len(parts) != 3 or parts[1] != b"blob" or parts[0] not in (b"100644", b"100755")
                or not _safe_tree_path(path)):
            dropped += 1
            continue
        keep.append((parts[2].decode("ascii"), path))
    root = d.resolve()
    if keep:
        cf = run(["cat-file", "--batch"], input="".join(oid + "\n" for oid, _ in keep).encode("ascii"))
        if cf.returncode != 0:
            raise RuntimeError(f"git cat-file failed: {cf.stderr.decode('utf-8', 'replace').strip()[:300]}")
        buf, pos = cf.stdout, 0
        for oid, path in keep:
            nl = buf.index(b"\n", pos)
            header = buf[pos:nl].split(b" ")
            if len(header) != 3 or header[0].decode("ascii") != oid or header[1] != b"blob":
                raise RuntimeError(f"git cat-file: unexpected header for {oid}")
            size = int(header[2])
            data = buf[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
            target = (d / path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                dropped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return dropped


def clone_at(repo: str, sha: str, cache: Path) -> Path:
    """Shallow-fetch `sha` of `repo` into cache/<owner>__<name>@<sha>; reuse if present."""
    d = Path(cache) / f"{repo.replace('/', '__')}@{sha}"

    def git(*args: str) -> subprocess.CompletedProcess:
        try:
            return _git(d, *args)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"git {args[0]} timed out after {exc.timeout}s for {repo}@{sha}") from exc

    if (d / ".git").exists():
        head = git("rev-parse", "HEAD")
        if head.returncode == 0 and head.stdout.strip() == sha:
            return d
    d.mkdir(parents=True, exist_ok=True)
    steps = [("init", "-q"),
             ("fetch", "--depth", "1", remote_url(repo), sha),
             ("checkout", "--quiet", "--force", "FETCH_HEAD")]
    for step in steps:
        r = git(*step)
        if r.returncode != 0 and step[0] == "checkout" and "invalid path" in r.stderr:
            # Paths illegal on NTFS (e.g. ':'): no git security setting is changed. Write the
            # tree's regular files via ls-tree/cat-file (no index), then point HEAD at the SHA.
            global LAST_PLUMBING_DROPPED

            def run(args, input=None):
                try:
                    return _git(d, *args, text=False, input=input)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        f"git {args[0]} timed out after {exc.timeout}s for {repo}@{sha}") from exc

            LAST_PLUMBING_DROPPED = _plumbing_checkout(d, run)
            r = git("update-ref", "HEAD", "FETCH_HEAD")
            if r.returncode != 0:
                raise RuntimeError(
                    f"git update-ref failed for {repo}@{sha}: {r.stderr.strip()[:300]}")
            continue
        if r.returncode != 0:
            raise RuntimeError(f"git {step[0]} failed for {repo}@{sha}: {r.stderr.strip()[:300]}")
    return d


def run_audit(root: Path, label: str = "") -> dict:
    """Run the analyzer with --root = clone root; return parsed JSON plus wall_ms."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    t0 = time.perf_counter()
    who = label or "analyzer run"
    try:
        proc = _run_with_tree_kill(
            [sys.executable, "-m", "mipdelta.cli", "--audit-solidity", "--root", str(root), "--json"],
            cwd=ROOT, env=env, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{who}: analyzer timed out after {exc.timeout}s") from exc
    wall_ms = round((time.perf_counter() - t0) * 1000, 1)
    if proc.returncode != 0:
        raise RuntimeError(f"{who}: analyzer exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"{who}: unparseable analyzer stdout ({exc})") from exc
    if not isinstance(data, dict) or "findings" not in data:
        raise RuntimeError(f"{who}: analyzer JSON has no 'findings'")
    data["wall_ms"] = wall_ms
    return data


def _production_counts(root: Path) -> tuple[int, int]:
    files = lines = 0
    for p in sorted(root.rglob("*.sol")):
        rel = p.relative_to(root).as_posix()
        if rel.split("/")[0] == ".git" or not p.is_file():
            continue
        if is_production_sol(rel):
            files += 1
            lines += len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    return files, lines


def audit_contest(contest: dict, clone: Path) -> dict:
    out = run_audit(clone, label=contest["name"])
    allf = out["findings"]
    kept = []
    for f in allf:
        path = norm_path(f.get("path", ""))
        if is_production_sol(path):
            kept.append({**f, "path": path})
    pfiles, plines = _production_counts(clone)
    return {
        "name": contest["name"],
        "companion_repo": contest["companion_repo"],
        "resolved_sha": contest["resolved_sha"],
        "files_scanned": out.get("files_scanned", 0),
        "production_files": pfiles,
        "production_lines": plines,
        "wall_ms": out["wall_ms"],
        "all_findings_count": len(allf),
        "findings": kept,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="which", required=True, choices=["dev", "measurement"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine-commit", required=True)
    ap.add_argument("--recall-set", default=None)
    ap.add_argument("--cache", default=None)
    args = ap.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rs_path = Path(args.recall_set) if args.recall_set else out_dir / "recall_set.json"
    cache = Path(args.cache) if args.cache else DEFAULT_CACHE
    contests = [c for c in json.loads(rs_path.read_text(encoding="utf-8"))["contests"]
                if c["set"] == args.which]

    results = []
    for c in contests:
        try:
            clone = clone_at(c["companion_repo"], c["resolved_sha"], cache)
            results.append(audit_contest(c, clone))
        except Exception as exc:  # per-contest isolation: record and continue
            msg = re.sub(r"(?:[A-Za-z]:[/\\][^\s'\"]*|file://\S*)", "<path>", str(exc))
            results.append({"name": c["name"], "companion_repo": c["companion_repo"],
                            "resolved_sha": c["resolved_sha"], "error": f"{type(exc).__name__}: {msg}"})
    payload = {"set": args.which, "engine_commit": args.engine_commit, "contests": results}
    _assert_no_local_paths(payload)
    (out_dir / f"hits_{args.which}.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
