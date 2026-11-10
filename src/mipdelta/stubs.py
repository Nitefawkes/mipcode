from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping

from .facts import parse_file
from .model import FileFacts


@dataclass(frozen=True)
class TypeCandidateIndex:
    """Version-pinned static type-annotation candidate table (.pyi + inline annotations).

    Annotations provide static receiver candidates only; they do not prove runtime
    receiver identity or dynamic plugin dispatch.
    """

    class_attr_types: Mapping[tuple[str, str], tuple[str, ...]]
    class_bases: Mapping[str, tuple[str, ...]]

    @classmethod
    def empty(cls) -> "TypeCandidateIndex":
        return cls({}, {})

    @classmethod
    def build(
        cls,
        source_files: Mapping[str, FileFacts],
        stub_files: Mapping[str, FileFacts] | None = None,
    ) -> "TypeCandidateIndex":
        attrs: dict[tuple[str, str], list[str]] = defaultdict(list)
        bases: dict[str, list[str]] = defaultdict(list)

        for collection in (stub_files or {}, source_files):
            for facts in collection.values():
                for cfact in facts.classes.values():
                    short_cls = cfact.name
                    for base in cfact.bases:
                        bshort = base.split(".")[-1]
                        if bshort and bshort not in bases[short_cls]:
                            bases[short_cls].append(bshort)
                    for attr_name, ann_type in cfact.attr_annotations.items():
                        tshort = ann_type.split(".")[-1]
                        if tshort and tshort not in attrs[(short_cls, attr_name)]:
                            attrs[(short_cls, attr_name)].append(tshort)

        return cls(
            {k: tuple(v) for k, v in attrs.items()},
            {k: tuple(v) for k, v in bases.items()},
        )

    def resolve_attr_types(self, class_short_name: str, attr_name: str) -> tuple[str, ...]:
        seen_classes: set[str] = set()
        queue = [class_short_name]
        found: list[str] = []
        while queue:
            current = queue.pop(0)
            if current in seen_classes:
                continue
            seen_classes.add(current)
            for target in self.class_attr_types.get((current, attr_name), ()):
                if target not in found:
                    found.append(target)
            for base in self.class_bases.get(current, ()):
                if base not in seen_classes:
                    queue.append(base)
        return tuple(found)


def parse_stub_tree(stub_texts: Mapping[str, str]) -> dict[str, FileFacts]:
    """Parse .pyi stub texts into FileFacts keyed by normalized logical module path."""
    out: dict[str, FileFacts] = {}
    for raw_path, text in stub_texts.items():
        norm = raw_path.replace("\\", "/")
        logical = norm[:-4] + ".py" if norm.endswith(".pyi") else norm
        out[norm] = parse_file(logical, text)
    return out
