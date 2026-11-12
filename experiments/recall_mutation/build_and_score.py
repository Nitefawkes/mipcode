"""Build a stale-file mutation corpus from commit history and score mip-delta's recall on it.

For each non-merge commit C with parent P, the script finds functions and methods whose
parameter list C changed, or that C removed, using the standard library `ast` module only.
In another production file Y that C also edited, the calls to such a definition that sit on
lines C changed are the ground-truth sites: upstream edited them in the same commit. The
mutation M is C's tree with Y put back to P's version, which is the state "the author changed
the definition and everything else, but forgot file Y". mip-delta is then run on P -> M and on
P -> C, and the findings new in M are matched against the sites.

The ground truth never uses mip-delta code. The target repository is read through Git
plumbing only. Nothing is checked out, imported, or executed. Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import difflib
import inspect
import json
import subprocess
import sys
import time
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "history_replay"))

from replay_history import (  # noqa: E402
    GitReader,
    analyze,
    is_support_path,
    is_test_path,
    site_key,
)
from mipdelta.cache import FactCache  # noqa: E402
from mipdelta.delta import OPT_IN_DETECTORS  # noqa: E402
from mipdelta.ledger import enclosing_function  # noqa: E402

warnings.simplefilter("ignore", SyntaxWarning)  # historical sources, never executed

SCHEMA = "mip-delta recall-mutation corpus v1"
MATERIALIZATION = (
    "Tree M is the tree of `commit` with the file at `stale_path` replaced by the blob `stale_blob` "
    "(the parent's version of that file). Every other path is exactly as in `commit`. "
    "Line numbers of sites refer to the `stale_blob` text, which is also the text of that file in M."
)
BINDINGS = ("breaking", "breaking_removed", "removed_shimmed", "compatible", "unknown")
UNIQUENESS = ("unique", "ambiguous")
CALL_TEXT_LIMIT = 400

# Names that builtins or methods of builtin types also provide. A call such as `d.get(k)` would
# otherwise match a repository's single `def get` and land in the "unique" stratum.
_BUILTIN_TYPES = (object, dict, list, tuple, set, frozenset, str, bytes, bytearray, int, float, complex, type)
BUILTIN_NAMES = frozenset(
    name
    for owner in (__import__("builtins"), *_BUILTIN_TYPES)
    for name in dir(owner)
    if not (name.startswith("__") and name.endswith("__"))
)


class CorpusGitReader(GitReader):
    """GitReader that checks parent presence with one batch call instead of one process per commit."""

    def commits(self) -> list[tuple[str, str, str]]:
        raw = self._run("log", "--no-merges", "--reverse", "--format=%H%x00%P%x00%s").decode("utf-8", "replace")
        rows = []
        for line in raw.splitlines():
            sha, parents, subject = (line.split("\x00") + ["", ""])[:3]
            rows.append((sha, parents.split(" ")[0] if parents else "", subject))
        wanted = sorted({parent for _, parent, _ in rows if parent})
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "cat-file", "--batch-check"],
            input="".join(f"{parent}^{{tree}}\n" for parent in wanted).encode("ascii"),
            capture_output=True,
            check=True,
        )
        present = {
            parent
            for parent, answer in zip(wanted, proc.stdout.decode("ascii", "replace").splitlines())
            if not answer.endswith("missing")
        }
        return [row for row in rows if row[1] in present]


def is_production(path: str) -> bool:
    return not is_test_path(path) and not is_support_path(path)


def is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


# --------------------------------------------------------------------------- definitions


@dataclass(frozen=True)
class Definition:
    qualname: str
    name: str
    line: int
    params: tuple[tuple[str, str, bool], ...]  # (name, kind, has_default)
    in_class: bool
    staticmethod: bool
    node: ast.FunctionDef | ast.AsyncFunctionDef

    @property
    def is_method(self) -> bool:
        return self.in_class and not self.staticmethod


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def parameters(args: ast.arguments) -> tuple[tuple[str, str, bool], ...]:
    positional = [*args.posonlyargs, *args.args]
    first_default = len(positional) - len(args.defaults)
    rows: list[tuple[str, str, bool]] = []
    for index, arg in enumerate(positional):
        kind = "positional_only" if index < len(args.posonlyargs) else "positional_or_keyword"
        rows.append((arg.arg, kind, index >= first_default))
    if args.vararg is not None:
        rows.append((args.vararg.arg, "var_positional", False))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        rows.append((arg.arg, "keyword_only", default is not None))
    if args.kwarg is not None:
        rows.append((args.kwarg.arg, "var_keyword", False))
    return tuple(rows)


def render_parameters(params: tuple[tuple[str, str, bool], ...]) -> str:
    parts: list[str] = []
    kinds = [kind for _, kind, _ in params]
    for index, (name, kind, has_default) in enumerate(params):
        if kind == "keyword_only" and "var_positional" not in kinds and (index == 0 or kinds[index - 1] != "keyword_only"):
            parts.append("*")
        text = {"var_positional": f"*{name}", "var_keyword": f"**{name}"}.get(kind, name)
        parts.append(text + ("=..." if has_default else ""))
        if kind == "positional_only" and (index + 1 == len(params) or kinds[index + 1] != "positional_only"):
            parts.append("/")
    return "(" + ", ".join(parts) + ")"


def _compound_bodies(node: ast.stmt) -> list[list[ast.stmt]]:
    """Statement lists nested in a compound statement that is not a function or class."""
    bodies = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(node, field, None)
        if isinstance(value, list):
            bodies.append(value)
    for handler in getattr(node, "handlers", []) or []:
        bodies.append(handler.body)
    for case in getattr(node, "cases", []) or []:
        bodies.append(case.body)
    return bodies


@dataclass
class FileDefinitions:
    callable_defs: dict[str, Definition]  # importable functions and methods, duplicates removed
    duplicates: set[str]
    defined_leafs: dict[str, set[str]]  # leaf name -> qualnames of every def/class at any depth
    assigned_leafs: set[str]  # module-level and class-level assignment targets
    getattr_strings: set[str]  # string constants inside a module-level __getattr__ (deprecation shims)


def file_definitions(text: str) -> FileDefinitions | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    found: dict[str, list[Definition]] = {}
    assigned: set[str] = set()
    getattr_strings: set[str] = set()

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "__getattr__" and not prefix:
                    getattr_strings.update(
                        sub.value
                        for sub in ast.walk(node)
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    )
                decorators = _decorator_names(node)
                if "overload" in decorators:
                    continue
                definition = Definition(
                    prefix + node.name,
                    node.name,
                    node.lineno,
                    parameters(node.args),
                    in_class,
                    "staticmethod" in decorators,
                    node,
                )
                found.setdefault(definition.qualname, []).append(definition)
                # Functions nested in a function body are local and cannot be called from Y.
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".", True)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for leaf in ast.walk(target):
                        if isinstance(leaf, ast.Name):
                            assigned.add(leaf.id)
            else:
                for nested in _compound_bodies(node):
                    visit(nested, prefix, in_class)

    visit(tree.body, "", False)

    leafs: dict[str, set[str]] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = prefix + child.name
                leafs.setdefault(child.name, set()).add(qualname)
                inner = qualname + (".<locals>." if not isinstance(child, ast.ClassDef) else ".")
                walk(child, inner)
            else:
                walk(child, prefix)

    walk(tree, "")
    callable_defs = {q: rows[0] for q, rows in found.items() if len(rows) == 1}
    duplicates = {q for q, rows in found.items() if len(rows) > 1}
    return FileDefinitions(callable_defs, duplicates, leafs, assigned, getattr_strings)


class DefinitionCache:
    """File definitions keyed by blob id."""

    def __init__(self, reader: GitReader) -> None:
        self.reader = reader
        self._rows: dict[str, FileDefinitions | None] = {}

    def get(self, blobs: dict[str, str], path: str) -> FileDefinitions | None:
        blob = blobs.get(path)
        if blob is None:
            return None
        if blob not in self._rows:
            text = self.reader.texts(blobs, [path]).get(path)
            self._rows[blob] = file_definitions(text) if text is not None else None
        return self._rows[blob]

    def leaf_counts(self, blobs: dict[str, str]) -> Counter[str]:
        """Distinct (path, qualname) definitions of each leaf name among production files."""
        counts: Counter[str] = Counter()
        for path in blobs:
            if not is_production(path):
                continue
            defs = self.get(blobs, path)
            if defs is not None:
                for leaf, qualnames in defs.defined_leafs.items():
                    counts[leaf] += len(qualnames)
        return counts

    def names_available(self, blobs: dict[str, str]) -> set[str]:
        names: set[str] = set()
        for path in blobs:
            if not is_production(path):
                continue
            defs = self.get(blobs, path)
            if defs is not None:
                names.update(defs.defined_leafs)
                names.update(defs.assigned_leafs)
        return names


@dataclass(frozen=True)
class ChangedDefinition:
    path: str
    qualname: str
    name: str
    change: str  # "signature" or "removed"
    old: Definition
    new: Definition | None
    shimmed: bool = False  # removed, but its file's module __getattr__ still names it

    def describe(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "qualname": self.qualname,
            "name": self.name,
            "change": self.change,
            "removed_behind_module_getattr": self.shimmed,
            "old_line": self.old.line,
            "new_line": self.new.line if self.new else None,
            "is_method": (self.new or self.old).is_method,
            "old_parameters": render_parameters(self.old.params),
            "new_parameters": render_parameters(self.new.params) if self.new else None,
            "old_parameter_list": [list(p) for p in self.old.params],
            "new_parameter_list": [list(p) for p in self.new.params] if self.new else None,
        }


def changed_definitions(
    defs: DefinitionCache, before: dict[str, str], after: dict[str, str], changed: list[str]
) -> tuple[list[ChangedDefinition], Counter[str]]:
    counts: Counter[str] = Counter()
    rows: list[ChangedDefinition] = []
    available_after: set[str] | None = None
    for path in changed:
        if not is_production(path) or path not in before:
            continue
        old_file = defs.get(before, path)
        if old_file is None:
            counts["definition_files_unparsable"] += 1
            continue
        new_file = defs.get(after, path) if path in after else None
        if path in after and new_file is None:
            counts["definition_files_unparsable"] += 1
            continue
        for qualname, old in old_file.callable_defs.items():
            if is_dunder(old.name):
                continue
            new = new_file.callable_defs.get(qualname) if new_file else None
            if new is not None:
                if new.params != old.params:
                    rows.append(ChangedDefinition(path, qualname, old.name, "signature", old, new))
                continue
            if new_file is not None and qualname in new_file.duplicates:
                counts["definitions_skipped_duplicate_qualname"] += 1
                continue
            if available_after is None:
                available_after = defs.names_available(after)
            if old.name not in available_after:
                shimmed = new_file is not None and old.qualname == old.name and old.name in new_file.getattr_strings
                rows.append(ChangedDefinition(path, qualname, old.name, "removed", old, None, shimmed))
    return rows, counts


# --------------------------------------------------------------------------- call sites


def changed_line_map(old_text: str, new_text: str) -> tuple[set[int], set[int]]:
    """Old-side lines (1-based) replaced or deleted, and boundaries after which lines were inserted."""
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    touched: set[int] = set()
    inserts: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            touched.update(range(i1 + 1, i2 + 1))
        elif tag == "insert":
            inserts.add(i1)  # inserted between old line i1 and old line i1 + 1
    return touched, inserts


def call_span(call: ast.Call) -> tuple[int, int]:
    """Lines from the callee's name to the closing parenthesis."""
    start = call.func.end_lineno if isinstance(call.func, ast.Attribute) else call.lineno
    return start or call.lineno, call.end_lineno or call.lineno


def callee_leaf(call: ast.Call) -> tuple[str | None, str | None]:
    if isinstance(call.func, ast.Name):
        return call.func.id, "name"
    if isinstance(call.func, ast.Attribute):
        return call.func.attr, "attribute"
    return None, None


def binding_stratum(change: ChangedDefinition, call: ast.Call, form: str) -> str:
    if change.change == "removed":
        return "removed_shimmed" if change.shimmed else "breaking_removed"
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(k.arg is None for k in call.keywords):
        return "unknown"
    assert change.new is not None
    kinds = {
        "positional_only": inspect.Parameter.POSITIONAL_ONLY,
        "positional_or_keyword": inspect.Parameter.POSITIONAL_OR_KEYWORD,
        "var_positional": inspect.Parameter.VAR_POSITIONAL,
        "keyword_only": inspect.Parameter.KEYWORD_ONLY,
        "var_keyword": inspect.Parameter.VAR_KEYWORD,
    }
    params = list(change.new.params)
    # `Class.method(self, x)` passes the instance explicitly, so nothing is dropped.
    # `other.method(self)` is a bound call that happens to pass self as an argument.
    first = call.args[0] if call.args else None
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    receiver_name = (
        receiver.id if isinstance(receiver, ast.Name) else receiver.attr if isinstance(receiver, ast.Attribute) else ""
    )
    class_like = receiver_name[:1].isupper() or isinstance(getattr(builtins, receiver_name, None), type)
    unbound = class_like and isinstance(first, ast.Name) and first.id in ("self", "cls")
    if change.new.is_method and form == "attribute" and not unbound and params and params[0][1] in (
        "positional_only",
        "positional_or_keyword",
    ):
        params = params[1:]
    try:
        signature = inspect.Signature(
            [
                inspect.Parameter(name, kinds[kind], default=None if has_default else inspect.Parameter.empty)
                for name, kind, has_default in params
            ]
        )
    except (ValueError, TypeError):
        return "unknown"
    try:
        signature.bind(*[None] * len(call.args), **{k.arg: None for k in call.keywords if k.arg})
    except TypeError:
        return "breaking"
    return "compatible"


def uniqueness_stratum(name: str, leaf_counts: Counter[str]) -> tuple[str, str]:
    count = leaf_counts.get(name, 0)
    if count != 1:
        return "ambiguous", "multiple_definitions" if count > 1 else "no_definition_in_parent"
    if name in BUILTIN_NAMES:
        return "ambiguous", "builtin_collision"
    return "unique", "single_definition"


@dataclass
class Site:
    change: ChangedDefinition
    call: ast.Call
    form: str
    start: int
    end: int
    text: str
    rewritten: bool


def co_updated_sites(
    changes: list[ChangedDefinition], old_text: str, new_text: str
) -> tuple[list[Site], str | None]:
    try:
        old_tree = ast.parse(old_text)
        new_tree = ast.parse(new_text)
    except (SyntaxError, ValueError, RecursionError):
        return [], "unparsable"
    names = {change.name for change in changes}
    calls = []
    for node in ast.walk(old_tree):
        if isinstance(node, ast.Call):
            leaf, form = callee_leaf(node)
            if leaf in names:
                calls.append((node, leaf, form))
    if not calls:
        return [], None
    touched, inserts = changed_line_map(old_text, new_text)
    new_dumps = Counter(ast.dump(node) for node in ast.walk(new_tree) if isinstance(node, ast.Call))
    old_dumps = Counter(ast.dump(node) for node, _, _ in calls)
    sites = []
    for node, leaf, form in sorted(calls, key=lambda row: (row[0].lineno, row[0].col_offset)):
        start, end = call_span(node)
        if not (any(start <= line <= end for line in touched) or any(start <= b < end for b in inserts)):
            continue
        dump = ast.dump(node)
        text = ast.get_source_segment(old_text, node) or ""
        for change in changes:
            if change.name == leaf:
                sites.append(
                    Site(change, node, form, start, end, text[:CALL_TEXT_LIMIT], new_dumps[dump] < old_dumps[dump])
                )
    return sites, None


# --------------------------------------------------------------------------- scoring


class _MutatedView:
    """Presents one synthetic tree to replay_history.analyze under a pseudo commit id."""

    def __init__(self, reader: GitReader, key: str, blobs: dict[str, str]) -> None:
        self.reader, self.key, self.blobs = reader, key, blobs

    def python_blobs(self, commit: str) -> dict[str, str]:
        return self.blobs if commit == self.key else self.reader.python_blobs(commit)

    def texts(self, blobs: dict[str, str], paths: Any = None) -> dict[str, str]:
        return self.reader.texts(blobs, paths)


def new_findings(mutated_rows: list, complete_rows: list) -> list:
    remaining = Counter(site_key(f, a) for f, a in complete_rows)
    out = []
    for finding, anchor in mutated_rows:
        key = site_key(finding, anchor)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            out.append((finding, anchor))
    return out


def finding_row(finding: Any, anchor: str) -> dict[str, Any]:
    return {
        "detector": finding.detector,
        "symbol": finding.symbol,
        "path": finding.path,
        "line": finding.start_line,
        "anchor": anchor,
    }


def build_repository(repo: Path, limit: int | None, enable: tuple[str, ...] = ()) -> dict[str, Any]:
    reader = CorpusGitReader(repo)
    cache = FactCache()
    defs = DefinitionCache(reader)
    started = time.perf_counter()
    counts: Counter[str] = Counter()
    mutations: list[dict[str, Any]] = []
    parses_at_last_clear = 0
    try:
        commits = reader.commits()
        if limit:
            commits = commits[-limit:]
        for sha, parent, subject in commits:
            counts["commits_examined"] += 1
            before, after = reader.python_blobs(parent), reader.python_blobs(sha)
            changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
            if not any(is_production(p) for p in changed):
                continue
            counts["commits_changing_production_python"] += 1
            changes, skipped = changed_definitions(defs, before, after, changed)
            counts.update(skipped)
            if not changes:
                continue
            counts["commits_with_changed_definitions"] += 1
            counts["changed_definitions_signature"] += sum(c.change == "signature" for c in changes)
            counts["changed_definitions_removed"] += sum(c.change == "removed" for c in changes)

            stale_candidates = [
                p for p in changed if is_production(p) and p in before and p in after
            ]
            per_file: dict[str, list[Site]] = {}
            for path in stale_candidates:
                relevant = [c for c in changes if c.path != path]
                if not relevant:
                    continue
                texts_before = reader.texts(before, [path])
                texts_after = reader.texts(after, [path])
                if path not in texts_before or path not in texts_after:
                    continue
                sites, problem = co_updated_sites(relevant, texts_before[path], texts_after[path])
                if problem:
                    counts["stale_candidates_unparsable"] += 1
                if sites:
                    per_file[path] = sites
            if not per_file:
                continue
            counts["commits_with_mutations"] += 1
            leaf_counts = defs.leaf_counts(before)

            if cache.stats.parses - parses_at_last_clear > 4000:
                cache.clear()
                parses_at_last_clear = cache.stats.parses
            try:
                _, complete_rows = analyze(reader, cache, parent, sha, enable)
                complete_error = None
            except (RecursionError, ValueError) as exc:
                complete_rows, complete_error = [], type(exc).__name__
                counts["analysis_errors"] += 1

            for path, sites in per_file.items():
                mutated = dict(after)
                mutated[path] = before[path]
                key = f"mutation:{sha}:{path}"
                error = complete_error
                fresh: list = []
                if error is None:
                    try:
                        _, mutated_rows = analyze(_MutatedView(reader, key, mutated), cache, parent, key, enable)
                        fresh = new_findings(mutated_rows, complete_rows)
                    except (RecursionError, ValueError) as exc:
                        error = type(exc).__name__
                        counts["analysis_errors"] += 1
                stale_text = reader.texts(before, [path])[path]
                facts = cache.load(path, stale_text)
                in_file = [(f, a) for f, a in fresh if f.path == path]
                collateral = [(f, a) for f, a in fresh if f.path != path]
                stale_defs = defs.get(before, path)
                site_rows = []
                for site in sites:
                    uniqueness, reason = uniqueness_stratum(site.change.name, leaf_counts)
                    anchor = enclosing_function(facts, site.start)
                    hits = [
                        (f, a)
                        for f, a in in_file
                        if (f.start_line <= site.end and f.end_line >= site.start) or (anchor and a == anchor)
                    ]
                    site_rows.append(
                        {
                            "definition": site.change.describe(),
                            "line": site.start,
                            "end_line": site.end,
                            "call_line": site.call.lineno,
                            "call_text": site.text,
                            "call_form": site.form,
                            "call_rewritten_in_commit": site.rewritten,
                            "enclosing_function": anchor,
                            "binding": binding_stratum(site.change, site.call, site.form),
                            "name_uniqueness": uniqueness,
                            "uniqueness_reason": reason,
                            "definitions_of_name_in_parent": leaf_counts.get(site.change.name, 0),
                            "name_defined_in_stale_file": bool(
                                stale_defs and site.change.name in stale_defs.defined_leafs
                            ),
                            "site_hit": bool(hits) and error is None,
                            "file_hit": bool(in_file) and error is None,
                            "site_hit_detectors": sorted({f.detector for f, _ in hits}),
                            "site_hit_symbols": sorted({f.symbol for f, _ in hits}),
                            "file_hit_detectors": sorted({f.detector for f, _ in in_file}),
                        }
                    )
                counts["mutations"] += 1
                counts["sites"] += len(site_rows)
                mutations.append(
                    {
                        "id": f"{repo.name}:{sha[:12]}:{path}",
                        "repository": repo.name,
                        "commit": sha,
                        "parent": parent,
                        "subject": subject[:120],
                        "stale_path": path,
                        "stale_blob": before[path],
                        "commit_blob": after[path],
                        "other_changed_python_paths": [p for p in changed if p != path],
                        "analysis_error": error,
                        "sites": site_rows,
                        "new_findings_in_stale_file": [finding_row(f, a) for f, a in in_file],
                        "collateral": [finding_row(f, a) for f, a in collateral],
                    }
                )
        return {
            "schema": SCHEMA,
            "materialization": MATERIALIZATION,
            "repository": repo.name,
            "head": commits[-1][0] if commits else None,
            "limit": limit,
            "opt_in_detectors": sorted(enable),
            "wall_seconds": round(time.perf_counter() - started, 1),
            "counts": dict(sorted(counts.items())),
            "mutations": mutations,
        }
    finally:
        reader.close()


# --------------------------------------------------------------------------- summary


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 3) if total else None


def _cell(sites: list[dict[str, Any]], mutation_hits: dict[str, tuple[bool, bool]]) -> dict[str, Any]:
    by_mutation: dict[str, list[dict[str, Any]]] = {}
    for site in sites:
        by_mutation.setdefault(site["_mutation"], []).append(site)
    site_hits = sum(s["site_hit"] for s in sites)
    file_hits = sum(s["file_hit"] for s in sites)
    m_site = sum(any(s["site_hit"] for s in rows) for rows in by_mutation.values())
    m_file = sum(mutation_hits[m][1] for m in by_mutation)
    by_commit: dict[str, bool] = {}
    for site in sites:
        by_commit[site["_commit"]] = by_commit.get(site["_commit"], False) or site["site_hit"]
    return {
        "commits": len(by_commit),
        "commits_site_hit": sum(by_commit.values()),
        "sites": len(sites),
        "site_hits": site_hits,
        "file_hits": file_hits,
        "site_recall": _rate(site_hits, len(sites)),
        "file_recall": _rate(file_hits, len(sites)),
        "mutations": len(by_mutation),
        "mutations_site_hit": m_site,
        "mutations_file_hit": m_file,
        "mutation_site_recall": _rate(m_site, len(by_mutation)),
        "mutation_file_recall": _rate(m_file, len(by_mutation)),
    }


def summarize_corpus(mutations: list[dict[str, Any]]) -> dict[str, Any]:
    sites: list[dict[str, Any]] = []
    mutation_hits: dict[str, tuple[bool, bool]] = {}
    for mutation in mutations:
        if mutation["analysis_error"]:
            continue
        mutation_hits[mutation["id"]] = (
            any(s["site_hit"] for s in mutation["sites"]),
            bool(mutation["new_findings_in_stale_file"]),
        )
        for site in mutation["sites"]:
            sites.append({**site, "_mutation": mutation["id"], "_commit": f"{mutation['repository']}:{mutation['commit']}"})
    strata = {}
    for binding in BINDINGS:
        for uniqueness in UNIQUENESS:
            rows = [s for s in sites if s["binding"] == binding and s["name_uniqueness"] == uniqueness]
            strata[f"{binding} x {uniqueness}"] = _cell(rows, mutation_hits)
    marginals = {}
    for binding in BINDINGS:
        marginals[f"{binding} x any"] = _cell([s for s in sites if s["binding"] == binding], mutation_hits)
    for uniqueness in UNIQUENESS:
        marginals[f"any x {uniqueness}"] = _cell([s for s in sites if s["name_uniqueness"] == uniqueness], mutation_hits)
    breaking = {"breaking", "breaking_removed"}
    marginals["breaking_or_removed x unique"] = _cell(
        [s for s in sites if s["binding"] in breaking and s["name_uniqueness"] == "unique"], mutation_hits
    )
    marginals["breaking_or_removed x unique, call rewritten"] = _cell(
        [
            s
            for s in sites
            if s["binding"] in breaking and s["name_uniqueness"] == "unique" and s["call_rewritten_in_commit"]
        ],
        mutation_hits,
    )
    marginals["all"] = _cell(sites, mutation_hits)
    collateral = sum(len(m["collateral"]) for m in mutations if not m["analysis_error"])
    return {
        "mutations_scored": len(mutation_hits),
        "mutations_with_analysis_error": sum(1 for m in mutations if m["analysis_error"]),
        "sites_scored": len(sites),
        "sites_call_rewritten_in_commit": sum(s["call_rewritten_in_commit"] for s in sites),
        "uniqueness_reasons": dict(Counter(s["uniqueness_reason"] for s in sites)),
        "collateral_findings": collateral,
        "mutations_with_collateral": sum(1 for m in mutations if m["collateral"] and not m["analysis_error"]),
        "strata": strata,
        "marginals": marginals,
    }


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    per_repository = {}
    pooled_counts: Counter[str] = Counter()
    pooled_mutations: list[dict[str, Any]] = []
    for report in reports:
        per_repository[report["repository"]] = {
            "counts": report["counts"],
            "wall_seconds": report["wall_seconds"],
            **summarize_corpus(report["mutations"]),
        }
        pooled_counts.update(report["counts"])
        pooled_mutations.extend(report["mutations"])
    return {
        "schema": SCHEMA,
        "opt_in_detectors": reports[0]["opt_in_detectors"] if reports else [],
        "per_repository": per_repository,
        "pooled": {"counts": dict(sorted(pooled_counts.items())), **summarize_corpus(pooled_mutations)},
    }


def _fmt(rate: float | None) -> str:
    return "  -  " if rate is None else f"{rate:5.3f}"


def print_summary(summary: dict[str, Any], stream: Any = None) -> None:
    stream = stream or sys.stdout
    sections = [*summary["per_repository"].items(), ("POOLED", summary["pooled"])]
    for name, section in sections:
        counts = section["counts"]
        print(
            f"\n== {name}: {counts.get('commits_examined', 0)} commits, "
            f"{counts.get('commits_with_mutations', 0)} with mutations, "
            f"{section['mutations_scored']} mutations, {section['sites_scored']} sites, "
            f"{section['collateral_findings']} collateral findings in "
            f"{section['mutations_with_collateral']} mutations",
            file=stream,
        )
        print(
            f"{'stratum':48} {'sites':>5} {'site':>5} {'file':>5} {'s.rec':>5} {'f.rec':>5} "
            f"{'muts':>5} {'m.s.rec':>7} {'m.f.rec':>7} {'cmts':>5} {'c.hit':>5}",
            file=stream,
        )
        for label, cell in [*section["strata"].items(), *section["marginals"].items()]:
            if not cell["sites"] and " x any" not in label and label != "all":
                continue
            print(
                f"{label:48} {cell['sites']:5d} {cell['site_hits']:5d} {cell['file_hits']:5d} "
                f"{_fmt(cell['site_recall'])} {_fmt(cell['file_recall'])} {cell['mutations']:5d} "
                f"  {_fmt(cell['mutation_site_recall'])}   {_fmt(cell['mutation_file_recall'])} "
                f"{cell['commits']:5d} {cell['commits_site_hit']:5d}",
                file=stream,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, action="append", required=True, help="Git repository (repeatable)")
    parser.add_argument("--limit", type=int, default=None, help="Use only the most recent N non-merge commits")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument(
        "--enable",
        action="append",
        choices=sorted(OPT_IN_DETECTORS),
        default=[],
        help="Also run an opt-in detector (repeatable)",
    )
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    reports = []
    for repo in args.repo:
        report = build_repository(repo.resolve(), args.limit, tuple(args.enable))
        target = args.out / report["repository"]
        target.mkdir(parents=True, exist_ok=True)
        (target / "corpus.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        reports.append(report)
        counts = report["counts"]
        print(
            f"{report['repository']}: {counts.get('commits_examined', 0)} commits, "
            f"{counts.get('mutations', 0)} mutations, {counts.get('sites', 0)} sites, {report['wall_seconds']} s",
            file=sys.stderr,
        )
    summary = summarize(reports)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
