from __future__ import annotations

import builtins
from typing import Iterable, Mapping

from .cache import FactCache
from .facts import parse_file
from .model import CallFact, ClassFact, FileFacts, Finding, FunctionFact, ObservedCallEdge
from .resolve import (
    MutableResolutionIndex,
    build_index,
    resolve_call_with_provenance,
    value_refers_to_target,
)
from .signatures import call_head_is_parameter, signature_review_reason
from .stubs import parse_stub_tree

_DETECTOR_PRIORITY = {
    "required_keyword_missing": 1,
    "call_signature_review": 1,
    "removed_callable_review": 1,
    "callback_binding_review": 2,
    "override_signature_review": 3,
    "return_contract_review": 4,
    "exception_contract_review": 5,
    "delegating_wrapper_review": 6,
    "parameter_sink_review": 7,
    "parameter_truthiness_review": 8,
    "state_field_review": 9,
    "optional_keyword_contract": 10,
}

# Detectors that report only when a caller names them.
# - parameter_truthiness_review: upstream code routinely tests a None-default parameter
#   by truth value on purpose.
# - callback_binding_review: across two held-out rounds 22 of its 23 reports were false.
#   A callable passed as a value is unaffected when its target gains an optional parameter.
# removed_callable_review was opt-in for one round and is on again: its false reports were
# names still bound in the same scope, which it now recognizes.
OPT_IN_DETECTORS = frozenset({"parameter_truthiness_review", "callback_binding_review"})


def enabled_detectors(enable: Iterable[str]) -> frozenset[str]:
    """Validate a request for opt-in detectors."""
    if isinstance(enable, str):
        enable = (enable,)
    names = frozenset(enable)
    unknown = sorted(names - OPT_IN_DETECTORS)
    if unknown:
        raise ValueError(
            f"Not an opt-in detector: {', '.join(unknown)}. Available: {', '.join(sorted(OPT_IN_DETECTORS))}"
        )
    return names


_PROVENANCE_TAG = {
    "observed_runtime": "rt-obs",
    "static_ast": "static",
    "static_type_candidate": "type-cand",
}


def _is_test_path(path: str) -> bool:
    parts = path.replace("\\", "/").lower().split("/")
    if any(p in {"tests", "testing", "test_suite"} for p in parts[:-1]):
        return True
    leaf = parts[-1] if parts else ""
    return leaf.startswith("test_") or leaf.endswith("_test.py")


def _required_kwonly(function: FunctionFact | None) -> set[str]:
    if function is None:
        return set()
    return {
        parameter.name
        for parameter in function.parameters
        if parameter.kind == "kwonly" and parameter.default is None
    }


def _parameter_map(function: FunctionFact | None):
    return {p.name: p for p in function.parameters} if function else {}


def _has_kwarg(function: FunctionFact | None) -> bool:
    return bool(function and any(p.kind == "kwarg" for p in function.parameters))


def _constructor_pair_reads(function: FunctionFact, parameter: str, facts: FileFacts | None) -> bool:
    """__new__ and __init__ receive the same arguments; either one may consume a parameter."""
    if facts is None or function.class_qualname is None or function.name not in {"__new__", "__init__"}:
        return False
    cls = facts.classes.get(function.class_qualname)
    partner = cls.methods.get("__init__" if function.name == "__new__" else "__new__") if cls else None
    return partner is not None and parameter in partner.local_name_reads


def _nearest_base_method(
    cls: ClassFact, method: str, index: MutableResolutionIndex
) -> tuple[FunctionFact | None, bool]:
    """The closest workspace base definition of a method, and whether every base was resolved."""
    seen: set[str] = set()
    resolved = True
    level = [base.split(".")[-1] for base in cls.bases]
    while level:
        following: list[str] = []
        for name in level:
            if name in seen or name == "object":
                continue
            seen.add(name)
            bases = index.classes_by_short.get(name)
            if not bases:
                resolved = False
                continue
            for base in bases.values():
                if method in base.methods:
                    return base.methods[method], True
                following.extend(item.split(".")[-1] for item in base.bases)
        level = following
    return None, resolved


def _unread_parameter_is_expected(
    function: FunctionFact, parameter: str, facts: FileFacts | None, index: MutableResolutionIndex
) -> bool:
    """A new parameter can go unread where the signature is fixed by something else.

    Protocol methods, nested callbacks, and trivial hooks accept arguments they do not
    use. An override is still reported when the method it overrides consumes the
    parameter, or ignores it in a body that does real work.
    """
    if function.name.startswith("__") and function.name.endswith("__") and function.name != "__init__":
        return True
    expected = f"{function.class_qualname}.{function.name}" if function.class_qualname else function.name
    if function.qualname != expected:
        # Nested functions are usually callbacks whose caller fixes the arguments.
        return True
    cls = facts.classes.get(function.class_qualname) if facts and function.class_qualname else None
    if cls is None:
        return False
    base, resolved = _nearest_base_method(cls, function.name, index)
    if base is not None:
        return parameter not in base.local_name_reads and not base.own_calls
    overridden = any(function.name in child.methods for child in index.subclasses_of(cls.name))
    # A hook, or a method that an unresolved base may declare, is expected to ignore
    # arguments only when its body does no work of its own.
    return (overridden or not resolved) and not function.own_calls


def _default_value(default: str | None, facts: FileFacts | None) -> str | None:
    """A default's text, with a reference to a module constant replaced by its value."""
    if default is None or facts is None:
        return default
    return facts.constant_defaults.get(default, default)


def _still_bound(old: FunctionFact, new_file: FileFacts | None) -> bool:
    """The removed declaration's name is still assigned in the scope that declared it."""
    if new_file is None:
        return False
    if old.qualname == old.name:
        return old.name in new_file.module_name_stores
    if old.class_qualname is not None and old.qualname == f"{old.class_qualname}.{old.name}":
        cls = new_file.classes.get(old.class_qualname)
        return cls is not None and (
            old.name in cls.assigned_names
            or any(old.name in method.field_writes for method in cls.methods.values())
        )
    owner = new_file.functions.get(old.qualname.rsplit(".", 1)[0])
    return owner is not None and old.name in owner.local_name_stores


def _named_class_has_method(fn: FunctionFact, old: FunctionFact, call: CallFact, caller: FileFacts) -> bool:
    """The call names a class in the removed method's module, and that class has the method."""
    if fn.module != old.module or fn.name != old.name or fn.class_qualname is None:
        return False
    if len(call.callee_chain) < 2:
        return False
    receiver = call.callee_chain[-2]
    alias = caller.imports.get(receiver)
    return fn.class_qualname.rsplit(".", 1)[-1] == (alias[1] if alias and alias[1] else receiver)


def _class_may_inherit(cls: ClassFact, method: str, index: MutableResolutionIndex) -> bool:
    """A base class provides the method, or a base outside the workspace might."""
    seen: set[str] = set()
    pending = [base.split(".")[-1] for base in cls.bases]
    while pending:
        name = pending.pop()
        if name in seen or name == "object":
            continue
        seen.add(name)
        bases = index.classes_by_short.get(name)
        if not bases:
            return True
        for base in bases.values():
            if method in base.methods:
                return True
            pending.extend(item.split(".")[-1] for item in base.bases)
    return False


def _field_tokens(name: str) -> tuple[str, ...]:
    return tuple(
        token[:-1] if len(token) > 3 and token.endswith("s") else token
        for token in name.strip("_").lower().split("_")
        if token
    )


def _is_plural(name: str) -> bool:
    return name.rstrip("_").lower().endswith("s")


def _fields_share_stem(added: str, existing: str) -> bool:
    """Two attributes look like parallel state: limits/limit, or observers/_observer_minimum_levels."""
    first, second = _field_tokens(added), _field_tokens(existing)
    if not first or not second:
        return False
    if added.strip("_") == existing.strip("_"):
        # `_form` backing a `form` property is one field, not parallel state.
        return False
    if first == second:
        return True
    shorter, longer = sorted((first, second), key=len)
    # A longer name extending a collection's name is treated as a parallel collection.
    return longer[: len(shorter)] == shorter and _is_plural(added) and _is_plural(existing)


_BUILTIN_EXCEPTION_ANCESTORS = {
    name: frozenset(base.__name__ for base in value.__mro__ if base is not object)
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
}
_MAX_EXCEPTION_HOPS = 1


def _exception_ancestors(name: str, index: MutableResolutionIndex) -> tuple[frozenset[str], bool]:
    """Class names an exception is an instance of, and whether every base was resolved."""
    seen: set[str] = set()
    resolved = True
    pending = [name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        classes = index.classes_by_short.get(current)
        if classes:
            # A workspace class shadows a builtin of the same name.
            for cls in classes.values():
                pending.extend(base.split(".")[-1] for base in cls.bases)
        elif current in _BUILTIN_EXCEPTION_ANCESTORS:
            seen |= _BUILTIN_EXCEPTION_ANCESTORS[current]
        else:
            resolved = False
    return frozenset(seen), resolved


def _uncaught_exceptions(
    raised: Iterable[str], caught: Iterable[str], index: MutableResolutionIndex
) -> list[str]:
    """Raised names that no handler accepts. Abstains where the class relationship is unknown."""
    handlers: set[str] = set()
    handlers_in_workspace = True
    for name in caught:
        if name in index.classes_by_short:
            handlers.add(name)
            continue
        handlers_in_workspace = False
        # IOError and EnvironmentError are aliases whose class name is OSError.
        handlers.add(getattr(builtins, name).__name__ if name in _BUILTIN_EXCEPTION_ANCESTORS else name)
    uncaught = []
    for name in sorted(raised):
        ancestors, resolved = _exception_ancestors(name, index)
        if ancestors & handlers:
            continue
        if not resolved and not handlers_in_workspace:
            # An external exception may derive from an external or builtin handler class.
            continue
        uncaught.append(name)
    return uncaught


def _exception_contract_findings(
    old: FunctionFact, new: FunctionFact, index: MutableResolutionIndex
) -> tuple[list[Finding], int]:
    """Narrow handlers that a newly raised exception passes, at the call and one unguarded caller above it."""
    added = new.raised_exceptions - old.raised_exceptions
    if not added:
        return [], 0
    added_params = set(_parameter_map(new)) - set(_parameter_map(old))
    positional_slots = [
        p.name for p in new.parameters if p.kind in {"posonly", "positional"} and p.name not in {"self", "cls"}
    ]
    findings: list[Finding] = []
    inspected = 0
    visited = {(new.path, new.qualname)}
    frontier: list[tuple[FunctionFact, frozenset[str]]] = [(new, frozenset(added))]
    for hop in range(_MAX_EXCEPTION_HOPS + 1):
        next_frontier: list[tuple[FunctionFact, frozenset[str]]] = []
        for target, exceptions in frontier:
            for caller_facts, call in index.calls_for_callee(target.name):
                inspected += 1
                if call.under_broad_except:
                    continue
                resolved = resolve_call_with_provenance(call, caller_facts, index)
                provenance = next(
                    (prov for item, prov in resolved if item.path == target.path and item.qualname == target.qualname),
                    None,
                )
                if provenance is None:
                    continue
                if hop and (provenance != "static_ast" or len(resolved) != 1):
                    # Propagation follows only calls with one source-resolved target.
                    continue
                effective = set(exceptions)
                if hop == 0 and added_params and not (added_params & set(call.keyword_names)) and not call.has_kwargs:
                    effective -= {"ValueError", "TypeError"}
                if hop == 0 and added_params and not (call.has_kwargs or call.has_varargs):
                    omitted = {
                        name
                        for name in added_params - set(call.keyword_names)
                        if name not in positional_slots[: call.positional_count]
                    }
                    # A raise reached only through a test of a new parameter needs that parameter.
                    effective = {
                        name
                        for name in effective
                        if not all(
                            omitted & set(guards)
                            for raised, guards in new.guarded_raises
                            if raised == name
                        )
                        or not any(raised == name for raised, _ in new.guarded_raises)
                    }
                if not effective:
                    continue
                caught = call.enclosing_caught_exceptions
                uncaught = _uncaught_exceptions(effective, caught, index) if caught else sorted(effective)
                if not uncaught:
                    continue
                if caught:
                    names = ", ".join(uncaught)
                    handlers = ", ".join(caught)
                    detail = (
                        f"{new.qualname} newly raises {names}, bypassing caller try/except ({handlers})"
                        if hop == 0
                        else f"{new.qualname} newly raises {names}, which propagates through "
                        f"{target.qualname} past caller try/except ({handlers})"
                    )
                    findings.append(
                        Finding(
                            "exception_contract_review",
                            new.qualname,
                            caller_facts.path,
                            call.line,
                            call.line,
                            "candidate",
                            (detail,),
                            provenance,
                        )
                    )
                owner = caller_facts.functions.get(call.owner_function or "")
                if hop < _MAX_EXCEPTION_HOPS and owner is not None and (owner.path, owner.qualname) not in visited:
                    visited.add((owner.path, owner.qualname))
                    next_frontier.append((owner, frozenset(uncaught)))
        frontier = next_frontier
        if not frontier:
            break
    return findings, inspected


def deduplicate_findings(findings: Iterable[Finding]) -> list[Finding]:
    unique: dict[tuple, Finding] = {}
    for row in findings:
        if row.detector == "state_field_review":
            key = (row.detector, row.symbol, row.path, row.start_line, row.end_line)
            existing = unique.get(key)
            if existing:
                evidence = tuple(sorted(set((*existing.evidence, *row.evidence))))
                unique[key] = Finding(
                    row.detector,
                    row.symbol,
                    row.path,
                    row.start_line,
                    row.end_line,
                    row.confidence,
                    evidence,
                    row.resolution_source,
                )
            else:
                unique[key] = row
        else:
            key = (row.detector, row.symbol, row.path, row.start_line, row.evidence)
            unique[key] = row
    return sorted(unique.values(), key=lambda r: (r.path, r.start_line, r.detector, r.symbol))


def rank_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Deterministic risk-stratified ranking prioritizing production seams over test files."""
    deduped = deduplicate_findings(findings)
    return sorted(
        deduped,
        key=lambda r: (
            1 if _is_test_path(r.path) else 0,
            _DETECTOR_PRIORITY.get(r.detector, 99),
            0 if r.resolution_source == "observed_runtime" else (1 if r.resolution_source == "static_ast" else 2),
            r.path,
            r.start_line,
            r.symbol,
        ),
    )


def format_advisory(findings: Iterable[Finding], *, max_bytes: int = 250, max_items: int = 3) -> str:
    """Format ranked candidate findings into a bounded post-write advisory string (0 bytes when empty)."""
    ranked = rank_findings(findings)
    if not ranked:
        return ""
    header = "[mip-delta]"
    items: list[str] = []
    total_count = len(ranked)
    for row in ranked[:max_items]:
        tag = _PROVENANCE_TAG.get(row.resolution_source, "cand")
        item = f"{row.path}:{row.start_line} {row.detector}[{tag}] {row.symbol}"
        candidate_text = header + " " + "; ".join([*items, item])
        if total_count > len(items) + 1:
            rem = total_count - (len(items) + 1)
            if len((candidate_text + f" (+{rem})").encode("utf-8")) > max_bytes:
                if not items:
                    trimmed = candidate_text.encode("utf-8")[: max(0, max_bytes - 3)].decode("utf-8", errors="ignore")
                    return trimmed + "..."
                break
        elif len(candidate_text.encode("utf-8")) > max_bytes:
            if not items:
                trimmed = candidate_text.encode("utf-8")[: max(0, max_bytes - 3)].decode("utf-8", errors="ignore")
                return trimmed + "..."
            break
        items.append(item)
    rem = total_count - len(items)
    suffix = f" (+{rem})" if rem > 0 else ""
    out = header + " " + "; ".join(items) + suffix
    if len(items) == 1 and rem == 0 and ranked[0].evidence:
        with_ev = f"{out} ({ranked[0].evidence[0]})"
        if len(with_ev.encode("utf-8")) <= max_bytes:
            out = with_ev
    if len(out.encode("utf-8")) > max_bytes:
        out = out.encode("utf-8")[: max(0, max_bytes - 3)].decode("utf-8", errors="ignore") + "..."
    return out


def analyze_changed_files_in_index(
    before_changed: Mapping[str, FileFacts | None],
    after_changed: Mapping[str, FileFacts],
    index: MutableResolutionIndex,
    *,
    legacy_optional_keyword_all_callers: bool = False,
    enable: Iterable[str] = (),
) -> tuple[list[Finding], bool, int]:
    """Core incremental delta analyzer operating strictly on mutated files + MutableResolutionIndex.

    `enable` names detectors from OPT_IN_DETECTORS to run in addition to the default set.
    """
    enabled = enabled_detectors(enable)
    findings: list[Finding] = []
    changed_fns: list[tuple[FunctionFact | None, FunctionFact]] = []
    removed_fns: list[FunctionFact] = []
    changed_nullable_params: dict[tuple[str, str], set[str]] = {}
    added_optional_parameters: list[tuple[FunctionFact, str]] = []
    added_parameters: list[tuple[FunctionFact, str]] = []
    cross_file_triggered = False
    candidate_calls_inspected = 0

    removed_paths = before_changed.keys() - index.files.keys()
    for path in sorted(after_changed.keys() | removed_paths):
        old_file = before_changed.get(path)
        new_file = after_changed.get(path)
        if new_file is not None and new_file.parse_error is not None:
            # Editors can temporarily submit incomplete source to DeltaSession.
            continue
        old_map = old_file.functions if old_file else {}
        new_map = new_file.functions if new_file else {}

        for qualname in sorted(old_map.keys() | new_map.keys()):
            old = old_map.get(qualname)
            new = new_map.get(qualname)
            if new is None and old is not None:
                removed_fns.append(old)
                cross_file_triggered = True
                continue
            if new and (
                old is None
                or old.parameters != new.parameters
                or old.is_async != new.is_async
                or (old.source_slice != new.source_slice and old.body_dump != new.body_dump)
            ):
                changed_fns.append((old, new))
                old_params = _parameter_map(old)
                new_params = _parameter_map(new)
                if old is not None and (old.parameters != new.parameters or old.is_async != new.is_async):
                    cross_file_triggered = True
                elif old is None and _required_kwonly(new):
                    cross_file_triggered = True

                for name, parameter in new_params.items():
                    prior = old_params.get(name)
                    if prior is None or prior.kind != parameter.kind or prior.default != parameter.default:
                        if parameter.default == "Constant(value=None)":
                            changed_nullable_params.setdefault((new.path, new.qualname), set()).add(name)
                    if (
                        prior is None
                        and name not in {"self", "cls"}
                        and parameter.kind in {"kwonly", "positional"}
                        and parameter.default is not None
                    ):
                        if not legacy_optional_keyword_all_callers or parameter.kind == "kwonly":
                            added_optional_parameters.append((new, name))
                    if (
                        old is not None
                        and prior is None
                        and name not in {"self", "cls"}
                        and not name.startswith("_")
                        and parameter.kind not in {"vararg", "kwarg"}
                        and len(new.parameters) > len(old.parameters)
                        and not new.is_stub
                        and not _unread_parameter_is_expected(new, name, new_file, index)
                        and not _constructor_pair_reads(new, name, new_file)
                    ):
                        # Abstract declarations and catch-all parameters have no body to read them.
                        added_parameters.append((new, name))

    before_index: MutableResolutionIndex | None = None
    for old in removed_fns:
        new_file = index.files.get(old.path)
        replacement_import = new_file.imports.get(old.name) if new_file and old.qualname == old.name else None
        if replacement_import and replacement_import[1] and replacement_import[0] != old.module:
            # A direct re-export keeps the original import name available.
            continue
        if old.qualname == old.name and any(
            facts.module == old.module and (old.name in facts.imports or "*" in facts.imports)
            for facts in index.files.values()
        ):
            # The module became a package whose __init__ imports the name.
            continue
        surviving_class = new_file.classes.get(old.class_qualname) if new_file and old.class_qualname else None
        if surviving_class is not None and _class_may_inherit(surviving_class, old.name, index):
            continue
        if _still_bound(old, new_file):
            # Rebound as an alias, an unpacked local, or an attribute: the name still resolves.
            continue
        for caller_facts, call in index.calls_for_callee(old.name):
            candidate_calls_inspected += 1
            if call_head_is_parameter(call, caller_facts):
                continue
            current = resolve_call_with_provenance(call, caller_facts, index)
            if any(
                (fn.module == old.module and fn.qualname == old.qualname)
                or (fn.path == caller_facts.path and fn.name == old.name)
                or _named_class_has_method(fn, old, call, caller_facts)
                for fn, _ in current
            ):
                # Still defined, defined under the same name in the caller's own file, or
                # defined on the class the call names after that class was renamed.
                continue
            if before_index is None:
                previous_files = {
                    path: facts for path, facts in index.files.items() if path not in before_changed
                }
                previous_files.update({path: facts for path, facts in before_changed.items() if facts is not None})
                before_index = build_index(previous_files)
            resolved = resolve_call_with_provenance(call, caller_facts, before_index)
            if resolved != ((old, "static_ast"),):
                continue
            findings.append(
                Finding(
                    "removed_callable_review",
                    old.qualname,
                    caller_facts.path,
                    call.line,
                    call.line,
                    "candidate",
                    (f"previous declaration at {old.path}:{old.line} was removed; review this call",),
                    "static_ast",
                )
            )

    for old, new in changed_fns:
        old_required = _required_kwonly(old)
        required_added = _required_kwonly(new) - old_required
        signature_changed = old is not None and (old.parameters != new.parameters or old.is_async != new.is_async)

        # 1. Check the changed signature at resolved calls.
        if required_added or signature_changed:
            cross_file_triggered = True
            for caller_facts, call in index.calls_for_callee(new.name):
                candidate_calls_inspected += 1
                resolved = resolve_call_with_provenance(call, caller_facts, index)
                matched_prov = next(
                    (prov for target, prov in resolved if target.path == new.path and target.qualname == new.qualname),
                    None,
                )
                if matched_prov is None:
                    continue
                missing = required_added - set(call.keyword_names) if not call.has_kwargs else set()
                if missing:
                    names = ", ".join(sorted(missing))
                    findings.append(
                        Finding(
                            "required_keyword_missing",
                            new.qualname,
                            caller_facts.path,
                            call.line,
                            call.line,
                            "candidate",
                            (f"new required keyword(s) {names} may be absent at this call",),
                            matched_prov,
                        )
                    )
                elif old is not None and signature_changed and len(resolved) == 1:
                    reason = signature_review_reason(
                        old,
                        new,
                        call,
                        caller_facts,
                        before_changed.get(caller_facts.path, caller_facts),
                        matched_prov,
                    )
                    if reason:
                        findings.append(
                            Finding(
                                "call_signature_review",
                                new.qualname,
                                caller_facts.path,
                                call.line,
                                call.line,
                                "candidate",
                                (reason,),
                                matched_prov,
                            )
                        )

        # 2. First-class callable / descriptor value bindings
        if old and old.parameters != new.parameters:
            old_params = _parameter_map(old)
            new_params = _parameter_map(new)
            changed_names = {
                name
                for name, parameter in new_params.items()
                if name not in old_params or old_params[name] != parameter
            }
            for caller_facts, call in index.calls_for_value_leaf(new.name):
                candidate_calls_inspected += 1
                owner = caller_facts.functions.get(call.owner_function or "")
                if owner is not None and (
                    new.name in owner.local_name_stores or any(p.name == new.name for p in owner.parameters)
                ):
                    # The name is a parameter or local variable in this scope.
                    continue
                scope = new.qualname.rpartition(".")[0]
                if scope and scope != new.class_qualname:
                    inside = call.owner_function or ""
                    if caller_facts.path != new.path or not (inside == scope or inside.startswith(scope + ".")):
                        # Another function's nested helper of the same name.
                        continue
                if any(value_refers_to_target(value, caller_facts, new) for value in call.value_names):
                    wrapper_adapts = any(
                        wrapped_name == new.name and changed_names <= set(forwarded_keywords)
                        for wrapped_name, forwarded_keywords in call.callback_wrapped_keywords
                    )
                    if wrapper_adapts:
                        continue
                    # Also suppress functools.partial(fn, strict=...) when changed keywords are supplied directly
                    if call.callee_name == "partial" and changed_names <= set(call.keyword_names):
                        continue
                    findings.append(
                        Finding(
                            "callback_binding_review",
                            new.qualname,
                            caller_facts.path,
                            call.line,
                            call.line,
                            "candidate",
                            (f"changed callable {new.name} is passed as a value",),
                            "static_ast",
                        )
                    )

        # 3. Subclass override signature compatibility & default drift
        if new.class_qualname and old and old.parameters != new.parameters:
            base_name = new.class_qualname.split(".")[-1]
            old_params = _parameter_map(old)
            changed_base_params = {
                name: parameter
                for name, parameter in _parameter_map(new).items()
                if name not in old_params or old_params[name] != parameter
            }
            for child in index.subclasses_of(base_name):
                if child.qualname == new.class_qualname and child.path == new.path:
                    continue
                override = child.methods.get(new.name)
                if override is None:
                    continue
                override_params = _parameter_map(override)
                evidence = []
                for name, base_parameter in changed_base_params.items():
                    override_parameter = override_params.get(name)
                    if override_parameter is None:
                        positional = {"posonly", "positional"}
                        base_slots = [p.name for p in new.parameters if p.kind in positional]
                        override_slots = [p.name for p in override.parameters if p.kind in positional]
                        renamed_in_place = (
                            base_parameter.kind in positional
                            and len(old.parameters) == len(new.parameters)
                            and base_slots.index(name) < len(override_slots)
                        )
                        if not _has_kwarg(override) and not renamed_in_place:
                            evidence.append(f"override may omit changed base parameter {name}")
                        continue
                    incompatible_kinds = (
                        (base_parameter.kind == "posonly" and override_parameter.kind == "kwonly")
                        or (base_parameter.kind == "positional" and override_parameter.kind in {"posonly", "kwonly"})
                        or (base_parameter.kind == "kwonly" and override_parameter.kind == "posonly")
                    )
                    if incompatible_kinds:
                        evidence.append(
                            f"override parameter {name} kind {override_parameter.kind} may not accept base calls ({base_parameter.kind})"
                        )
                    base_default = _default_value(base_parameter.default, after_changed.get(new.path))
                    if (
                        name in old_params
                        and _default_value(old_params[name].default, before_changed.get(new.path)) != base_default
                        and _default_value(override_parameter.default, index.files.get(override.path)) != base_default
                    ):
                        evidence.append(
                            f"override default for changed base parameter {name} differs from the new base default"
                        )
                if evidence:
                    findings.append(
                        Finding(
                            "override_signature_review",
                            override.qualname,
                            override.path,
                            override.line,
                            override.end_line,
                            "candidate",
                            tuple(evidence),
                            "static_ast",
                        )
                    )

        # 4. Intra-class parallel state-field symmetry (strictly local to new.path)
        if old and new.class_qualname:
            added_fields = new.field_writes - old.field_writes
            cls = after_changed[new.path].classes.get(new.class_qualname)
            if added_fields and cls:
                old_file = before_changed.get(new.path)
                old_fns_in_file = old_file.functions if old_file else {}
                if new.name == "__init__":
                    operational_writers = []
                    for method in cls.methods.values():
                        if method.qualname == new.qualname:
                            continue
                        prior = old_fns_in_file.get(method.qualname)
                        prior_writes = prior.field_writes if prior else frozenset()
                        if (method.field_writes - prior_writes) & added_fields:
                            operational_writers.append(method)
                    if operational_writers:
                        continue
                for sibling in cls.methods.values():
                    if sibling.qualname == new.qualname:
                        continue
                    matched_fields = [
                        (field, read)
                        for field in added_fields
                        for read in sibling.field_reads
                        if field != read
                        and _fields_share_stem(field, read)
                        and read not in sibling.field_writes
                        and field not in sibling.field_reads
                        and field not in sibling.field_writes
                    ]
                    if matched_fields:
                        field, read = sorted(matched_fields)[0]
                        findings.append(
                            Finding(
                                "state_field_review",
                                sibling.qualname,
                                sibling.path,
                                sibling.line,
                                sibling.end_line,
                                "candidate",
                                (f"writer {new.qualname} adds self.{field}; {sibling.qualname} reads self.{read}",),
                                "static_ast",
                            )
                        )

        # 4b. Return contract review: fixed tuple-return arity change or newly nullable return
        if old is not None:
            tuple_arity_changed = (
                len(old.return_tuple_arities) == 1
                and len(new.return_tuple_arities) == 1
                and old.return_tuple_arities != new.return_tuple_arities
            )
            newly_nullable_return = (
                old.has_non_none_return
                and not old.has_explicit_none_return
                and not old.return_allows_none
                and new.has_non_none_return
                and new.has_explicit_none_return
                and not new.return_allows_none
            )
            if tuple_arity_changed or newly_nullable_return:
                cross_file_triggered = True
                old_arity = next(iter(old.return_tuple_arities)) if tuple_arity_changed else None
                new_arity = next(iter(new.return_tuple_arities)) if tuple_arity_changed else None
                for caller_facts, call in index.calls_for_callee(new.name):
                    candidate_calls_inspected += 1
                    resolved = resolve_call_with_provenance(call, caller_facts, index)
                    matched_prov = next(
                        (prov for item, prov in resolved if item.path == new.path and item.qualname == new.qualname),
                        None,
                    )
                    if matched_prov is None:
                        continue
                    if tuple_arity_changed and call.unpack_target_arity == old_arity:
                        findings.append(
                            Finding(
                                "return_contract_review",
                                new.qualname,
                                caller_facts.path,
                                call.line,
                                call.line,
                                "candidate",
                                (
                                    f"caller unpacks {old_arity} elements from {new.qualname}, which now returns {new_arity}-tuple",
                                ),
                                matched_prov,
                            )
                        )
                    elif newly_nullable_return and (call.result_dereferenced or call.unpack_target_arity is not None):
                        findings.append(
                            Finding(
                                "return_contract_review",
                                new.qualname,
                                caller_facts.path,
                                call.line,
                                call.line,
                                "candidate",
                                (
                                    f"{new.qualname} now returns None on a path while caller dereferences or unpacks result without guard",
                                ),
                                matched_prov,
                            )
                        )

        # 4c. Exception contract review: a newly raised exception passes a narrow caller handler
        if old is not None and new.raised_exceptions - old.raised_exceptions:
            cross_file_triggered = True
            exception_findings, inspected = _exception_contract_findings(old, new, index)
            findings.extend(exception_findings)
            candidate_calls_inspected += inspected

    # 5. Optional parameter forwarding: tightened v2 delegating-wrapper check (vs legacy all-callers mode)
    for target, parameter in added_optional_parameters:
        cross_file_triggered = True
        for caller_facts, call in index.calls_for_callee(target.name):
            candidate_calls_inspected += 1
            resolved = resolve_call_with_provenance(call, caller_facts, index)
            matched_prov = next(
                (prov for item, prov in resolved if item.path == target.path and item.qualname == target.qualname),
                None,
            )
            if matched_prov is None:
                continue

            if legacy_optional_keyword_all_callers:
                if parameter in call.keyword_names:
                    detail = f"call explicitly supplies new optional keyword {parameter}"
                else:
                    detail = f"call relies on new default for optional keyword {parameter}"
                findings.append(
                    Finding(
                        "optional_keyword_contract",
                        target.qualname,
                        caller_facts.path,
                        call.line,
                        call.line,
                        "candidate",
                        (detail,),
                        matched_prov,
                    )
                )
            else:
                # Tightened v2 rule: abstain when keyword or **kwargs is already supplied,
                # and abstain on ordinary consumers relying on backward-compatible defaults.
                # Only warn when an enclosing delegating wrapper (same method/function name
                # or accepting the same parameter name) calls target without forwarding it.
                if parameter in call.keyword_names or call.has_kwargs or parameter in call.value_names:
                    continue
                owner_fn = caller_facts.functions.get(call.owner_function) if call.owner_function else None
                if owner_fn is None:
                    continue
                if owner_fn.path == target.path and owner_fn.qualname == target.qualname:
                    continue
                owner_param_names = {p.name for p in owner_fn.parameters}
                is_delegating_wrapper = (owner_fn.name == target.name) or (parameter in owner_param_names)
                if not is_delegating_wrapper:
                    continue
                # If the delegating wrapper accepts `parameter` and forwards it to `target.name` in a
                # conditional branch (e.g. `if base_iri is not None: return plugin.serialize(x, base_iri=base_iri)`
                # followed by fallback `return plugin.serialize(x)`), do not flag the fallback call.
                if parameter in owner_param_names and any(
                    sibling.callee_name == target.name
                    and (
                        parameter in sibling.keyword_names
                        or sibling.has_kwargs
                        or parameter in sibling.value_names
                    )
                    for sibling in owner_fn.own_calls
                ):
                    continue
                findings.append(
                    Finding(
                        "delegating_wrapper_review",
                        owner_fn.qualname,
                        caller_facts.path,
                        call.line,
                        call.line,
                        "candidate",
                        (
                            f"delegating wrapper {owner_fn.qualname} calls {target.qualname} without forwarding optional parameter {parameter}",
                        ),
                        matched_prov,
                    )
                )

    # 6. Intra-function unused added parameter check (O(1) via pre-collected local_name_reads)
    for target, parameter in added_parameters:
        if parameter not in target.local_name_reads:
            findings.append(
                Finding(
                    "parameter_sink_review",
                    target.qualname,
                    target.path,
                    target.line,
                    target.end_line,
                    "candidate",
                    (f"new parameter {parameter} is not read in the function body; verify its intended behavioral sink",),
                    "static_ast",
                )
            )

    # 7. Opt-in: intra-function bare truthiness on newly added/changed None-default parameters
    #    (O(1) via bare_truthy_checks)
    for (path, qualname), nullable_names in sorted(changed_nullable_params.items()):
        fn = after_changed[path].functions.get(qualname)
        if fn is None:
            continue
        for tested_name, line in fn.bare_truthy_checks:
            if tested_name in nullable_names:
                findings.append(
                    Finding(
                        "parameter_truthiness_review",
                        qualname,
                        path,
                        line,
                        line,
                        "candidate",
                        (f"new or changed None-default parameter {tested_name} is tested as a bare truth value",),
                        "static_ast",
                    )
                )

    findings = [f for f in findings if f.detector not in OPT_IN_DETECTORS or f.detector in enabled]
    return deduplicate_findings(findings), cross_file_triggered, candidate_calls_inspected


def analyze_changed_only(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    cache: FactCache | None = None,
    enable: Iterable[str] = (),
) -> tuple[list[Finding], bool]:
    """Analyze changed files without the rest of the workspace.

    Returns the findings and whether a contract visible to other files changed. When
    that flag is False, the findings are complete and no other file needs to be read.
    When it is True, discard them and run analyze_delta over the whole workspace.
    `enable` names detectors from OPT_IN_DETECTORS to run as well.
    """
    parse = cache.load if cache is not None else parse_file
    paths = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    before_changed = {p: (parse(p, before[p]) if p in before else None) for p in paths}
    after_changed = {p: parse(p, after[p]) for p in paths if p in after}
    index = build_index(after_changed)
    findings, cross_file_triggered, _ = analyze_changed_files_in_index(
        before_changed, after_changed, index, enable=enable
    )
    return findings, cross_file_triggered


def analyze_delta(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    cache: FactCache | None = None,
    stub_files: Mapping[str, str] | None = None,
    runtime_edges: Iterable[ObservedCallEdge] | None = None,
    legacy_optional_keyword_all_callers: bool = False,
    enable: Iterable[str] = (),
) -> list[Finding]:
    """Stateless before/after package analyzer (preserving v1 API while eliminating redundant parses).

    `enable` names detectors from OPT_IN_DETECTORS to run in addition to the default set.
    """
    enable = enabled_detectors(enable)
    normalized_before = {p.replace("\\", "/"): text for p, text in before.items()}
    normalized_after = {p.replace("\\", "/"): text for p, text in after.items()}
    if stub_files is None:
        auto_stubs = {p: text for p, text in normalized_after.items() if p.endswith(".pyi")}
        if auto_stubs:
            stub_files = auto_stubs
            normalized_before = {p: text for p, text in normalized_before.items() if not p.endswith(".pyi")}
            normalized_after = {p: text for p, text in normalized_after.items() if not p.endswith(".pyi")}
    parse = cache.load if cache is not None else parse_file

    changed_paths = [
        p
        for p in sorted(normalized_before.keys() | normalized_after.keys())
        if normalized_before.get(p) != normalized_after.get(p)
    ]
    if not changed_paths:
        # Still populate cache if caller expects full load, else fast empty return
        if cache is not None:
            for p, text in normalized_after.items():
                parse(p, text)
        return []

    before_changed = {
        p: (parse(p, normalized_before[p]) if p in normalized_before else None)
        for p in changed_paths
    }
    after_files = {p: parse(p, text) for p, text in normalized_after.items()}
    after_changed = {p: after_files[p] for p in changed_paths if p in after_files}
    parsed_stubs = parse_stub_tree(stub_files) if stub_files else None
    index = build_index(after_files, stub_files=parsed_stubs, runtime_edges=runtime_edges)
    findings, _, _ = analyze_changed_files_in_index(
        before_changed,
        after_changed,
        index,
        legacy_optional_keyword_all_callers=legacy_optional_keyword_all_callers,
        enable=enable,
    )
    return findings
