from __future__ import annotations

import hashlib
import time
from typing import Iterable, Mapping, Sequence

from .cache import FactCache
from .delta import analyze_changed_files_in_index, format_advisory, rank_findings
from .model import (
    CustodyReport,
    FileFacts,
    Finding,
    ObservedCallEdge,
    SourceFile,
    WriteDeltaResult,
)
from .resolve import MutableResolutionIndex
from .stubs import parse_stub_tree


def _finding_key(finding: Finding) -> tuple[str, str, str, int, str]:
    return (
        finding.detector,
        finding.symbol,
        finding.path,
        finding.start_line,
        finding.resolution_source,
    )


class DeltaSession:
    """Stateful, content-addressed per-session workspace tracker for incremental write_file analysis.

    - Warm-up (`from_workspace`) parses the initial package once and records exact SHA-256 digests.
    - `apply_write(path, new_text)` parses ONLY the written file, updates `MutableResolutionIndex`
      in O(|file|), short-circuits cross-file checks when signatures are unchanged, and emits a
      bounded advisory string.
    - `verify_custody(disk_files)` performs an explicit SHA-256 custody check against unrecorded
      external mutations without relying on (mtime_ns, size).
    """

    def __init__(
        self,
        *,
        cache: FactCache | None = None,
        stub_files: Mapping[str, FileFacts] | None = None,
        runtime_edges: Iterable[ObservedCallEdge] | None = None,
        max_advisory_bytes: int = 250,
    ) -> None:
        self.cache = cache if cache is not None else FactCache()
        self.index = MutableResolutionIndex(stub_files=stub_files, runtime_edges=runtime_edges)
        self._baseline_digests: dict[str, str] = {}
        self._baseline_facts: dict[str, FileFacts] = {}
        self._active_digests: dict[str, str] = {}
        self._active_facts: dict[str, FileFacts] = {}
        self._emitted_finding_keys: set[tuple[str, str, str, int, str]] = set()
        self.max_advisory_bytes = max_advisory_bytes
        self.warmup_cpu_seconds: float = 0.0
        self.warmup_wall_seconds: float = 0.0

    @classmethod
    def from_workspace(
        cls,
        files: Mapping[str, str],
        *,
        cache: FactCache | None = None,
        stub_texts: Mapping[str, str] | None = None,
        runtime_edges: Iterable[ObservedCallEdge] | None = None,
        max_advisory_bytes: int = 250,
    ) -> "DeltaSession":
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        parsed_stubs = parse_stub_tree(stub_texts) if stub_texts else None
        session = cls(
            cache=cache,
            stub_files=parsed_stubs,
            runtime_edges=runtime_edges,
            max_advisory_bytes=max_advisory_bytes,
        )
        for raw_path in sorted(files):
            norm = session.cache.normalize_path(raw_path)
            source = SourceFile.from_text(norm, files[raw_path])
            facts = session.cache.load_source(source)
            session._baseline_digests[norm] = source.sha256
            session._baseline_facts[norm] = facts
            session._active_digests[norm] = source.sha256
            session._active_facts[norm] = facts
            session.index.add_file(facts)
        _ = session.index.type_index
        session.warmup_cpu_seconds = time.process_time() - cpu_start
        session.warmup_wall_seconds = time.perf_counter() - wall_start
        return session

    def digest_for(self, path: str) -> str | None:
        return self._active_digests.get(self.cache.normalize_path(path))

    def add_runtime_edges(self, edges: Iterable[ObservedCallEdge]) -> None:
        self.index.add_runtime_edges(edges)

    def verify_custody(self, current_files: Mapping[str, bytes | str]) -> CustodyReport:
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        normalized_current: dict[str, str] = {}
        for raw_path, val in current_files.items():
            norm = self.cache.normalize_path(raw_path)
            data = val.encode("utf-8") if isinstance(val, str) else val
            normalized_current[norm] = hashlib.sha256(data).hexdigest()

        expected_paths = set(self._active_digests)
        actual_paths = set(normalized_current)
        missing = tuple(sorted(expected_paths - actual_paths))
        unexpected = tuple(sorted(actual_paths - expected_paths))
        modified = tuple(
            sorted(
                p
                for p in (expected_paths & actual_paths)
                if normalized_current[p] != self._active_digests[p]
            )
        )
        clean = not missing and not unexpected and not modified
        return CustodyReport(
            clean=clean,
            checked_files=len(actual_paths),
            modified_externally=modified,
            missing_files=missing,
            unexpected_files=unexpected,
            cpu_seconds=time.process_time() - cpu_start,
            wall_seconds=time.perf_counter() - wall_start,
        )

    def apply_write(
        self,
        path: str,
        new_text: str,
        *,
        legacy_optional_keyword_all_callers: bool = False,
    ) -> WriteDeltaResult:
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        norm = self.cache.normalize_path(path)
        source = SourceFile.from_text(norm, new_text)
        old_sha = self._active_digests.get(norm)
        old_facts = self._active_facts.get(norm)

        if old_sha == source.sha256 and old_facts is not None:
            return WriteDeltaResult(
                path=norm,
                before_sha256=old_sha,
                after_sha256=source.sha256,
                content_changed=False,
                cross_file_triggered=False,
                parses_executed=0,
                candidate_calls_inspected=0,
                findings=(),
                ranked_findings=(),
                advisory_text="",
                advisory_utf8_bytes=0,
                cpu_seconds=time.process_time() - cpu_start,
                wall_seconds=time.perf_counter() - wall_start,
            )

        parses_before = self.cache.stats.parses
        new_facts = self.cache.load_source(source, prior_facts=old_facts)
        parses_executed = self.cache.stats.parses - parses_before

        self._active_digests[norm] = source.sha256
        self._active_facts[norm] = new_facts
        self.index.add_file(new_facts)

        findings, cross_triggered, calls_inspected = analyze_changed_files_in_index(
            {norm: old_facts},
            {norm: new_facts},
            self.index,
            legacy_optional_keyword_all_callers=legacy_optional_keyword_all_callers,
        )
        ranked = rank_findings(findings)
        self._emitted_finding_keys.update(_finding_key(f) for f in ranked)
        advisory = format_advisory(ranked, max_bytes=self.max_advisory_bytes)
        cpu_elapsed = time.process_time() - cpu_start
        wall_elapsed = time.perf_counter() - wall_start

        return WriteDeltaResult(
            path=norm,
            before_sha256=old_sha,
            after_sha256=source.sha256,
            content_changed=True,
            cross_file_triggered=cross_triggered,
            parses_executed=parses_executed,
            candidate_calls_inspected=calls_inspected,
            findings=tuple(findings),
            ranked_findings=tuple(ranked),
            advisory_text=advisory,
            advisory_utf8_bytes=len(advisory.encode("utf-8")),
            cpu_seconds=cpu_elapsed,
            wall_seconds=wall_elapsed,
        )

    def evaluate_new_runtime_findings(
        self,
        *,
        legacy_optional_keyword_all_callers: bool = False,
    ) -> WriteDeltaResult | None:
        """Re-evaluate modified session files after new runtime call edges are ingested."""
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        changed_paths = [
            p
            for p in sorted(set(self._baseline_digests) | set(self._active_digests))
            if self._baseline_digests.get(p) != self._active_digests.get(p) and p in self._active_facts
        ]
        if not changed_paths:
            return None
        before_changed = {p: self._baseline_facts.get(p) for p in changed_paths}
        after_changed = {p: self._active_facts[p] for p in changed_paths}
        findings, cross_triggered, calls_inspected = analyze_changed_files_in_index(
            before_changed,
            after_changed,
            self.index,
            legacy_optional_keyword_all_callers=legacy_optional_keyword_all_callers,
        )
        new_findings = [f for f in findings if _finding_key(f) not in self._emitted_finding_keys]
        if not new_findings:
            return None
        ranked = rank_findings(new_findings)
        self._emitted_finding_keys.update(_finding_key(f) for f in ranked)
        advisory = format_advisory(ranked, max_bytes=self.max_advisory_bytes)
        last_path = changed_paths[-1]
        return WriteDeltaResult(
            path=",".join(changed_paths),
            before_sha256=self._baseline_digests.get(last_path),
            after_sha256=self._active_digests[last_path],
            content_changed=True,
            cross_file_triggered=cross_triggered,
            parses_executed=0,
            candidate_calls_inspected=calls_inspected,
            findings=tuple(new_findings),
            ranked_findings=tuple(ranked),
            advisory_text=advisory,
            advisory_utf8_bytes=len(advisory.encode("utf-8")),
            cpu_seconds=time.process_time() - cpu_start,
            wall_seconds=time.perf_counter() - wall_start,
        )

    def apply_writes_batch(
        self,
        writes: Mapping[str, str] | Sequence[tuple[str, str]],
        *,
        legacy_optional_keyword_all_callers: bool = False,
    ) -> WriteDeltaResult:
        """Apply a multi-file patch atomically against the current session baseline."""
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        items = list(writes.items()) if isinstance(writes, Mapping) else list(writes)
        before_changed: dict[str, FileFacts | None] = {}
        after_changed: dict[str, FileFacts] = {}
        parses_before = self.cache.stats.parses
        changed_any = False
        first_path = ""
        last_old_sha = None
        last_new_sha = ""

        for raw_path, new_text in items:
            norm = self.cache.normalize_path(raw_path)
            if not first_path:
                first_path = norm
            source = SourceFile.from_text(norm, new_text)
            old_sha = self._active_digests.get(norm)
            if norm not in before_changed:
                before_changed[norm] = self._active_facts.get(norm)
                last_old_sha = old_sha
            last_new_sha = source.sha256
            if old_sha == source.sha256 and norm in self._active_facts:
                continue
            changed_any = True
            prior_file_facts = self._active_facts.get(norm)
            new_facts = self.cache.load_source(source, prior_facts=prior_file_facts)
            self._active_digests[norm] = source.sha256
            self._active_facts[norm] = new_facts
            self.index.add_file(new_facts)
            after_changed[norm] = new_facts

        parses_executed = self.cache.stats.parses - parses_before
        if not changed_any:
            return WriteDeltaResult(
                path=first_path,
                before_sha256=last_old_sha,
                after_sha256=last_new_sha,
                content_changed=False,
                cross_file_triggered=False,
                parses_executed=0,
                candidate_calls_inspected=0,
                findings=(),
                ranked_findings=(),
                advisory_text="",
                advisory_utf8_bytes=0,
                cpu_seconds=time.process_time() - cpu_start,
                wall_seconds=time.perf_counter() - wall_start,
            )

        findings, cross_triggered, calls_inspected = analyze_changed_files_in_index(
            before_changed,
            after_changed,
            self.index,
            legacy_optional_keyword_all_callers=legacy_optional_keyword_all_callers,
        )
        ranked = rank_findings(findings)
        self._emitted_finding_keys.update(_finding_key(f) for f in ranked)
        advisory = format_advisory(ranked, max_bytes=self.max_advisory_bytes)
        return WriteDeltaResult(
            path=",".join(sorted(after_changed)),
            before_sha256=last_old_sha,
            after_sha256=last_new_sha,
            content_changed=True,
            cross_file_triggered=cross_triggered,
            parses_executed=parses_executed,
            candidate_calls_inspected=calls_inspected,
            findings=tuple(findings),
            ranked_findings=tuple(ranked),
            advisory_text=advisory,
            advisory_utf8_bytes=len(advisory.encode("utf-8")),
            cpu_seconds=time.process_time() - cpu_start,
            wall_seconds=time.perf_counter() - wall_start,
        )
