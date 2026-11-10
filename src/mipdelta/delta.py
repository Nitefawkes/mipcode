from __future__ import annotations

from typing import Iterable, Mapping

from .cache import FactCache
from .facts import parse_file
from .model import FileFacts, Finding, FunctionFact, ObservedCallEdge
from .resolve import (
    MutableResolutionIndex,
    build_index,
    resolve_call_with_provenance,
    value_refers_to_target,
)
from .stubs import parse_stub_tree

_DETECTOR_PRIORITY = {
    "required_keyword_missing": 1,
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


def _field_stem(name: str) -> str:
    name = name.lstrip("_")
    for suffix in ("_minimum_levels", "_thresholds", "_limits", "_levels", "_policies"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name[:-1] if name.endswith("s") else name


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
) -> tuple[list[Finding], bool, int]:
    """Core incremental delta analyzer operating strictly on mutated files + MutableResolutionIndex."""
    findings: list[Finding] = []
    changed_fns: list[tuple[FunctionFact | None, FunctionFact]] = []
    changed_nullable_params: dict[tuple[str, str], set[str]] = {}
    added_optional_parameters: list[tuple[FunctionFact, str]] = []
    added_parameters: list[tuple[FunctionFact, str]] = []
    cross_file_triggered = False
    candidate_calls_inspected = 0

    for path in sorted(after_changed):
        old_file = before_changed.get(path)
        new_file = after_changed[path]
        old_map = old_file.functions if old_file else {}
        new_map = new_file.functions

        for qualname in sorted(old_map.keys() | new_map.keys()):
            old = old_map.get(qualname)
            new = new_map.get(qualname)
            if new and (
                old is None
                or old.parameters != new.parameters
                or (old.source_slice != new.source_slice and old.body_dump != new.body_dump)
            ):
                changed_fns.append((old, new))
                old_params = _parameter_map(old)
                new_params = _parameter_map(new)
                if old is not None and old.parameters != new.parameters:
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
                    if prior is None and name not in {"self", "cls"}:
                        added_parameters.append((new, name))

    for old, new in changed_fns:
        old_required = _required_kwonly(old)
        required_added = _required_kwonly(new) - old_required

        # 1. Required keyword missing at resolved callers
        if required_added:
            cross_file_triggered = True
            for caller_facts, call in index.calls_for_callee(new.name):
                candidate_calls_inspected += 1
                if call.has_kwargs:
                    continue
                resolved = resolve_call_with_provenance(call, caller_facts, index)
                matched_prov = next(
                    (prov for target, prov in resolved if target.path == new.path and target.qualname == new.qualname),
                    None,
                )
                if matched_prov is None:
                    continue
                missing = required_added - set(call.keyword_names)
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
                        if not _has_kwarg(override):
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
                    if (
                        name in old_params
                        and old_params[name].default != base_parameter.default
                        and override_parameter.default != base_parameter.default
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
                        and _field_stem(field) == _field_stem(read)
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

        # 4c. Exception contract review: newly raised exception type bypassing narrow caller try/except handler
        if old is not None:
            added_raises = new.raised_exceptions - old.raised_exceptions
            if added_raises:
                cross_file_triggered = True
                added_param_names = set(_parameter_map(new)) - set(_parameter_map(old))
                for caller_facts, call in index.calls_for_callee(new.name):
                    candidate_calls_inspected += 1
                    if not call.enclosing_caught_exceptions:
                        continue
                    resolved = resolve_call_with_provenance(call, caller_facts, index)
                    matched_prov = next(
                        (prov for item, prov in resolved if item.path == new.path and item.qualname == new.qualname),
                        None,
                    )
                    if matched_prov is None:
                        continue
                    effective_raises = set(added_raises)
                    if added_param_names and not (added_param_names & set(call.keyword_names)) and not call.has_kwargs:
                        effective_raises -= {"ValueError", "TypeError"}
                    if not effective_raises:
                        continue
                    caught_set = set(call.enclosing_caught_exceptions)
                    for caught_name in list(caught_set):
                        for sub in index.subclasses_of(caught_name):
                            caught_set.add(sub.name)
                    uncaught = sorted(effective_raises - caught_set)
                    if uncaught:
                        exc_list = ", ".join(uncaught)
                        caught_list = ", ".join(call.enclosing_caught_exceptions)
                        findings.append(
                            Finding(
                                "exception_contract_review",
                                new.qualname,
                                caller_facts.path,
                                call.line,
                                call.line,
                                "candidate",
                                (
                                    f"{new.qualname} newly raises {exc_list}, bypassing caller try/except ({caught_list})",
                                ),
                                matched_prov,
                            )
                        )

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

    # 7. Intra-function bare truthiness on newly added/changed None-default parameters (O(1) via bare_truthy_checks)
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

    return deduplicate_findings(findings), cross_file_triggered, candidate_calls_inspected


def analyze_delta(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    cache: FactCache | None = None,
    stub_files: Mapping[str, str] | None = None,
    runtime_edges: Iterable[ObservedCallEdge] | None = None,
    legacy_optional_keyword_all_callers: bool = False,
) -> list[Finding]:
    """Stateless before/after package analyzer (preserving v1 API while eliminating redundant parses)."""
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
    )
    return findings
