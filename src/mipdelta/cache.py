from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .facts import parse_file
from .model import FileFacts, SourceFile


@dataclass
class CacheStats:
    hits: int = 0
    parses: int = 0
    invalidations: int = 0


class FactCache:
    """In-memory FileFacts cache keyed by normalized path and exact SHA-256 digest.

    Filesystem metadata (mtime_ns, size) is never used as a freshness or custody proof.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], FileFacts] = {}
        self._active_digest: dict[str, str] = {}
        self.stats = CacheStats()

    @staticmethod
    def normalize_path(path: str) -> str:
        raw = path.replace("\\", "/")
        absolute = raw.startswith("/") or (len(raw) > 1 and raw[1] == ":")
        parts: list[str] = []
        for part in raw.split("/"):
            if part in ("", "."):
                continue
            if part == ".." and parts and parts[-1] != "..":
                parts.pop()
            elif part == ".." and not absolute:
                parts.append(part)
            else:
                parts.append(part)
        prefix = "/" if raw.startswith("/") else ""
        if len(raw) > 1 and raw[1] == ":":
            drive, _, rest = raw.partition(":")
            prefix = f"{drive.lower()}:/" if rest.startswith("/") else f"{drive.lower()}:"
        return prefix + "/".join(parts)

    def load(self, path: str, text: str, *, prior_facts: FileFacts | None = None) -> FileFacts:
        normalized = self.normalize_path(path)
        source = SourceFile.from_text(normalized, text)
        return self.load_source(source, prior_facts=prior_facts)

    def load_source(self, source: SourceFile, *, prior_facts: FileFacts | None = None) -> FileFacts:
        normalized = self.normalize_path(source.path)
        key = (normalized, source.sha256)
        cached = self._rows.get(key)
        if cached is not None:
            prior = self._active_digest.get(normalized)
            if prior is not None and prior != source.sha256:
                self.stats.invalidations += 1
            self._active_digest[normalized] = source.sha256
            self.stats.hits += 1
            return cached
        prior_digest = self._active_digest.get(normalized)
        if prior_digest is not None and prior_digest != source.sha256:
            self.stats.invalidations += 1
            if prior_facts is None:
                prior_facts = self._rows.get((normalized, prior_digest))
        facts = parse_file(normalized, source.text, prior_facts=prior_facts)
        self._rows[key] = facts
        self._active_digest[normalized] = source.sha256
        self.stats.parses += 1
        return facts

    def clear(self) -> None:
        self._rows.clear()
        self._active_digest.clear()

    def digest_for(self, path: str) -> str | None:
        return self._active_digest.get(self.normalize_path(path))

    def iter_entries(self) -> Iterator[tuple[str, str, FileFacts]]:
        for (path, digest), facts in self._rows.items():
            yield path, digest, facts
