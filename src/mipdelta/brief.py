"""Compile one bounded source brief from a Python snapshot and a task ticket."""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

from .facts import parse_file
from .model import FunctionFact
from .resolve import build_index, resolve_call_with_provenance


ARMS = ("none", "map", "lod")
DEFAULT_MAX_BYTES = 1024
MAX_BRIDGE_CALLEES = 2
MAX_SIBLING_CALLERS = 4
_WORDS = re.compile(r"[a-z0-9]{3,}")


@dataclass(frozen=True)
class Span:
    path: str
    start: int
    end: int
    label: str

    def render(self, sources: dict[str, list[str]]) -> str:
        lines = sources[self.path][self.start - 1:self.end]
        body = "\n".join(f"{number}: {line}" for number, line in enumerate(lines, self.start))
        return f"{self.path}:{self.start}-{self.end} [{self.label}]\n{body}\n"


def _words(text: str) -> set[str]:
    return set(_WORDS.findall(text.lower()))


def _key(fn: FunctionFact) -> tuple[str, str]:
    return fn.path, fn.qualname


def _rank(fn: FunctionFact, lines: dict[str, list[str]], terms: set[str]) -> tuple:
    name_hits = len(_words(fn.qualname) & terms)
    path_hits = len(_words(fn.path) & terms)
    body_hits = len(_words("\n".join(lines[fn.path][fn.line - 1:fn.end_line])) & terms)
    return -name_hits, -path_hits, -body_hits, fn.path, fn.line, fn.qualname


def _map_spans(ranked: list[FunctionFact], lines: dict[str, list[str]], terms: set[str]):
    # Each entry shows a declaration and, where relevant, a literal context window.
    for fn in ranked:
        yield Span(fn.path, fn.line, fn.line, "declaration")
        hits = [
            i for i in range(fn.line + 1, fn.end_line + 1)
            if _words(lines[fn.path][i - 1]) & terms
        ]
        if hits:
            line = min(hits, key=lambda i: (-len(_words(lines[fn.path][i - 1]) & terms), i))
            yield Span(fn.path, max(fn.line + 1, line - 1), min(fn.end_line, line + 1), "literal match")


def _related_spans(fn: FunctionFact, relation: str, line: int):
    yield Span(fn.path, fn.line, fn.end_line, f"L3 {relation} at line {line}")
    if fn.end_line > fn.line:
        start, end = max(fn.line, line - 1), min(fn.end_line, line + 1)
        yield Span(fn.path, start, end, f"L3 {relation} window")


def _lod_spans(ranked: list[FunctionFact], facts):
    if not ranked:
        return
    index = build_index(facts)
    seed = ranked[0]
    yield Span(seed.path, seed.line, seed.end_line, "L3 task seed")
    positions = {_key(fn): position for position, fn in enumerate(ranked)}
    functions = {_key(fn): fn for fn in ranked}
    seed_key = _key(seed)
    incoming = {}
    callees = set()
    for path in sorted(facts):
        for call in facts[path].calls:
            owner = path, call.owner_function
            if owner not in functions:
                continue
            resolved = resolve_call_with_provenance(call, facts[path], index)
            if len(resolved) != 1:
                continue
            target, provenance = resolved[0]
            target_key = _key(target)
            if provenance != "static_ast" or target_key == owner or target_key not in functions:
                continue
            incoming.setdefault(target_key, {})[owner] = call.line
            if owner == seed_key:
                callees.add(target_key)

    direct = {key: ("static caller", line) for key, line in incoming.get(seed_key, {}).items()}
    for key in callees:
        direct.setdefault(key, ("static callee", functions[key].line))
    for key in sorted(direct, key=positions.__getitem__):
        relation, line = direct[key]
        yield from _related_spans(functions[key], relation, line)

    # A seed wrapper can lead to other callers of the same resolved callee.
    # This is one outgoing edge and one incoming edge, with no recursive expansion.
    seen = {seed_key, *direct}
    bridges = sorted(callees, key=positions.__getitem__)[:MAX_BRIDGE_CALLEES]
    siblings = {}
    for bridge in bridges:
        for key, line in incoming.get(bridge, {}).items():
            if key not in seen:
                siblings.setdefault(key, (bridge, line))
    for key in sorted(siblings, key=positions.__getitem__)[:MAX_SIBLING_CALLERS]:
        bridge, line = siblings[key]
        callee = functions[bridge]
        yield from _related_spans(functions[key], f"sibling via {callee.path}:{callee.line}", line)

    for fn in ranked[1:]:
        yield Span(fn.path, fn.line, fn.line, "L2 declaration")


def compile_brief(
    files: dict[str, str], ticket: str, arm: str, max_bytes: int = DEFAULT_MAX_BYTES
) -> tuple[str, tuple[Span, ...]]:
    """Return the exact briefing and the source spans physically included in it."""
    if arm not in ARMS or max_bytes < 0:
        raise ValueError("unknown arm or negative byte allowance")
    if arm == "none" or not max_bytes:
        return "", ()
    facts = {path: parse_file(path, source) for path, source in sorted(files.items())}
    if any(file.parse_error for file in facts.values()):
        raise ValueError("snapshot contains unparseable Python source")
    lines = {path: text.splitlines() for path, text in files.items()}
    terms = _words(ticket)
    ranked = sorted(
        (fn for file in facts.values() for fn in file.functions.values()),
        key=lambda fn: _rank(fn, lines, terms),
    )
    spans = _map_spans(ranked, lines, terms) if arm == "map" else _lod_spans(ranked, facts)
    header = "Initial source excerpts. Verify in the checkout. Coverage is partial.\n\n"
    used = len(header.encode("utf-8"))
    covered = set()
    blocks = []
    emitted = []
    for span in spans:
        physical_lines = {(span.path, line) for line in range(span.start, span.end + 1)}
        if physical_lines <= covered:
            continue
        block = span.render(lines)
        size = len(block.encode("utf-8"))
        if used + size > max_bytes:
            continue
        blocks.append(block)
        emitted.append(span)
        covered.update(physical_lines)
        used += size
    return (header + "".join(blocks), tuple(emitted)) if blocks else ("", ())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--ticket", type=Path, required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    if args.max_bytes < 0:
        parser.error("--max-bytes must be nonnegative")
    if args.arm == "none" or args.max_bytes == 0:
        return
    files = {
        path.relative_to(args.root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(args.root.rglob("*.py"))
        if path.is_file() and not path.is_symlink()
        and all(not part.startswith(".") and part not in {"venv", "__pycache__", "build", "dist"}
                for part in path.relative_to(args.root).parts)
    }
    brief, _ = compile_brief(files, args.ticket.read_text(encoding="utf-8"), args.arm, args.max_bytes)
    import sys
    sys.stdout.buffer.write(brief.encode("utf-8"))


if __name__ == "__main__":
    main()
