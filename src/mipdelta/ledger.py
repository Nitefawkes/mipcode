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

from .delta import finding_rank_key
from .model import FileFacts, Finding

_PROVENANCE_TAG = {
    "observed_runtime": "rt-obs",
    "static_ast": "static",
    "static_type_candidate": "type-cand",
}

STALLED_WRITE_THRESHOLD = 3


@dataclass(frozen=True)
class Obligation:
    id: str
    anchor: str
    finding: Finding
    waived: str | None = None
    reopen_count: int = 0


@dataclass(frozen=True)
class LedgerUpdate:
    opened: tuple[Obligation, ...]
    discharged: tuple[Obligation, ...]
    open: tuple[Obligation, ...]
    reopened: tuple[Obligation, ...] = ()
    stalled_writes: int = 0
    thrashing: bool = False


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
    """Tracks which findings are new, still open, discharged, waived, or oscillating."""

    def __init__(self) -> None:
        self._open: dict[str, Obligation] = {}
        self._waived: dict[str, str] = {}
        self._reported_at_stop: set[str] = set()
        self._ever_discharged: set[str] = set()
        self._reopen_counts: dict[str, int] = {}
        self._stalled_writes: int = 0
        self._last_touched_path: str | None = None

    def update(
        self,
        findings: Iterable[Finding],
        anchor_for: Callable[[Finding], str] = lambda finding: "",
        *,
        edited_path: str | None = None,
    ) -> LedgerUpdate:
        """Replace the open set with the current findings and report what changed."""
        prev_open_count = sum(1 for item in self._open.values() if item.waived is None)
        groups: dict[tuple[str, str, str, str], list[Finding]] = {}
        for finding in findings:
            anchor = anchor_for(finding)
            groups.setdefault((finding.detector, finding.symbol, finding.path, anchor), []).append(finding)
        current: dict[str, Obligation] = {}
        for (_, _, _, anchor), rows in groups.items():
            # Several identical sites in one function are told apart by their order.
            for ordinal, finding in enumerate(sorted(rows, key=lambda row: (row.start_line, row.end_line))):
                identifier = _obligation_id(finding, anchor, ordinal)
                if identifier in current:
                    continue
                waived = self._waived.get(identifier)
                reopen_count = self._reopen_counts.get(identifier, 0)
                if identifier not in self._open and waived is None and identifier in self._ever_discharged:
                    reopen_count += 1
                    self._reopen_counts[identifier] = reopen_count
                current[identifier] = Obligation(identifier, anchor, finding, waived, reopen_count)
        opened = tuple(
            item for key, item in current.items() if key not in self._open and item.waived is None
        )
        discharged = tuple(
            item for key, item in self._open.items() if key not in current and item.waived is None
        )
        for item in discharged:
            self._ever_discharged.add(item.id)
        reopened = tuple(item for item in opened if item.reopen_count >= 1)

        self._open = current
        self._reported_at_stop &= set(current)
        now_open = self.open()
        now_open_count = len(now_open)

        if now_open_count == 0 or prev_open_count == 0:
            self._stalled_writes = 0
            self._last_touched_path = edited_path if (now_open_count > 0 and edited_path) else None
        elif edited_path:
            if discharged or now_open_count < prev_open_count:
                self._stalled_writes = 0
                self._last_touched_path = edited_path
            elif edited_path == self._last_touched_path:
                self._stalled_writes += 1
            else:
                self._stalled_writes = 1
                self._last_touched_path = edited_path

        thrashing = bool(reopened) or (now_open_count > 0 and self._stalled_writes >= STALLED_WRITE_THRESHOLD)
        return LedgerUpdate(
            opened,
            discharged,
            now_open,
            reopened=reopened,
            stalled_writes=self._stalled_writes,
            thrashing=thrashing,
        )

    def open(self) -> tuple[Obligation, ...]:
        return tuple(
            sorted(
                (item for item in self._open.values() if item.waived is None),
                key=lambda item: (*finding_rank_key(item.finding), item.id),
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
        self._open[identifier] = Obligation(item.id, item.anchor, item.finding, note, item.reopen_count)
        return True

    def unreported_at_stop(self) -> tuple[Obligation, ...]:
        """Open obligations that have not yet been listed in a completion check."""
        return tuple(item for item in self.open() if item.id not in self._reported_at_stop)

    def mark_reported_at_stop(self, obligations: Iterable[Obligation]) -> None:
        self._reported_at_stop.update(item.id for item in obligations)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "mipdelta.ledger.v1",
            "open": [
                {"id": item.id, "anchor": item.anchor, "finding": _finding_to_dict(item.finding)}
                for item in self._open.values()
            ],
            "waived": dict(sorted(self._waived.items())),
            "reported_at_stop": sorted(self._reported_at_stop),
        }
        if self._ever_discharged:
            payload["ever_discharged"] = sorted(self._ever_discharged)
        if self._reopen_counts:
            payload["reopen_counts"] = dict(sorted(self._reopen_counts.items()))
        if self._stalled_writes > 0:
            payload["stalled_writes"] = self._stalled_writes
        if self._last_touched_path:
            payload["last_touched_path"] = self._last_touched_path
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObligationLedger":
        ledger = cls()
        if data.get("schema") != "mipdelta.ledger.v1":
            return ledger
        ledger._waived = {str(key): str(value) for key, value in dict(data.get("waived", {})).items()}
        ledger._ever_discharged = {str(item) for item in data.get("ever_discharged", ())}
        ledger._reopen_counts = {
            str(key): max(0, int(value))
            for key, value in dict(data.get("reopen_counts", {})).items()
        }
        ledger._stalled_writes = max(0, int(data.get("stalled_writes", 0) or 0))
        last_path = data.get("last_touched_path")
        ledger._last_touched_path = str(last_path) if isinstance(last_path, str) and last_path else None
        for row in data.get("open", ()):
            identifier = str(row["id"])
            ledger._open[identifier] = Obligation(
                identifier,
                str(row.get("anchor", "")),
                _finding_from_dict(row["finding"]),
                ledger._waived.get(identifier),
                ledger._reopen_counts.get(identifier, 0),
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


def format_thrash_advisory(
    update: LedgerUpdate,
    *,
    edited_path: str | None = None,
    max_bytes: int = 600,
    show_ids: bool = False,
) -> str:
    """Format a widened advisory when obligations oscillate or consecutive writes stall."""
    if not update.thrashing or not update.open:
        return ""
    if update.reopened:
        max_reopen = max(item.reopen_count for item in update.reopened)
        lead = update.reopened[0]
        scope = lead.anchor or lead.finding.symbol
        header = (
            f"[mip-delta:thrash] oscillating (reopened {max_reopen}x in {scope}); "
            f"{len(update.open)} open:"
        )
        reopened_ids = {item.id for item in update.reopened}
        rows = [*update.reopened, *(item for item in update.open if item.id not in reopened_ids)]
    else:
        target = edited_path or "file"
        header = (
            f"[mip-delta:thrash] stalled ({update.stalled_writes} writes to {target} without reducing "
            f"{len(update.open)} open):"
        )
        rows = list(update.open)
    items: list[str] = []
    for index, item in enumerate(rows):
        finding = item.finding
        tag = _PROVENANCE_TAG.get(finding.resolution_source, "cand")
        anchor_note = f" in {item.anchor}" if item.anchor and item.anchor != finding.symbol else ""
        text = f"{finding.path}:{finding.start_line} {finding.detector}[{tag}] {finding.symbol}{anchor_note}"
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
