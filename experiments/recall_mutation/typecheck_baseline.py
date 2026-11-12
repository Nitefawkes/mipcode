"""Type-checker baseline for the stale-file mutation corpus: does mypy report the stale call?

For each mutation in a corpus written by `build_and_score.py`, the script materializes the
commit's tree C from the local clone with read-only Git plumbing (`git ls-tree` and
`git cat-file`), runs mypy on the package that contains the stale file, then overwrites the
stale file with the parent's blob (tree M) and runs mypy again with the same incremental cache.
Errors present for M and absent for C, compared as a multiset of (path, error code, message)
with line numbers ignored, are new. They are matched against the corpus's ground-truth sites
and set against mip-delta's hits from the corpus.

mypy's configuration is fixed here and independent of the target project: an empty config file,
no site-packages, missing imports ignored, untyped bodies checked. Nothing from the target
repository is checked out, installed, imported or executed; mypy only reads its source files.
Standard library only; mypy runs in a separate process.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA = "mip-delta recall-mutation typecheck baseline v1"
BINDINGS = ("breaking", "breaking_removed", "removed_shimmed", "compatible", "unknown")
UNIQUENESS = ("unique", "ambiguous")
STATUSES = ("ok", "timeout", "mypy_crash", "blocking_error", "materialize_failed")
SOURCE_SUFFIXES = (".py", ".pyi")
SOURCE_NAMES = ("py.typed",)

# Flags fixed by this script. `{config}` and `{cache}` are filled per run; the target follows.
MYPY_FLAGS = (
    "--config-file", "{config}",
    "--cache-dir", "{cache}",
    "--no-site-packages",
    "--ignore-missing-imports",
    "--check-untyped-defs",
    "--no-error-summary",
    "--hide-error-context",
    "--no-color-output",
    "--show-column-numbers",
    "--show-error-codes",
    "--no-pretty",
)
# Environment variables that would change what mypy or its interpreter reads.
SCRUBBED_ENV_PREFIXES = ("MYPY", "PYTHON")

ERROR_LINE = re.compile(
    r"^(?P<path>(?:[A-Za-z]:)?[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)? "
    r"(?P<severity>error|warning|note): (?P<message>.*?)(?:  \[(?P<code>[a-z0-9-]+)\])?$"
)
LINE_REFERENCE = re.compile(r"\bline \d+\b")


# --------------------------------------------------------------------------- git plumbing


class Clone:
    """Read-only access to a local clone through `git ls-tree` and `git cat-file`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _git(self, *args: str, data: bytes | None = None) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.path), *args], input=data, capture_output=True, check=True
        ).stdout

    def source_blobs(self, commit: str) -> dict[str, str]:
        """Python sources and `py.typed` markers in the commit's tree: path -> blob id."""
        out: dict[str, str] = {}
        for record in self._git("ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
            if not record:
                continue
            meta, _, raw_path = record.partition(b"\t")
            mode, kind, blob = meta.decode("ascii").split(" ")
            path = raw_path.decode("utf-8", "surrogateescape")
            if kind != "blob" or mode not in ("100644", "100755"):
                continue
            if path.endswith(SOURCE_SUFFIXES) or path.rsplit("/", 1)[-1] in SOURCE_NAMES:
                out[path] = blob
        return out

    def blobs(self, ids: list[str]) -> dict[str, bytes]:
        unique = sorted(set(ids))
        raw = self._git("cat-file", "--batch", data="".join(f"{blob}\n" for blob in unique).encode("ascii"))
        out: dict[str, bytes] = {}
        pos = 0
        for blob in unique:
            end = raw.index(b"\n", pos)
            header = raw[pos:end].decode("ascii").split(" ")
            if len(header) != 3 or header[1] != "blob":
                raise ValueError(f"cat-file could not read {blob}: {' '.join(header)}")
            size = int(header[2])
            out[blob] = raw[end + 1 : end + 1 + size]
            pos = end + 1 + size + 1
        return out


def _long(path: Path) -> str:
    """Path string that survives Windows' 260 character limit."""
    text = str(path.resolve())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def materialize(clone: Clone, commit: str, tree: Path) -> dict[str, Any]:
    """Write the commit's Python sources into `tree`. Files that cannot be written are counted."""
    blobs = clone.source_blobs(commit)
    contents = clone.blobs(list(blobs.values()))
    skipped: list[str] = []
    for path, blob in blobs.items():
        target = tree.joinpath(*path.split("/"))
        try:
            os.makedirs(_long(target.parent), exist_ok=True)
            with open(_long(target), "wb") as handle:
                handle.write(contents[blob])
        except OSError:
            skipped.append(path)
    return {"files": len(blobs), "unwritable": sorted(skipped), "blobs": blobs}


def package_target(tree: Path, stale_path: str) -> str:
    """Top-most directory reached by walking up from the stale file while `__init__.py` exists."""
    parts = stale_path.split("/")[:-1]
    top: list[str] | None = None
    while parts and (tree.joinpath(*parts) / "__init__.py").is_file():
        top = list(parts)
        parts = parts[:-1]
    return "/".join(top) if top else stale_path


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(_long(path), ignore_errors=True)


# --------------------------------------------------------------------------- mypy


class Mypy:
    def __init__(self, command: list[str], timeout: float, incremental: bool, config: Path) -> None:
        self.command = command
        self.timeout = timeout
        self.incremental = incremental
        self.config = config
        self.env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith(SCRUBBED_ENV_PREFIXES)
        }

    def argv(self, cache: Path, target: str) -> list[str]:
        flags = [flag.format(config=self.config, cache=cache) for flag in MYPY_FLAGS]
        if not self.incremental:
            flags += ["--no-incremental"]
        return [*self.command, *flags, target]

    def template(self) -> list[str]:
        """The command as recorded: executable reduced to its basename, per-run paths as placeholders."""
        head = [Path(self.command[0]).name, *self.command[1:]]
        flags = list(MYPY_FLAGS) + ([] if self.incremental else ["--no-incremental"])
        return [*head, *flags, "{target}"]

    def version(self) -> str:
        proc = subprocess.run([*self.command, "--version"], capture_output=True, text=True, env=self.env, timeout=120)
        return (proc.stdout or proc.stderr).strip()

    def run(self, tree: Path, cache: Path, target: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                self.argv(cache, target),
                cwd=tree,
                capture_output=True,
                env=self.env,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "seconds": round(time.perf_counter() - started, 2), "errors": []}
        seconds = round(time.perf_counter() - started, 2)
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        errors, unparsed = parse_output(stdout, tree)
        crashed = (
            proc.returncode not in (0, 1, 2)
            or "INTERNAL ERROR" in stdout + stderr
            or "Traceback (most recent call last)" in stdout + stderr
        )
        if crashed:
            status = "mypy_crash"
        elif proc.returncode == 2:
            status = "blocking_error"
        elif proc.returncode == 1 and not errors:
            status = "mypy_crash"  # failed without a parsable error line
        else:
            status = "ok"
        row: dict[str, Any] = {"status": status, "seconds": seconds, "returncode": proc.returncode, "errors": errors}
        if status != "ok":
            row["detail"] = (stderr.strip() or "\n".join(unparsed) or stdout.strip())[-2000:]
        if unparsed:
            row["unparsed_lines"] = len(unparsed)
        return row


def parse_output(stdout: str, tree: Path) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[dict[str, Any]] = []
    unparsed: list[str] = []
    for line in stdout.splitlines():
        match = ERROR_LINE.match(line)
        if not match:
            if line.strip():
                unparsed.append(line)
            continue
        if match["severity"] != "error":
            continue
        path = match["path"]
        if os.path.isabs(path):
            try:
                path = os.path.relpath(path, tree)
            except ValueError:
                pass
        errors.append(
            {
                "path": path.replace("\\", "/"),
                "line": int(match["line"]),
                "column": int(match["col"]) if match["col"] else None,
                "code": match["code"],
                "message": match["message"],
            }
        )
    return errors, unparsed


def error_key(error: dict[str, Any]) -> tuple[str, str | None, str]:
    return error["path"], error["code"], LINE_REFERENCE.sub("line N", error["message"])


def line_map(old_text: str, new_text: str) -> dict[int, int]:
    """1-based lines of `old_text` that are unchanged in `new_text`, mapped to their new line."""
    mapping: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(None, old_text.splitlines(), new_text.splitlines(), autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset + 1] = block.b + offset + 1
    return mapping


def new_errors(
    mutated: list[dict[str, Any]], complete: list[dict[str, Any]], stale_path: str, c_to_m: dict[int, int]
) -> list[dict[str, Any]]:
    """Errors for M left over after removing C's errors as a multiset keyed by (path, code, message).

    The count of new errors per key is the multiset difference. When a key has more instances in M
    than in C, which M instances count as new is decided by pairing each C instance with the M
    instance on its line in M (lines of the stale file are carried over by a line diff), then with
    the nearest remaining one. Line numbers never decide whether an error is new, only which one.
    """
    by_key_m: dict[tuple, list[dict[str, Any]]] = {}
    by_key_c: dict[tuple, list[dict[str, Any]]] = {}
    for error in mutated:
        by_key_m.setdefault(error_key(error), []).append(error)
    for error in complete:
        by_key_c.setdefault(error_key(error), []).append(error)
    fresh: list[dict[str, Any]] = []
    for key, m_rows in by_key_m.items():
        c_rows = by_key_c.get(key, [])
        if len(m_rows) <= len(c_rows):
            continue
        open_m = list(range(len(m_rows)))
        expected = [
            (c_to_m.get(row["line"]) if row["path"] == stale_path else row["line"]) for row in c_rows
        ]
        unpaired: list[int | None] = []
        for line in expected:
            exact = next((i for i in open_m if line is not None and m_rows[i]["line"] == line), None)
            if exact is None:
                unpaired.append(line)
            else:
                open_m.remove(exact)
        for line in unpaired:
            nearest = min(open_m, key=lambda i: abs(m_rows[i]["line"] - (line or 0)))
            open_m.remove(nearest)
        fresh.extend(m_rows[i] for i in open_m)
    return sorted(fresh, key=lambda e: (e["path"], e["line"], e["column"] or 0, e["code"] or "", e["message"]))


def parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None


def import_spans(tree: ast.Module | None, name: str) -> list[tuple[int, int]]:
    """Line spans of `from ... import` statements, at any depth, that import `name`."""
    if tree is None:
        return []
    return [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and any(alias.name == name for alias in node.names)
    ]


def enclosing_span(tree: ast.Module | None, line: int) -> tuple[int, int] | None:
    """Line span of the innermost function definition that contains `line`, decorators excluded."""
    if tree is None:
        return None
    best: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end and (best is None or node.lineno >= best[0]):
                best = (node.lineno, end)
    return best


# --------------------------------------------------------------------------- per repository


def _site_row(site: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "binding": site["binding"],
        "name_uniqueness": site["name_uniqueness"],
        "call_rewritten_in_commit": site.get("call_rewritten_in_commit"),
        "definition_change": site["definition"]["change"],
        "definition_path": site["definition"]["path"],
        "definition_qualname": site["definition"]["qualname"],
        "line": site["line"],
        "end_line": site["end_line"],
        "call_line": site["call_line"],
        "site_hit": site["site_hit"],
        "file_hit": site["file_hit"],
        "tc_status": status,
        "tc_site_hit": False,
        "tc_site_hit_names_definition": False,
        "tc_import_hit": False,
        "tc_site_or_import_hit": False,
        "tc_enclosing_hit": False,
        "tc_file_hit": False,
        "tc_site_hit_errors": [],
    }


def _mutation_shell(mutation: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": mutation["id"],
        "commit": mutation["commit"],
        "parent": mutation["parent"],
        "stale_path": mutation["stale_path"],
        "stale_blob": mutation["stale_blob"],
        "mipdelta_analysis_error": mutation.get("analysis_error"),
        "tc_status": status,
        **extra,
        "sites": [{"index": i, **_site_row(site, status)} for i, site in enumerate(mutation["sites"])],
    }


def run_repository(
    corpus: dict[str, Any], clone: Clone, mypy: Mypy, work: Path, max_mutations: int | None, keep_work: bool
) -> dict[str, Any]:
    started = time.perf_counter()
    mutations = sorted(corpus["mutations"], key=lambda m: m["id"])
    if max_mutations is not None:
        mutations = mutations[:max_mutations]
    by_commit: dict[str, list[dict[str, Any]]] = {}
    for mutation in mutations:
        by_commit.setdefault(mutation["commit"], []).append(mutation)
    rows: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []
    for commit, group in by_commit.items():
        root = work / corpus["repository"] / commit[:12]
        remove_tree(root)
        tree, caches = root / "t", root / "c"
        commit_row: dict[str, Any] = {"commit": commit, "mutations": len(group), "targets": {}}
        commits.append(commit_row)
        try:
            info = materialize(clone, commit, tree)
            commit_row["files"] = info["files"]
            commit_row["unwritable_files"] = info["unwritable"]
            stale_bytes = clone.blobs([m["stale_blob"] for m in group])
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            commit_row["materialize_error"] = f"{type(exc).__name__}: {exc}"[:500]
            rows.extend(_mutation_shell(m, "materialize_failed") for m in group)
            remove_tree(root)
            continue
        baselines: dict[str, dict[str, Any]] = {}
        for mutation in group:
            path = mutation["stale_path"]
            stale_file = tree.joinpath(*path.split("/"))
            if info["blobs"].get(path) != mutation["commit_blob"] or path in info["unwritable"]:
                rows.append(_mutation_shell(mutation, "materialize_failed", detail="stale file missing in commit tree"))
                continue
            target = package_target(tree, path)
            if target not in baselines:
                cache = caches / str(len(baselines))
                baselines[target] = mypy.run(tree, cache, target) | {"cache": cache}
                base = baselines[target]
                commit_row["targets"][target] = {
                    "status": base["status"],
                    "seconds": base["seconds"],
                    "errors": len(base["errors"]),
                    "errors_by_code": dict(Counter(e["code"] for e in base["errors"]).most_common()),
                    **({"detail": base["detail"]} if "detail" in base else {}),
                }
            base = baselines[target]
            if base["status"] != "ok":
                rows.append(_mutation_shell(mutation, base["status"], target=target, detail="commit tree: " + base["status"]))
                continue
            with open(_long(stale_file), "rb") as handle:
                original = handle.read()
            try:
                with open(_long(stale_file), "wb") as handle:
                    handle.write(stale_bytes[mutation["stale_blob"]])
                result = mypy.run(tree, base["cache"], target)
            finally:
                with open(_long(stale_file), "wb") as handle:
                    handle.write(original)
            rows.append(score_mutation(mutation, base, result, target, original, stale_bytes[mutation["stale_blob"]]))
        if not keep_work:
            remove_tree(root)
    if not keep_work:
        remove_tree(work / corpus["repository"])
    return {
        "schema": SCHEMA,
        "repository": corpus["repository"],
        "corpus_schema": corpus.get("schema"),
        "corpus_head": corpus.get("head"),
        "max_mutations": max_mutations,
        "wall_seconds": round(time.perf_counter() - started, 1),
        "commits": commits,
        "mutations": rows,
    }


def score_mutation(
    mutation: dict[str, Any],
    base: dict[str, Any],
    result: dict[str, Any],
    target: str,
    commit_bytes: bytes,
    stale_bytes: bytes,
) -> dict[str, Any]:
    path = mutation["stale_path"]
    extra: dict[str, Any] = {"target": target, "seconds": result["seconds"]}
    if result["status"] != "ok":
        return _mutation_shell(mutation, result["status"], **extra, detail=result.get("detail"))
    commit_text = commit_bytes.decode("utf-8", "replace")
    stale_text = stale_bytes.decode("utf-8", "replace")
    fresh = new_errors(result["errors"], base["errors"], path, line_map(commit_text, stale_text))
    in_file = [e for e in fresh if e["path"] == path]
    elsewhere = [e for e in fresh if e["path"] != path]
    row = _mutation_shell(
        mutation,
        "ok",
        **extra,
        errors_commit=len(base["errors"]),
        errors_mutated=len(result["errors"]),
        new_errors_in_stale_file=in_file,
        new_errors_elsewhere=elsewhere,
    )
    stale_tree = parse(stale_text)
    for site_row, site in zip(row["sites"], mutation["sites"]):
        name = site["definition"]["name"]
        span = enclosing_span(stale_tree, site["line"])
        imports = import_spans(stale_tree, name)
        hits = [e for e in in_file if site["line"] <= e["line"] <= site["end_line"] or e["line"] == site["call_line"]]
        enclosing = [e for e in in_file if span and span[0] <= e["line"] <= span[1]]
        # mypy reports a removed name once, where it is imported, and types the name as Any after that.
        import_hits = [
            e
            for e in in_file
            if any(a <= e["line"] <= b for a, b in imports) and f'"{name}"' in e["message"]
        ]
        site_row["tc_site_hit"] = bool(hits)
        # Line attribution alone also credits unrelated errors that happen to sit on the call's lines.
        site_row["tc_site_hit_names_definition"] = any(f'"{name}"' in e["message"] for e in hits)
        site_row["tc_import_hit"] = bool(import_hits)
        site_row["tc_site_or_import_hit"] = bool(hits or import_hits)
        site_row["tc_enclosing_hit"] = bool(hits or enclosing)
        site_row["tc_file_hit"] = bool(in_file)
        site_row["tc_site_hit_errors"] = [{k: e[k] for k in ("line", "code", "message")} for e in hits]
        site_row["tc_import_hit_errors"] = [{k: e[k] for k in ("line", "code", "message")} for e in import_hits]
        site_row["tc_site_hit_codes"] = sorted({e["code"] or "" for e in hits})
        site_row["tc_file_hit_codes"] = sorted({e["code"] or "" for e in in_file})
        site_row["enclosing_function_span"] = list(span) if span else None
    return row


# --------------------------------------------------------------------------- summary

# (label, mip-delta flag, mypy flag) per comparison. "site" is the primary criterion; the others
# credit mypy for reporting at the import of a removed name or anywhere in the enclosing function
# (mip-delta's site_hit already counts its own findings in the enclosing function).
COMPARISONS = (
    ("site", "site_hit", "tc_site_hit"),
    ("site_or_import", "site_hit", "tc_site_or_import_hit"),
    ("site_or_enclosing", "site_hit", "tc_enclosing_hit"),
    ("file", "file_hit", "tc_file_hit"),
)


def _overlap(sites: list[dict[str, Any]], mip: str, tc: str) -> dict[str, Any]:
    excluded = Counter()
    scored = []
    for site in sites:
        if site["_mipdelta_error"]:
            excluded["mipdelta_analysis_error"] += 1
        elif site["tc_status"] != "ok":
            excluded[site["tc_status"]] += 1
        else:
            scored.append(site)

    def table(units: list[tuple[bool, bool]]) -> dict[str, Any]:
        both = sum(a and b for a, b in units)
        mip_only = sum(a and not b for a, b in units)
        tc_only = sum(b and not a for a, b in units)
        return {
            "scored": len(units),
            "both": both,
            "mip_delta_only": mip_only,
            "mypy_only": tc_only,
            "neither": len(units) - both - mip_only - tc_only,
            "mip_delta_rate": round((both + mip_only) / len(units), 3) if units else None,
            "mypy_rate": round((both + tc_only) / len(units), 3) if units else None,
            "union_rate": round((both + mip_only + tc_only) / len(units), 3) if units else None,
        }

    groups: dict[str, list[dict[str, Any]]] = {}
    for site in scored:
        groups.setdefault(site["_mutation"], []).append(site)
    excluded_mutations = Counter()
    seen: set[str] = set()
    for site in sites:
        if site["_mutation"] in seen or site["_mutation"] in groups:
            continue
        seen.add(site["_mutation"])
        excluded_mutations["mipdelta_analysis_error" if site["_mipdelta_error"] else site["tc_status"]] += 1
    return {
        "sites": {**table([(s[mip], s[tc]) for s in scored]), "excluded": dict(excluded)},
        "mutations": {
            **table([(any(s[mip] for s in rows), any(s[tc] for s in rows)) for rows in groups.values()]),
            "excluded": dict(excluded_mutations),
        },
    }


def _strata(sites: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for binding in BINDINGS:
        for uniqueness in UNIQUENESS:
            out[f"{binding} x {uniqueness}"] = [
                s for s in sites if s["binding"] == binding and s["name_uniqueness"] == uniqueness
            ]
    for binding in BINDINGS:
        out[f"{binding} x any"] = [s for s in sites if s["binding"] == binding]
    for uniqueness in UNIQUENESS:
        out[f"any x {uniqueness}"] = [s for s in sites if s["name_uniqueness"] == uniqueness]
    breaking = {"breaking", "breaking_removed"}
    out["breaking_or_removed x unique"] = [
        s for s in sites if s["binding"] in breaking and s["name_uniqueness"] == "unique"
    ]
    out["breaking_or_removed x unique, call rewritten"] = [
        s for s in out["breaking_or_removed x unique"] if s["call_rewritten_in_commit"]
    ]
    out["all"] = list(sites)
    return out


def summarize_rows(mutations: list[dict[str, Any]]) -> dict[str, Any]:
    sites = [
        {**site, "_mutation": m["id"], "_mipdelta_error": bool(m["mipdelta_analysis_error"])}
        for m in mutations
        for site in m["sites"]
    ]
    ok = [m for m in mutations if m["tc_status"] == "ok" and not m["mipdelta_analysis_error"]]
    seconds = sorted(m["seconds"] for m in mutations if m["tc_status"] == "ok" and "seconds" in m)
    return {
        "mutations": len(mutations),
        "sites": len(sites),
        "mutation_status": dict(Counter(m["tc_status"] for m in mutations)),
        "mutations_with_mipdelta_analysis_error": sum(bool(m["mipdelta_analysis_error"]) for m in mutations),
        "new_errors_in_stale_files": sum(len(m["new_errors_in_stale_file"]) for m in ok),
        "new_errors_elsewhere": sum(len(m["new_errors_elsewhere"]) for m in ok),
        "mutations_with_new_errors_elsewhere": sum(bool(m["new_errors_elsewhere"]) for m in ok),
        "new_error_codes_in_stale_files": dict(
            Counter(e["code"] for m in ok for e in m["new_errors_in_stale_file"]).most_common()
        ),
        "mutated_run_seconds": {
            "count": len(seconds),
            "total": round(sum(seconds), 1),
            "median": seconds[len(seconds) // 2] if seconds else None,
            "max": seconds[-1] if seconds else None,
        },
        "overlap": {
            label: {name: _overlap(rows, mip, tc) for name, rows in _strata(sites).items()}
            for label, mip, tc in COMPARISONS
        },
    }


def summarize(reports: list[dict[str, Any]], environment: dict[str, Any]) -> dict[str, Any]:
    pooled: list[dict[str, Any]] = []
    per_repository = {}
    for report in reports:
        commit_seconds = [t["seconds"] for c in report["commits"] for t in c["targets"].values()]
        per_repository[report["repository"]] = {
            "wall_seconds": report["wall_seconds"],
            "commit_runs": len(commit_seconds),
            "commit_run_seconds_total": round(sum(commit_seconds), 1),
            **summarize_rows(report["mutations"]),
        }
        pooled.extend(report["mutations"])
    return {"schema": SCHEMA, **environment, "per_repository": per_repository, "pooled": summarize_rows(pooled)}


def print_summary(summary: dict[str, Any], stream: Any = None) -> None:
    stream = stream or sys.stdout
    print(f"mypy: {summary['mypy_version']}; python: {summary['python_version']}", file=stream)
    print("command: " + " ".join(summary["command"]), file=stream)
    sections = [*summary["per_repository"].items(), ("POOLED", summary["pooled"])]
    for name, section in sections:
        timing = section["mutated_run_seconds"]
        print(
            f"\n== {name}: {section['mutations']} mutations, {section['sites']} sites; status {section['mutation_status']}; "
            f"mip-delta analysis errors {section['mutations_with_mipdelta_analysis_error']}; "
            f"mutated-run median {timing['median']} s, max {timing['max']} s; "
            f"new mypy errors in stale files {section['new_errors_in_stale_files']}, "
            f"elsewhere {section['new_errors_elsewhere']}",
            file=stream,
        )
        for label, _, _ in COMPARISONS:
            cols = f"{'both':>5} {'mip':>5} {'mypy':>5} {'none':>5} {'excl':>5}"
            print(f"-- {label}: sites, then mutations; mip = mip-delta only, mypy = mypy only", file=stream)
            print(f"{'stratum':46} {cols}   {cols}", file=stream)
            for stratum, cell in section["overlap"][label].items():
                s, m = cell["sites"], cell["mutations"]
                if not s["scored"] and not sum(s["excluded"].values()) and " x any" not in stratum and stratum != "all":
                    continue
                print(
                    f"{stratum:46} {s['both']:5d} {s['mip_delta_only']:5d} {s['mypy_only']:5d} {s['neither']:5d} "
                    f"{sum(s['excluded'].values()):5d}   {m['both']:5d} {m['mip_delta_only']:5d} {m['mypy_only']:5d} "
                    f"{m['neither']:5d} {sum(m['excluded'].values()):5d}",
                    file=stream,
                )


# --------------------------------------------------------------------------- main


def _repo_map(values: list[str]) -> dict[str, Path]:
    out = {}
    for value in values:
        name, sep, path = value.partition("=")
        if not sep or not name or not path:
            raise SystemExit(f"--repo expects NAME=PATH, got {value!r}")
        out[name] = Path(path)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, action="append", required=True, help="corpus.json (repeatable)")
    parser.add_argument("--repo", action="append", default=[], help="NAME=PATH of a local clone (repeatable)")
    parser.add_argument("--work", type=Path, required=True, help="Scratch directory for materialized trees")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--python", default=sys.executable, help="Interpreter that has mypy installed")
    parser.add_argument("--mypy", default=None, help="mypy command (default: <python> -I -X utf8 -m mypy)")
    parser.add_argument("--max-mutations", type=int, default=None, help="First N mutations by id, per corpus")
    parser.add_argument("--timeout", type=float, default=900.0, help="Seconds per mypy run")
    parser.add_argument("--no-incremental", action="store_true", help="Run every mypy check from scratch")
    parser.add_argument("--keep-work", action="store_true", help="Keep materialized trees and caches")
    args = parser.parse_args(argv)

    repos = _repo_map(args.repo)
    corpora = [json.loads(path.read_text(encoding="utf-8")) for path in args.corpus]
    missing = sorted({c["repository"] for c in corpora} - repos.keys())
    if missing:
        raise SystemExit(f"no --repo NAME=PATH for: {', '.join(missing)}")

    args.work.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    config = args.work / "mypy-empty.ini"
    config.write_text("[mypy]\n", encoding="utf-8")
    # -I keeps the interpreter from putting the target tree (mypy's cwd) on sys.path; -X utf8 fixes
    # the encoding of mypy's piped output on Windows.
    command = (
        shlex.split(args.mypy, posix=os.name != "nt") if args.mypy else [args.python, "-I", "-X", "utf8", "-m", "mypy"]
    )
    mypy = Mypy(command, args.timeout, not args.no_incremental, config.resolve())
    python_version = subprocess.run(
        [args.python, "-I", "-c", "import sys; print(sys.version.split()[0])"], capture_output=True, text=True
    ).stdout.strip()
    environment = {
        "mypy_version": mypy.version(),
        "python_version": python_version,
        "command": mypy.template(),
        "config_file_contents": config.read_text(encoding="utf-8"),
        "incremental": mypy.incremental,
        "timeout_seconds": args.timeout,
        "corpora": [
            {"repository": c["repository"], "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for c, p in zip(corpora, args.corpus)
        ],
    }
    reports = []
    for corpus in corpora:
        report = run_repository(
            corpus, Clone(repos[corpus["repository"]]), mypy, args.work, args.max_mutations, args.keep_work
        )
        report["environment"] = environment
        target = args.out / report["repository"]
        target.mkdir(parents=True, exist_ok=True)
        (target / "typecheck.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        reports.append(report)
        print(
            f"{report['repository']}: {len(report['mutations'])} mutations, {report['wall_seconds']} s",
            file=sys.stderr,
        )
    if not args.keep_work:
        config.unlink(missing_ok=True)
    summary = summarize(reports, environment)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
