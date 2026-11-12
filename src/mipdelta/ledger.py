"""Open review obligations across a sequence of edits.

A contract-changing edit can leave work at other sites: a caller to update, a wrapper
that should forward a new parameter, a handler that no longer covers a raised exception.
The ledger keeps those sites open until a later edit resolves them, the change is
reverted, or the obligation is waived as intentional.

Identity is independent of line numbers, so edits above a site do not reopen it.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from typing import Any, Callable, Iterable, Mapping

from .model import FileFacts, Finding

_PROVENANCE_TAG = {
    "observed_runtime": "rt-obs",
    "static_ast": "static",
    "static_type_candidate": "type-cand",
}


@dataclass(frozen=True)
class Obligation:
    id: str
    anchor: str
    finding: Finding
    waived: str | None = None


@dataclass(frozen=True)
class LedgerUpdate:
    opened: tuple[Obligation, ...]
    discharged: tuple[Obligation, ...]
    open: tuple[Obligation, ...]


def enclosing_function(facts: FileFacts | None, line: int) -> str:
    """Qualified name of the innermost function containing a line, or an empty string."""
    if facts is None:
        return ""
    best = ""
    best_span = None
    for function in facts.functions.values():
        if function.line <= line <= function.end_line:
            span = function.end_line - function.line
            if best_span is None or span < best_span:
                best, best_span = function.qualname, span
    return best


def _obligation_id(finding: Finding, anchor: str, ordinal: int) -> str:
    key = "\x1f".join((finding.detector, finding.symbol, finding.path, anchor, str(ordinal)))
    return sha1(key.encode("utf-8")).hexdigest()[:8]


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        "detector": finding.detector,
        "symbol": finding.symbol,
        "path": finding.path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "confidence": finding.confidence,
        "evidence": list(finding.evidence),
        "resolution_source": finding.resolution_source,
    }


def _finding_from_dict(row: Mapping[str, Any]) -> Finding:
    return Finding(
        str(row["detector"]),
        str(row["symbol"]),
        str(row["path"]),
        int(row["start_line"]),
        int(row["end_line"]),
        str(row.get("confidence", "candidate")),
        tuple(str(item) for item in row.get("evidence", ())),
        str(row.get("resolution_source", "static_ast")),
    )


class ObligationLedger:
    """Tracks which findings are new, still open, discharged, or waived."""

    def __init__(self) -> None:
        self._open: dict[str, Obligation] = {}
        self._waived: dict[str, str] = {}
        self._reported_at_stop: set[str] = set()

    def update(
        self,
        findings: Iterable[Finding],
        anchor_for: Callable[[Finding], str] = lambda finding: "",
    ) -> LedgerUpdate:
        """Replace the open set with the current findings and report what changed."""
        groups: dict[tuple[str, str, str, str], list[Finding]] = {}
        for finding in findings:
            anchor = anchor_for(finding)
            groups.setdefault((finding.detector, finding.symbol, finding.path, anchor), []).append(finding)
        current: dict[str, Obligation] = {}
        for (_, _, _, anchor), rows in groups.items():
            # Several identical sites in one function are told apart by their order.
            for ordinal, finding in enumerate(sorted(rows, key=lambda row: (row.start_line, row.end_line))):
                identifier = _obligation_id(finding, anchor, ordinal)
                current.setdefault(
                    identifier, Obligation(identifier, anchor, finding, self._waived.get(identifier))
                )
        opened = tuple(
            item for key, item in current.items() if key not in self._open and item.waived is None
        )
        discharged = tuple(
            item for key, item in self._open.items() if key not in current and item.waived is None
        )
        self._open = current
        self._reported_at_stop &= set(current)
        return LedgerUpdate(opened, discharged, self.open())

    def open(self) -> tuple[Obligation, ...]:
        return tuple(
            sorted(
                (item for item in self._open.values() if item.waived is None),
                key=lambda item: (item.finding.path, item.finding.start_line, item.finding.detector, item.id),
            )
        )

    def waived(self) -> tuple[Obligation, ...]:
        return tuple(item for item in self._open.values() if item.waived is not None)

    def waive(self, identifier: str, reason: str = "") -> bool:
        """Mark one open obligation as intentional. Returns False for an unknown id."""
        item = self._open.get(identifier)
        if item is None:
            return False
        note = reason or "waived"
        self._waived[identifier] = note
        self._open[identifier] = Obligation(item.id, item.anchor, item.finding, note)
        return True

    def unreported_at_stop(self) -> tuple[Obligation, ...]:
        """Open obligations that have not yet been listed in a completion check."""
        return tuple(item for item in self.open() if item.id not in self._reported_at_stop)

    def mark_reported_at_stop(self, obligations: Iterable[Obligation]) -> None:
        self._reported_at_stop.update(item.id for item in obligations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "mipdelta.ledger.v1",
            "open": [
                {"id": item.id, "anchor": item.anchor, "finding": _finding_to_dict(item.finding)}
                for item in self._open.values()
            ],
            "waived": dict(sorted(self._waived.items())),
            "reported_at_stop": sorted(self._reported_at_stop),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObligationLedger":
        ledger = cls()
        if data.get("schema") != "mipdelta.ledger.v1":
            return ledger
        ledger._waived = {str(key): str(value) for key, value in dict(data.get("waived", {})).items()}
        for row in data.get("open", ()):
            identifier = str(row["id"])
            ledger._open[identifier] = Obligation(
                identifier,
                str(row.get("anchor", "")),
                _finding_from_dict(row["finding"]),
                ledger._waived.get(identifier),
            )
        ledger._reported_at_stop = {str(item) for item in data.get("reported_at_stop", ())} & set(ledger._open)
        return ledger


def format_open_obligations(
    obligations: Iterable[Obligation], *, max_bytes: int = 600, show_ids: bool = False
) -> str:
    """List open obligations within a byte budget, or return an empty string when none are open.

    Sites that do not fit are summarized as a count per file, so the total is always stated.
    """
    rows = list(obligations)
    if not rows:
        return ""
    header = f"[mip-delta] {len(rows)} open:"
    items: list[str] = []
    for index, item in enumerate(rows):
        finding = item.finding
        tag = _PROVENANCE_TAG.get(finding.resolution_source, "cand")
        text = f"{finding.path}:{finding.start_line} {finding.detector}[{tag}] {finding.symbol}"
        if show_ids:
            text += f" #{item.id}"
        rest = rows[index + 1:]
        tail = _rollup(rest)
        candidate = header + " " + "; ".join([*items, text]) + tail
        if len(candidate.encode("utf-8")) > max_bytes and items:
            break
        items.append(text)
    out = header + " " + "; ".join(items) + _rollup(rows[len(items):])
    if len(out.encode("utf-8")) > max_bytes:
        out = out.encode("utf-8")[: max(0, max_bytes - 3)].decode("utf-8", errors="ignore") + "..."
    return out


def _rollup(rest: list[Obligation]) -> str:
    if not rest:
        return ""
    per_file: dict[str, int] = {}
    for item in rest:
        per_file[item.finding.path] = per_file.get(item.finding.path, 0) + 1
    ranked = sorted(per_file.items(), key=lambda pair: (-pair[1], pair[0]))
    shown = ", ".join(f"{path} x{count}" for path, count in ranked[:3])
    more = f", +{len(ranked) - 3} files" if len(ranked) > 3 else ""
    return f" (+{len(rest)}: {shown}{more})"
