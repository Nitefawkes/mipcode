from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from .model import CallFact, ClassFact, FileFacts, FunctionFact, ObservedCallEdge
from .stubs import TypeCandidateIndex


def _absolute_module(imported: str, current_module: str) -> str:
    if not imported.startswith("."):
        return imported
    level = len(imported) - len(imported.lstrip("."))
    suffix = imported[level:]
    package = current_module.split(".")[:-1]
    trim = max(0, level - 1)
    if trim:
        package = package[: max(0, len(package) - trim)]
    if suffix:
        package.extend(part for part in suffix.split(".") if part)
    return ".".join(package)


class MutableResolutionIndex:
    """Incremental symbol, subclass, and call-site index supporting O(|file|) updates."""

    def __init__(
        self,
        *,
        stub_files: Mapping[str, FileFacts] | None = None,
        runtime_edges: Iterable[ObservedCallEdge] | None = None,
    ) -> None:
        self.files: dict[str, FileFacts] = {}
        self.by_module_name: dict[tuple[str, str], dict[str, FunctionFact]] = defaultdict(dict)
        self.by_short_name: dict[str, dict[str, FunctionFact]] = defaultdict(dict)
        self.classes_by_short: dict[str, dict[str, ClassFact]] = defaultdict(dict)
        self.subclasses_by_base: dict[str, dict[str, ClassFact]] = defaultdict(dict)
        self.calls_by_callee: dict[str, dict[str, list[CallFact]]] = defaultdict(dict)
        self.calls_by_value_leaf: dict[str, dict[str, list[CallFact]]] = defaultdict(dict)
        self._callee_keys_by_path: dict[str, set[str]] = defaultdict(set)
        self._val_keys_by_path: dict[str, set[str]] = defaultdict(set)
        self._stub_files: dict[str, FileFacts] = dict(stub_files or {})
        self._type_index: TypeCandidateIndex | None = None
        self._runtime_by_site: dict[tuple[str, str], list[ObservedCallEdge]] = defaultdict(list)
        if runtime_edges:
            self.add_runtime_edges(runtime_edges)

    def add_runtime_edges(self, edges: Iterable[ObservedCallEdge]) -> None:
        for edge in edges:
            key = (edge.caller_qualname, edge.callee_name)
            if edge not in self._runtime_by_site[key]:
                self._runtime_by_site[key].append(edge)
            if ".<locals>." in edge.caller_qualname:
                outer = edge.caller_qualname.split(".<locals>.")[0]
                outer_key = (outer, edge.callee_name)
                if edge not in self._runtime_by_site[outer_key]:
                    self._runtime_by_site[outer_key].append(edge)

    @property
    def type_index(self) -> TypeCandidateIndex:
        if self._type_index is None:
            self._type_index = TypeCandidateIndex.build(self.files, self._stub_files)
        return self._type_index

    @staticmethod
    def _class_type_sig(facts: FileFacts) -> dict[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]]:
        return {
            c.qualname: (c.bases, tuple(sorted(c.attr_annotations.items())))
            for c in facts.classes.values()
            if c.bases or c.attr_annotations
        }

    def remove_file(self, path: str) -> None:
        old = self.files.pop(path, None)
        if old is None:
            return
        if self._class_type_sig(old):
            self._type_index = None
        for fn in old.functions.values():
            ident = f"{path}::{fn.qualname}"
            mod_bucket = self.by_module_name.get((old.module, fn.name))
            if mod_bucket:
                mod_bucket.pop(ident, None)
                if not mod_bucket:
                    self.by_module_name.pop((old.module, fn.name), None)
            short_bucket = self.by_short_name.get(fn.name)
            if short_bucket:
                short_bucket.pop(ident, None)
                if not short_bucket:
                    self.by_short_name.pop(fn.name, None)
        for cls in old.classes.values():
            cident = f"{path}::{cls.qualname}"
            cbucket = self.classes_by_short.get(cls.name)
            if cbucket:
                cbucket.pop(cident, None)
                if not cbucket:
                    self.classes_by_short.pop(cls.name, None)
            for base in cls.bases:
                bshort = base.split(".")[-1]
                sbucket = self.subclasses_by_base.get(bshort)
                if sbucket:
                    sbucket.pop(cident, None)
                    if not sbucket:
                        self.subclasses_by_base.pop(bshort, None)
        for callee_bucket in self._callee_keys_by_path.pop(path, ()):
            bucket = self.calls_by_callee.get(callee_bucket)
            if bucket is not None:
                bucket.pop(path, None)
                if not bucket:
                    self.calls_by_callee.pop(callee_bucket, None)
        for val_bucket in self._val_keys_by_path.pop(path, ()):
            bucket = self.calls_by_value_leaf.get(val_bucket)
            if bucket is not None:
                bucket.pop(path, None)
                if not bucket:
                    self.calls_by_value_leaf.pop(val_bucket, None)

    def add_file(self, facts: FileFacts) -> None:
        path = facts.path
        old = self.files.get(path)
        old_sig = self._class_type_sig(old) if old is not None else {}
        saved_tindex = self._type_index
        if path in self.files:
            self.remove_file(path)
        self.files[path] = facts
        if old_sig == self._class_type_sig(facts):
            self._type_index = saved_tindex
        else:
            self._type_index = None
        for fn in facts.functions.values():
            ident = f"{path}::{fn.qualname}"
            self.by_module_name[(facts.module, fn.name)][ident] = fn
            self.by_short_name[fn.name][ident] = fn
        for cls in facts.classes.values():
            cident = f"{path}::{cls.qualname}"
            self.classes_by_short[cls.name][cident] = cls
            for base in cls.bases:
                bshort = base.split(".")[-1]
                if bshort:
                    self.subclasses_by_base[bshort][cident] = cls
        for call in facts.calls:
            callee_keys = {call.callee_name}
            if len(call.callee_chain) == 1 and call.callee_chain[0] in facts.imports:
                _, imported_sym = facts.imports[call.callee_chain[0]]
                if imported_sym:
                    callee_keys.add(imported_sym)
            for ckey in callee_keys:
                self.calls_by_callee[ckey].setdefault(path, []).append(call)
                self._callee_keys_by_path[path].add(ckey)
            seen_leaves: set[str] = set()
            for val in call.value_names:
                leaf = val.split(".")[-1]
                if leaf and leaf not in seen_leaves:
                    seen_leaves.add(leaf)
                    self.calls_by_value_leaf[leaf].setdefault(path, []).append(call)
                    self._val_keys_by_path[path].add(leaf)
                if "." not in val and val in facts.imports:
                    _, imp_sym = facts.imports[val]
                    if imp_sym and imp_sym not in seen_leaves:
                        seen_leaves.add(imp_sym)
                        self.calls_by_value_leaf[imp_sym].setdefault(path, []).append(call)
                        self._val_keys_by_path[path].add(imp_sym)

    def calls_for_callee(self, callee_name: str) -> list[tuple[FileFacts, CallFact]]:
        out: list[tuple[FileFacts, CallFact]] = []
        for path in sorted(self.calls_by_callee.get(callee_name, ())):
            facts = self.files[path]
            for call in self.calls_by_callee[callee_name][path]:
                out.append((facts, call))
        return out

    def calls_for_value_leaf(self, symbol_name: str) -> list[tuple[FileFacts, CallFact]]:
        out: list[tuple[FileFacts, CallFact]] = []
        for path in sorted(self.calls_by_value_leaf.get(symbol_name, ())):
            facts = self.files[path]
            for call in self.calls_by_value_leaf[symbol_name][path]:
                out.append((facts, call))
        return out

    def subclasses_of(self, base_short_name: str) -> list[ClassFact]:
        bucket = self.subclasses_by_base.get(base_short_name, {})
        return [bucket[k] for k in sorted(bucket)]

    def runtime_edges_for(self, caller_qualname: str | None, callee_name: str) -> list[ObservedCallEdge]:
        if not caller_qualname:
            return []
        return list(self._runtime_by_site.get((caller_qualname, callee_name), ()))


@dataclass(frozen=True)
class ResolutionIndex:
    """Immutable view compatible with v1 build_index callers, backed by MutableResolutionIndex."""

    mutable: MutableResolutionIndex

    @property
    def by_module_name(self) -> Mapping[tuple[str, str], tuple[FunctionFact, ...]]:
        return {k: tuple(v.values()) for k, v in self.mutable.by_module_name.items()}

    @property
    def by_short_name(self) -> Mapping[str, tuple[FunctionFact, ...]]:
        return {k: tuple(v.values()) for k, v in self.mutable.by_short_name.items()}


def build_index(
    files: Mapping[str, FileFacts],
    *,
    stub_files: Mapping[str, FileFacts] | None = None,
    runtime_edges: Iterable[ObservedCallEdge] | None = None,
) -> MutableResolutionIndex:
    idx = MutableResolutionIndex(stub_files=stub_files, runtime_edges=runtime_edges)
    for path in sorted(files):
        idx.add_file(files[path])
    return idx


def _suffix(module: str, symbol: str, index: MutableResolutionIndex | ResolutionIndex) -> tuple[FunctionFact, ...]:
    mut = index.mutable if isinstance(index, ResolutionIndex) else index
    exact = mut.by_module_name.get((module, symbol))
    if exact:
        return tuple(exact.values())
    return tuple(
        row
        for (mod, name), rows in mut.by_module_name.items()
        if name == symbol and (mod == module or mod.endswith("." + module))
        for row in rows.values()
    )


def resolve_call_with_provenance(
    call: CallFact,
    facts: FileFacts,
    index: MutableResolutionIndex | ResolutionIndex,
) -> tuple[tuple[FunctionFact, str], ...]:
    """Resolve call targets with explicit provenance: static_ast, static_type_candidate, or observed_runtime."""
    mut = index.mutable if isinstance(index, ResolutionIndex) else index
    chain = call.callee_chain

    # 1. Exact static import / module alias resolution
    if chain:
        alias = facts.imports.get(chain[0])
        if alias:
            imported_module, imported_symbol = alias
            imported_module = _absolute_module(imported_module, facts.module)
            if imported_symbol:
                tail = ".".join([imported_symbol, *chain[1:]])
                rows = _suffix(imported_module, tail.split(".")[-1], mut)
                if len(chain) >= 2 and imported_symbol != tail.split(".")[-1]:
                    cls_filtered = [
                        r for r in rows
                        if r.class_qualname and r.class_qualname.split(".")[-1] == imported_symbol
                    ]
                    if cls_filtered:
                        return tuple((r, "static_ast") for r in cls_filtered)
                if rows:
                    return tuple((r, "static_ast") for r in rows)
            else:
                module = ".".join([imported_module, *chain[1:-1]])
                rows = _suffix(module, chain[-1], mut)
                if rows:
                    return tuple((r, "static_ast") for r in rows)

    # 2. Direct self.method(...) resolution on enclosing class (or base class)
    if call.receiver_chain == ("self",) and call.owner_function and "." in call.owner_function:
        owner_class = call.owner_function.rsplit(".", 1)[0].split(".")[-1]
        candidates = [
            fn
            for fn in mut.by_short_name.get(call.callee_name, {}).values()
            if fn.class_qualname and fn.class_qualname.split(".")[-1] == owner_class
        ]
        if len(candidates) == 1:
            return ((candidates[0], "static_ast"),)

    # 3. Same-module unambiguous function or Class.method call
    if len(chain) == 1:
        same_module = mut.by_module_name.get((facts.module, call.callee_name))
        if same_module and len(same_module) == 1:
            fn = next(iter(same_module.values()))
            if fn.class_qualname is None:
                return ((fn, "static_ast"),)
    elif len(chain) == 2 and chain[0] in facts.classes:
        method = facts.classes[chain[0]].methods.get(call.callee_name)
        if method is not None:
            return ((method, "static_ast"),)

    # 4. Direct constructor receiver: Cls().method(...)
    if call.constructed_receiver:
        cls = call.constructed_receiver
        candidates = [
            fn
            for fn in mut.by_short_name.get(call.callee_name, {}).values()
            if fn.class_qualname and fn.class_qualname.split(".")[-1] == cls
        ]
        if len(candidates) == 1:
            return ((candidates[0], "static_ast"),)

    # 5. Observed runtime edges from Python 3.12 sys.monitoring (positive execution evidence)
    rt_edges = mut.runtime_edges_for(call.owner_function, call.callee_name)
    if rt_edges:
        rt_matches: list[tuple[FunctionFact, str]] = []
        seen_ids: set[str] = set()
        for edge in rt_edges:
            if edge.receiver_type:
                rshort = edge.receiver_type.split(".")[-1]
                for fn in mut.by_short_name.get(call.callee_name, {}).values():
                    ident = f"{fn.path}::{fn.qualname}"
                    if ident not in seen_ids and fn.class_qualname and fn.class_qualname.split(".")[-1] == rshort:
                        seen_ids.add(ident)
                        rt_matches.append((fn, "observed_runtime"))
        if rt_matches:
            return tuple(rt_matches)

    # 6. Static .pyi / inline type-annotation receiver-chain candidate resolution
    if call.receiver_chain and (mut._stub_files or mut.files):
        head = call.receiver_chain[0]
        candidate_classes: tuple[str, ...] = ()
        if head == "self" and call.owner_function and "." in call.owner_function:
            owner_cls = call.owner_function.rsplit(".", 1)[0].split(".")[-1]
            candidate_classes = (owner_cls,)
        elif call.owner_function and call.owner_function in facts.functions:
            owner_fn = facts.functions[call.owner_function]
            for pname, ptype in owner_fn.param_annotations:
                if pname == head:
                    candidate_classes = (ptype.split(".")[-1],)
                    break

        tindex = mut.type_index
        for attr in call.receiver_chain[1:]:
            next_classes: list[str] = []
            for cshort in candidate_classes:
                for resolved in tindex.resolve_attr_types(cshort, attr):
                    if resolved not in next_classes:
                        next_classes.append(resolved)
            candidate_classes = tuple(next_classes)
            if not candidate_classes:
                break

        if candidate_classes and (len(call.receiver_chain) > 1 or head != "self"):
            typed_matches: list[tuple[FunctionFact, str]] = []
            for fn in mut.by_short_name.get(call.callee_name, {}).values():
                if fn.class_qualname and fn.class_qualname.split(".")[-1] in candidate_classes:
                    typed_matches.append((fn, "static_type_candidate"))
            if len(typed_matches) == 1:
                return tuple(typed_matches)

    return ()


def resolve_call(
    call: CallFact,
    facts: FileFacts,
    index: MutableResolutionIndex | ResolutionIndex,
) -> tuple[FunctionFact, ...]:
    return tuple(fn for fn, _ in resolve_call_with_provenance(call, facts, index))


def value_refers_to_target(name: str, facts: FileFacts, target: FunctionFact) -> bool:
    if name == target.name and facts.module == target.module:
        return True
    first, *rest = name.split(".")
    alias = facts.imports.get(first)
    if not alias:
        return False
    raw_module, imported = alias
    module = _absolute_module(raw_module, facts.module)
    if imported:
        resolved = ".".join([imported, *rest])
        return module == target.module and resolved == target.name
    resolved_module = ".".join([module, *rest[:-1]]) if rest else module
    resolved_name = rest[-1] if rest else first
    return resolved_module == target.module and resolved_name == target.name
