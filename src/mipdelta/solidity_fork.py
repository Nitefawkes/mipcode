"""Zero-dependency Solidity fork-vs-upstream invariant regression differ."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .delta import _path_tier, is_vendored_path, rank_findings
from .model import Finding
from .solidity import (
    _CALLER_AUTH_HELPERS,
    _DEADLINE_IDENTS,
    _EMERGENCY_OR_PAUSE_FN_RE,
    _ETH_SEND_RE,
    _IDENT_RE,
    _NOT_PAUSED_HELPERS,
    _PUBLIC_CALLBACK_OR_INIT_NAMES,
    _PURE_EXTERNAL_TARGETS,
    _PURE_REVERT_BODY_RE,
    _REENTRANCY_HELPERS,
    _REQUIRE_RE,
    _TOKEN_IN_METHODS,
    _TOKEN_OUT_METHODS,
    _TRANSFER_HELPER_LIBS,
    SolCallFact,
    SolContractFact,
    SolFileFacts,
    SolFunctionFact,
    SolParamFact,
    SolStateVarFact,
    _classify_modifier_guard,
    _collect_ancestor_modifiers,
    _collect_ancestor_state_vars,
    _extract_base_ident,
    _extract_direct_guards,
    _extract_direct_surfaces,
    _extract_inline_guard_conditions,
    _extract_storage_vars,
    _extract_sync_steps,
    _extract_unconditional_calls,
    _is_external_call_in_fn,
    _is_state_var_mutated,
    _is_storage_ref_mutated,
    _top_level_unconditional_callees,
    parse_solidity_file,
)

_BOILERPLATE_FNS = frozenset({"constructor", "receive", "fallback", "supportsInterface"})
_SKIP_FORK_PATH_SEGMENTS = frozenset({"lib", "node_modules", "out", "cache", "artifacts", "dependencies"})

_AUTH_MOD_PREFIX_RE = re.compile(r"^(?:only|require|check|auth|restricted)", re.IGNORECASE)
_GUARD_MOD_NAME_RE = re.compile(r"^(?:only|when|non|not|require|check|ensure)[A-Z_]")
_SIGNATURE_AUTH_RE = re.compile(
    r"\b(?:ecrecover|ECDSA\s*\.\s*(?:recover|tryRecover)|SignatureChecker\s*\.\s*isValidSignature|"
    r"Permit2|ISignatureTransfer|EIP712)\b"
)

_TRANSFER_CALL_METHODS = _TOKEN_IN_METHODS | frozenset({"transfer", "safeTransfer", "safeTransferETH", "sendValue"})

_ROUND_UP_RE = re.compile(
    r"\b(?:Rounding\s*\.\s*(?:Up|Ceil)|mulDivUp|mulWadUp|divWadUp|rayMulUp|rayDivUp|"
    r"ceilDiv|mulDivRoundingUp|divRoundingUp|divUp)\b"
)
_ROUND_DOWN_RE = re.compile(
    r"\b(?:Rounding\s*\.\s*(?:Down|Floor)|mulDivDown|mulWadDown|divWadDown|rayMulDown|rayDivDown|"
    r"floorDiv|divRoundingDown|divDown)\b"
)
_PLAIN_DIV_RE = re.compile(r"\bmulDiv\s*\(|(?<![*/])/(?![*/=])")
_ERC4626_CONVERSION_FNS = frozenset({"withdraw", "mint", "redeem", "deposit", "_withdraw", "_deposit"})
_ERC4626_UP_RE = re.compile(r"\bpreview(?:Withdraw|Mint)\s*\(")
_ERC4626_DOWN_RE = re.compile(r"\bconvertTo(?:Shares|Assets)\s*\(")

_SLIPPAGE_FIELD_RE = re.compile(
    r"\b(amountOutMinimum|amountInMaximum|minAmountOut|amountOutMin|minReturn|minAmount|minLiquidity)\s*:\s*([^,}\n]+)"
)
_DEADLINE_FIELD_RE = re.compile(
    r"\b(deadline|expiry|expiration|validUntil)\s*:\s*([^,}\n]+)"
)
_SLIPPAGE_ARG_HINT_RE = re.compile(r"(?:min[A-Z_]|OutMin$|AmountMin$|slippage)", re.IGNORECASE)
_DEADLINE_ARG_HINT_RE = re.compile(r"(?:deadline|expiry|expiration|validUntil)", re.IGNORECASE)
_ZERO_LITERAL_RE = re.compile(r"^\s*(?:0+|0x0+|uint(?:256)?\s*\(\s*0\s*\))\s*$")
_WEAK_DEADLINE_RE = re.compile(
    r"^\s*(?:block\s*\.\s*timestamp|type\s*\(\s*uint(?:256)?\s*\)\s*\.\s*max|uint(?:256)?\s*\(\s*-1\s*\)|~uint(?:256)?\s*\(\s*0\s*\))\s*$"
)
_LOCAL_VAR_SIMPLE_RE = re.compile(
    r"\b(?:u?int(?:8|16|32|64|128|256)?|address|bool)\s+(?:memory\s+|calldata\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*="
)


@dataclass(frozen=True)
class _ContractCtx:
    contract: SolContractFact
    file_facts: SolFileFacts
    all_vars: dict[str, SolStateVarFact]
    mutable_vars: dict[str, SolStateVarFact]
    modifier_defs: dict[str, tuple[tuple[SolParamFact, ...], str]]
    all_fns_by_name: dict[str, list[SolFunctionFact]]
    mutating_helpers: frozenset[str]
    zero_arg_view_helpers: frozenset[str]
    custom_mod_inline_idents: dict[str, frozenset[str]]


@dataclass(frozen=True)
class _TreeIndex:
    files: dict[str, SolFileFacts]
    contexts: dict[int, _ContractCtx]
    contracts: list[SolContractFact]
    contracts_by_name: dict[str, list[SolContractFact]]
    workspace_libraries: frozenset[str]
    readonly_lib_map: dict[str, frozenset[str]]
    all_readonly_set: frozenset[str]
    all_func_stems: frozenset[str]
    state_var_names_by_contract: dict[int, frozenset[str]]


def _is_fork_excluded_path(path: str, *, include_non_prod: bool) -> bool:
    norm = path.replace("\\", "/")
    if is_vendored_path(norm):
        return True
    parts = [p.lower() for p in norm.split("/") if p]
    if any(seg in _SKIP_FORK_PATH_SEGMENTS for seg in parts[:-1]):
        return True
    if not include_non_prod and _path_tier(norm) > 0:
        return True
    return False


def _camel_suffix_token(name: str) -> str:
    tokens = re.findall(r"[A-Z][a-z0-9]*", name)
    return tokens[-1].lower() if tokens else name.lower()


def _path_suffix_overlap(path_a: str, path_b: str) -> int:
    pa = [p.lower() for p in path_a.replace("\\", "/").split("/") if p]
    pb = [p.lower() for p in path_b.replace("\\", "/").split("/") if p]
    count = 0
    for xa, xb in zip(reversed(pa), reversed(pb)):
        if xa != xb:
            break
        count += 1
    return count


def _kind_group(kind: str) -> str:
    return "library" if kind == "library" else "contract"


def _build_tree_index(files: Mapping[str, str], *, include_non_prod: bool) -> _TreeIndex:
    parsed: dict[str, SolFileFacts] = {}
    contracts_by_name: dict[str, list[SolContractFact]] = {}
    workspace_libs: set[str] = set()
    readonly_mut: dict[str, set[str]] = {}
    all_readonly: set[str] = set()
    contracts: list[SolContractFact] = []
    all_func_stems: set[str] = set()

    for path in sorted(files):
        if _is_fork_excluded_path(path, include_non_prod=include_non_prod):
            continue
        f_facts = parse_solidity_file(path, files[path])
        parsed[path] = f_facts
        for c in f_facts.contracts:
            contracts_by_name.setdefault(c.name, []).append(c)
            if c.kind != "interface":
                contracts.append(c)
            if c.kind == "library":
                workspace_libs.add(c.name)
                ro = readonly_mut.setdefault(c.name, set())
                for fn in c.functions:
                    if fn.mutability in ("view", "pure"):
                        ro.add(fn.name)
                        all_readonly.add(fn.name)
            for fn in c.functions:
                all_func_stems.add(fn.name.lstrip("_").lower())

    readonly_lib_map = {k: frozenset(v) for k, v in readonly_mut.items()}
    all_readonly_set = frozenset(all_readonly)
    contexts: dict[int, _ContractCtx] = {}
    state_var_names_by_contract: dict[int, frozenset[str]] = {}

    for f_facts in parsed.values():
        for c in f_facts.contracts:
            if c.kind == "interface":
                continue
            all_vars = _collect_ancestor_state_vars(c, contracts_by_name)
            mutable_vars = {k: v for k, v in all_vars.items() if not v.is_constant and not v.is_immutable}
            mod_defs = _collect_ancestor_modifiers(c, contracts_by_name)
            all_fns_by_name: dict[str, list[SolFunctionFact]] = {}
            for fn in c.functions:
                all_fns_by_name.setdefault(fn.name, []).append(fn)
            # Walk ancestor contracts for internal helpers and inherited modifiers
            seen_anc: set[str] = {c.name}
            q = list(c.bases)
            while q:
                bname = q.pop(0)
                if bname in seen_anc:
                    continue
                seen_anc.add(bname)
                for bc in contracts_by_name.get(bname, ()):
                    for bfn in bc.functions:
                        all_fns_by_name.setdefault(bfn.name, []).append(bfn)
                    q.extend(bc.bases)

            zero_arg_view = frozenset(
                fn.name
                for fns in all_fns_by_name.values()
                for fn in fns
                if fn.has_body and fn.mutability in ("view", "pure") and len(fn.params) == 0
            )
            custom_mod_idents: dict[str, frozenset[str]] = {}
            for mname in mod_defs:
                cls = _classify_modifier_guard(mname, mod_defs)
                if cls is not None and cls[0].startswith("mod:") and cls[2]:
                    custom_mod_idents[cls[0]] = cls[2]
            mut_helpers: set[str] = set()
            for fns in all_fns_by_name.values():
                for hfn in fns:
                    if not hfn.has_body or hfn.mutability in ("view", "pure"):
                        continue
                    if hfn.visibility not in ("internal", "private"):
                        continue
                    hsvars = _extract_storage_vars(hfn)
                    hprim, _ = _extract_direct_surfaces(hfn, mutable_vars, all_vars)
                    if hprim or any(_is_storage_ref_mutated(hfn.body_masked, v) for v in hsvars):
                        mut_helpers.add(hfn.name)

            contexts[id(c)] = _ContractCtx(
                contract=c,
                file_facts=f_facts,
                all_vars=all_vars,
                mutable_vars=mutable_vars,
                modifier_defs=mod_defs,
                all_fns_by_name=all_fns_by_name,
                mutating_helpers=frozenset(mut_helpers),
                zero_arg_view_helpers=zero_arg_view,
                custom_mod_inline_idents=custom_mod_idents,
            )
            state_var_names_by_contract[id(c)] = frozenset(all_vars.keys())

    return _TreeIndex(
        files=parsed,
        contexts=contexts,
        contracts=contracts,
        contracts_by_name=contracts_by_name,
        workspace_libraries=frozenset(workspace_libs),
        readonly_lib_map=readonly_lib_map,
        all_readonly_set=all_readonly_set,
        all_func_stems=frozenset(all_func_stems),
        state_var_names_by_contract=state_var_names_by_contract,
    )


def _sig_names(contract: SolContractFact) -> set[str]:
    return {
        fn.name
        for fn in contract.functions
        if fn.has_body and fn.name not in _BOILERPLATE_FNS and not _PURE_REVERT_BODY_RE.match(fn.body_masked)
    }


def _match_contracts(
    upstream: _TreeIndex,
    fork: _TreeIndex,
) -> list[tuple[SolContractFact, SolContractFact]]:
    used_u: set[int] = set()
    used_f: set[int] = set()
    pairs: list[tuple[SolContractFact, SolContractFact]] = []

    # Pass 1: exact contract/library name within compatible kind group
    by_name_u: dict[tuple[str, str], list[SolContractFact]] = {}
    by_name_f: dict[tuple[str, str], list[SolContractFact]] = {}
    for uc in upstream.contracts:
        by_name_u.setdefault((_kind_group(uc.kind), uc.name), []).append(uc)
    for fc in fork.contracts:
        by_name_f.setdefault((_kind_group(fc.kind), fc.name), []).append(fc)

    for key in sorted(set(by_name_u) & set(by_name_f)):
        u_list = by_name_u[key]
        f_list = by_name_f[key]
        scored: list[tuple[int, float, int, SolContractFact, SolContractFact]] = []
        for uc in u_list:
            su = _sig_names(uc)
            for fc in f_list:
                sf = _sig_names(fc)
                union = len(su | sf)
                jacc = (len(su & sf) / union) if union else 1.0
                scored.append((_path_suffix_overlap(uc.path, fc.path), jacc, len(su & sf), uc, fc))
        scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        for _, _, _, uc, fc in scored:
            if id(uc) in used_u or id(fc) in used_f:
                continue
            used_u.add(id(uc))
            used_f.add(id(fc))
            pairs.append((uc, fc))

    # Pass 2: renamed / rebranded contracts (`AavePool` -> `ZeroPool`, `BaseHook` -> `MyHook`)
    rem_u = [c for c in upstream.contracts if id(c) not in used_u]
    rem_f = [c for c in fork.contracts if id(c) not in used_f]
    sig_cache = {id(c): _sig_names(c) for c in (*rem_u, *rem_f)}
    candidates: list[tuple[float, int, int, int, SolContractFact, SolContractFact]] = []
    for uc in rem_u:
        su = sig_cache[id(uc)]
        if not su:
            continue
        u_sfx = _camel_suffix_token(uc.name)
        for fc in rem_f:
            if _kind_group(uc.kind) != _kind_group(fc.kind):
                continue
            sf = sig_cache[id(fc)]
            if not sf:
                continue
            inter = len(su & sf)
            if inter < 2:
                continue
            jacc = inter / len(su | sf)
            base_or_sfx = 1 if (uc.name in fc.bases or (u_sfx == _camel_suffix_token(fc.name) and len(u_sfx) >= 4)) else 0
            if (inter >= 3 and jacc >= 0.45) or (base_or_sfx and inter >= 2 and jacc >= 0.35):
                candidates.append((jacc, inter, base_or_sfx, _path_suffix_overlap(uc.path, fc.path), uc, fc))
    candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)
    for _, _, _, _, uc, fc in candidates:
        if id(uc) in used_u or id(fc) in used_f:
            continue
        used_u.add(id(uc))
        used_f.add(id(fc))
        pairs.append((uc, fc))

    return pairs


def _is_stub_or_empty(fn: SolFunctionFact) -> bool:
    if not fn.has_body or not fn.body_masked.strip():
        return True
    return bool(_PURE_REVERT_BODY_RE.match(fn.body_masked))


def _match_functions(
    u_contract: SolContractFact,
    f_contract: SolContractFact,
) -> list[tuple[SolFunctionFact, SolFunctionFact]]:
    u_fns = [fn for fn in u_contract.functions if not _is_stub_or_empty(fn) and fn.name not in _BOILERPLATE_FNS]
    f_fns = [fn for fn in f_contract.functions if not _is_stub_or_empty(fn) and fn.name not in _BOILERPLATE_FNS]
    used_u: set[int] = set()
    used_f: set[int] = set()
    matched: list[tuple[SolFunctionFact, SolFunctionFact]] = []

    # Exact `(name, arity)` match first
    f_by_key: dict[tuple[str, int], list[SolFunctionFact]] = {}
    for ff in f_fns:
        f_by_key.setdefault((ff.name, len(ff.params)), []).append(ff)
    for uf in u_fns:
        cands = [ff for ff in f_by_key.get((uf.name, len(uf.params)), ()) if id(ff) not in used_f]
        if len(cands) == 1:
            ff = cands[0]
            used_u.add(id(uf))
            used_f.add(id(ff))
            matched.append((uf, ff))

    # Unique name fallback (forks adding or removing parameters)
    rem_f_by_name: dict[str, list[SolFunctionFact]] = {}
    for ff in f_fns:
        if id(ff) not in used_f:
            rem_f_by_name.setdefault(ff.name, []).append(ff)
    rem_u_by_name: dict[str, list[SolFunctionFact]] = {}
    for uf in u_fns:
        if id(uf) not in used_u:
            rem_u_by_name.setdefault(uf.name, []).append(uf)
    for name, ul in rem_u_by_name.items():
        fl = rem_f_by_name.get(name, ())
        if len(ul) == 1 and len(fl) == 1:
            used_u.add(id(ul[0]))
            used_f.add(id(fl[0]))
            matched.append((ul[0], fl[0]))

    return matched


def _collect_effective_guards(
    fn: SolFunctionFact,
    ctx: _ContractCtx,
    workspace_libraries: frozenset[str],
) -> tuple[dict[str, str], frozenset[str], bool]:
    """Return `(effective_guard_map, cond_idents, has_2hop_ext_call)` for `fn`."""
    direct = _extract_direct_guards(
        fn, ctx.modifier_defs, ctx.zero_arg_view_helpers, ctx.custom_mod_inline_idents
    )
    guards: dict[str, str] = dict(direct)
    if _SIGNATURE_AUTH_RE.search(fn.body_masked):
        guards.setdefault("caller_auth", "signature auth")
    for mod in fn.modifiers:
        low = mod.lower()
        if mod in ctx.modifier_defs:
            _, mbody = ctx.modifier_defs[mod]
            if _REQUIRE_RE.search(mbody):
                # Capture parameterized security modifiers missed by `_classify_modifier_guard`
                g_m = _classify_modifier_guard(mod, ctx.modifier_defs)
                if g_m is None and _GUARD_MOD_NAME_RE.match(mod):
                    guards.setdefault(f"mod:{mod}", mod)
                if _SIGNATURE_AUTH_RE.search(mbody):
                    guards.setdefault("caller_auth", mod)
        elif _GUARD_MOD_NAME_RE.match(mod) and f"mod:{mod}" not in guards:
            cls = _classify_modifier_guard(mod, ctx.modifier_defs)
            if cls is None and not re.search(r"(?:init|proxy|delegate)", low):
                guards.setdefault(f"mod:{mod}", mod)

    cond_idents: set[str] = set()
    for cond in _extract_inline_guard_conditions(fn.body_masked):
        cond_idents.update(_IDENT_RE.findall(cond))

    visited: set[int] = {id(fn)}
    frontier: list[tuple[SolFunctionFact, int]] = [(fn, 0)]
    while frontier:
        cur, depth = frontier.pop(0)
        if depth >= 2:
            continue
        for callee in _top_level_unconditional_callees(cur.body_masked):
            for cand in ctx.all_fns_by_name.get(callee, ()):
                if not cand.has_body or id(cand) in visited:
                    continue
                visited.add(id(cand))
                cguards = _extract_direct_guards(
                    cand, ctx.modifier_defs, ctx.zero_arg_view_helpers, ctx.custom_mod_inline_idents
                )
                for gk, gl in cguards.items():
                    guards.setdefault(gk, gl)
                if _SIGNATURE_AUTH_RE.search(cand.body_masked):
                    guards.setdefault("caller_auth", "signature auth")
                for cond in _extract_inline_guard_conditions(cand.body_masked):
                    cond_idents.update(_IDENT_RE.findall(cond))
                frontier.append((cand, depth + 1))

    has_ext = False
    ext_visited: set[int] = {id(fn)}
    ext_q: list[tuple[SolFunctionFact, int]] = [(fn, 0)]
    while ext_q:
        cur, depth = ext_q.pop(0)
        if _is_external_call_in_fn(cur, ctx.all_vars, workspace_libraries):
            has_ext = True
            break
        if depth >= 2:
            continue
        for call in cur.calls:
            if call.target_expr is not None:
                continue
            for cand in ctx.all_fns_by_name.get(call.callee_name, ()):
                if not cand.has_body or cand.visibility not in ("internal", "private") or id(cand) in ext_visited:
                    continue
                ext_visited.add(id(cand))
                ext_q.append((cand, depth + 1))

    return guards, frozenset(cond_idents), has_ext


def _fn_mutates_or_transfers(fn: SolFunctionFact, ctx: _ContractCtx) -> bool:
    if fn.mutability in ("view", "pure"):
        return False
    svars = _extract_storage_vars(fn)
    if any(_is_storage_ref_mutated(fn.body_masked, v) for v in svars):
        return True
    prim, reent = _extract_direct_surfaces(fn, ctx.mutable_vars, ctx.all_vars)
    if prim or reent or _ETH_SEND_RE.search(fn.body_masked):
        return True
    for name in ctx.all_vars:
        if _is_state_var_mutated(fn.body_masked, name):
            return True
    for call in fn.calls:
        if call.callee_name in _TRANSFER_CALL_METHODS:
            return True
        if call.target_expr is None and call.callee_name in ctx.mutating_helpers:
            return True
    return False


def _extract_effective_syncs(
    fn: SolFunctionFact,
    ctx: _ContractCtx,
    tree: _TreeIndex,
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """Return `(uncond_sync_by_stem, any_call_stems)` across fn, attached modifiers, and 2-hop helpers.

    Each entry in `uncond_sync_by_stem` maps `method_stem` -> `(display_label, target_kind)`, where
    `target_kind` is `'self'`, `'storage'`, `'lib:<Name>'`, or `'var:<name>'`.
    """
    uncond_by_stem: dict[str, tuple[str, str]] = {}
    any_stems: set[str] = set()

    def _norm_stem(method_name: str) -> str:
        return method_name.lstrip("_").lower()

    def _record_steps(calls_seq: Iterable[SolCallFact], svars: Mapping[str, str], *, is_uncond: bool) -> None:
        steps = _extract_sync_steps(
            calls_seq,
            svars,
            ctx.mutable_vars,
            ctx.all_vars,
            ctx.mutating_helpers,
            tree.readonly_lib_map,
            tree.all_readonly_set,
        )
        for skey, slabel in steps.items():
            method = skey.rsplit(".", 1)[-1]
            stem = _norm_stem(method)
            any_stems.add(stem)
            if not is_uncond:
                continue
            if skey.startswith("sync:self."):
                tkind = "self"
            else:
                tgt = skey[len("sync:") :].rsplit(".", 1)[0]
                if tgt in ctx.mutable_vars:
                    tkind = f"var:{tgt}"
                elif tgt[0].isupper() and tgt in tree.workspace_libraries:
                    tkind = f"lib:{tgt}"
                else:
                    tkind = "storage"
            uncond_by_stem.setdefault(stem, (slabel, tkind))

    svars = _extract_storage_vars(fn)
    _record_steps(_extract_unconditional_calls(fn.body_masked), svars, is_uncond=True)
    _record_steps(fn.calls, svars, is_uncond=False)
    for c in fn.calls:
        if c.callee_name and not c.callee_name[0].isupper():
            any_stems.add(_norm_stem(c.callee_name))

    # Sync steps executed inside attached modifiers (e.g. `updateReward` modifier)
    for mod in fn.modifiers:
        if mod in ctx.modifier_defs:
            _, mbody = ctx.modifier_defs[mod]
            _record_steps(_extract_unconditional_calls(mbody), {}, is_uncond=True)
            for callee in _top_level_unconditional_callees(mbody):
                if callee and not callee[0].isupper():
                    any_stems.add(_norm_stem(callee))

    # Propagate up to 2 hops through internal/private helpers
    visited: set[int] = {id(fn)}
    frontier: list[tuple[SolFunctionFact, int, bool]] = [(fn, 0, True)]
    while frontier:
        cur, depth, uncond_path = frontier.pop(0)
        if depth >= 2:
            continue
        cur_uncond = _top_level_unconditional_callees(cur.body_masked) if uncond_path else set()
        for c in cur.calls:
            if c.target_expr is not None:
                continue
            for cand in ctx.all_fns_by_name.get(c.callee_name, ()):
                if not cand.has_body or cand.visibility not in ("internal", "private") or id(cand) in visited:
                    continue
                visited.add(id(cand))
                c_svars = _extract_storage_vars(cand)
                child_uncond = uncond_path and (c.callee_name in cur_uncond)
                if child_uncond:
                    _record_steps(_extract_unconditional_calls(cand.body_masked), c_svars, is_uncond=True)
                _record_steps(cand.calls, c_svars, is_uncond=False)
                for hc in cand.calls:
                    if hc.callee_name and not hc.callee_name[0].isupper():
                        any_stems.add(_norm_stem(hc.callee_name))
                frontier.append((cand, depth + 1, child_uncond))

    return uncond_by_stem, any_stems


def _unconditional_sync_and_transfer_lines(
    fn: SolFunctionFact,
    ctx: _ContractCtx,
    tree: _TreeIndex,
) -> tuple[dict[str, list[int]], list[tuple[str, int]]]:
    """Return `(sync_stem -> [lines], [(xfer_label, line)])` for top-level unconditional calls."""
    svars = _extract_storage_vars(fn)
    uncond_calls = _extract_unconditional_calls(fn.body_masked)
    steps = _extract_sync_steps(
        uncond_calls,
        svars,
        ctx.mutable_vars,
        ctx.all_vars,
        ctx.mutating_helpers,
        tree.readonly_lib_map,
        tree.all_readonly_set,
    )
    sync_stems = {k.rsplit(".", 1)[-1].lstrip("_").lower(): k.rsplit(".", 1)[-1] for k in steps}
    sync_lines: dict[str, list[int]] = {}
    xfer_lines: list[tuple[str, int]] = []

    for c in uncond_calls:
        cname = c.callee_name
        if not cname or cname[0].isupper():
            continue
        stem = cname.lstrip("_").lower()
        if stem in sync_stems:
            sync_lines.setdefault(stem, []).append(c.line)
        if c.target_expr is not None and cname in _TRANSFER_CALL_METHODS:
            xfer_lines.append((cname, c.line))
    for m in _ETH_SEND_RE.finditer(fn.body_masked):
        # Only count top-level ETH send lines outside conditionals by checking braces before match
        prefix = fn.body_masked[: m.start()]
        if prefix.count("{") - prefix.count("}") == 0:
            line = fn.body_masked[: m.start()].count("\n") + 1
            xfer_lines.append(("ETH.transfer", line))

    return sync_lines, xfer_lines


def _rounding_polarity(fn: SolFunctionFact) -> tuple[bool, bool, bool]:
    """Return `(has_up, has_down, has_plain_div)` signals for a function body."""
    body = fn.body_masked
    up = bool(_ROUND_UP_RE.search(body))
    down = bool(_ROUND_DOWN_RE.search(body))
    if fn.name in _ERC4626_CONVERSION_FNS:
        if _ERC4626_UP_RE.search(body):
            up = True
        if _ERC4626_DOWN_RE.search(body):
            down = True
    plain_div = bool(_PLAIN_DIV_RE.search(body))
    return up, down, plain_div


def _check_dropped_or_widened_guards(
    u_fn: SolFunctionFact,
    f_fn: SolFunctionFact,
    u_ctx: _ContractCtx,
    f_ctx: _ContractCtx,
    u_tree: _TreeIndex,
    f_tree: _TreeIndex,
    f_path: str,
) -> list[Finding]:
    findings: list[Finding] = []
    u_guards, _, _ = _collect_effective_guards(u_fn, u_ctx, u_tree.workspace_libraries)
    f_guards, f_cond_idents, f_has_ext = _collect_effective_guards(f_fn, f_ctx, f_tree.workspace_libraries)
    u_is_pub = u_fn.visibility in ("external", "public")
    f_is_pub = f_fn.visibility in ("external", "public")
    f_is_ro = f_fn.mutability in ("view", "pure")

    # 1) Visibility widening (`internal`/`private` -> `external`/`public`) without caller auth
    if (
        u_fn.visibility in ("internal", "private")
        and f_is_pub
        and not f_is_ro
        and f_fn.name.lstrip("_").lower() not in _PUBLIC_CALLBACK_OR_INIT_NAMES
        and not f_fn.name.lstrip("_").lower().startswith(("init", "initialize", "reinitialize"))
        and "caller_auth" not in f_guards
        and _fn_mutates_or_transfers(f_fn, f_ctx)
    ):
        findings.append(
            Finding(
                detector="fork_dropped_guard",
                symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                path=f_path,
                start_line=f_fn.start_line,
                end_line=f_fn.end_line,
                evidence=(f"widened {u_fn.visibility} -> {f_fn.visibility} from upstream without caller auth",),
                resolution_source="static_ast",
                confidence="high",
            )
        )

    # 2) Dropped canonical or custom security guards (skip view/pure or visibility-narrowed functions)
    if u_fn.mutability in ("view", "pure") or f_is_ro:
        return findings
    if u_is_pub and not f_is_pub:
        return findings

    u_all_mod_stems = {m.lower() for m in u_fn.modifiers}
    f_all_mod_stems = {m.lower() for m in f_fn.modifiers}

    for gkey, glabel in sorted(u_guards.items()):
        if gkey in f_guards:
            continue
        if gkey.startswith(("sync:", "helper:")):
            continue
        if gkey == "reentrancy" and not f_has_ext:
            continue
        if gkey == "not_paused" and _EMERGENCY_OR_PAUSE_FN_RE.search(f_fn.name):
            continue
        if gkey.startswith("mod:"):
            mod_name = gkey.removeprefix("mod:")
            if _EMERGENCY_OR_PAUSE_FN_RE.search(f_fn.name):
                continue
            if mod_name.lower() in f_all_mod_stems:
                continue
            # Suppress if inline condition checks the same state identifiers as the upstream modifier
            eq_idents = u_ctx.custom_mod_inline_idents.get(gkey, frozenset())
            if eq_idents and (eq_idents <= f_cond_idents):
                continue
            # Suppress if upstream modifier was auth-like and fork added `caller_auth`
            if _AUTH_MOD_PREFIX_RE.match(mod_name) and "caller_auth" in f_guards:
                continue
            # Suppress if upstream modifier called a helper (like `updateReward`) and fork calls it inline
            if any(c.callee_name.lower() == mod_name.lower() for c in f_fn.calls):
                continue
        elif gkey == "deadline":
            # Only flag deadline drop if fork also retains deadline-style field/param naming context
            has_dl = any(
                p.name and p.name.lower() in {d.lower() for d in _DEADLINE_IDENTS} for p in f_fn.params
            ) or bool(f_cond_idents & _DEADLINE_IDENTS)
            if not has_dl and not any(d.lower() in u_all_mod_stems for d in _DEADLINE_IDENTS):
                continue

        findings.append(
            Finding(
                detector="fork_dropped_guard",
                symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                path=f_path,
                start_line=f_fn.start_line,
                end_line=f_fn.end_line,
                evidence=(f"dropped upstream {glabel} ({gkey}) in fork",),
                resolution_source="static_ast",
                confidence="high",
            )
        )
    return findings


def _direct_uncond_sync_stems(fn: SolFunctionFact, ctx: _ContractCtx, tree: _TreeIndex) -> set[str]:
    svars = _extract_storage_vars(fn)
    steps = _extract_sync_steps(
        _extract_unconditional_calls(fn.body_masked),
        svars,
        ctx.mutable_vars,
        ctx.all_vars,
        ctx.mutating_helpers,
        tree.readonly_lib_map,
        tree.all_readonly_set,
    )
    return {k.rsplit(".", 1)[-1].lstrip("_").lower() for k in steps}


def _check_sync_regressions(
    u_fn: SolFunctionFact,
    f_fn: SolFunctionFact,
    u_ctx: _ContractCtx,
    f_ctx: _ContractCtx,
    u_tree: _TreeIndex,
    f_tree: _TreeIndex,
    f_path: str,
) -> tuple[list[Finding], set[str]]:
    findings: list[Finding] = []
    emitted_stems: set[str] = set()
    if u_fn.mutability in ("view", "pure") or f_fn.mutability in ("view", "pure"):
        return findings, emitted_stems
    if not _fn_mutates_or_transfers(f_fn, f_ctx):
        return findings, emitted_stems

    u_uncond_sync, _ = _extract_effective_syncs(u_fn, u_ctx, u_tree)
    f_uncond_sync, f_any_stems = _extract_effective_syncs(f_fn, f_ctx, f_tree)

    for stem, (u_label, tkind) in sorted(u_uncond_sync.items()):
        if stem in f_uncond_sync or stem in f_any_stems:
            continue
        # Suppress if caller invoked another method whose name shares the sync verb stem (e.g. `updateInterestRatesAndPool`)
        if any(stem in fst or fst in stem for fst in f_any_stems if len(fst) >= 6):
            continue
        # Suppress if upstream sync helper/method was removed everywhere across the entire fork tree (removed module)
        if stem in u_tree.all_func_stems and stem not in f_tree.all_func_stems:
            continue
        # Suppress when upstream sync was on a deleted contract state variable (removed optional hook)
        if tkind.startswith("var:"):
            vname = tkind[len("var:") :]
            if vname not in f_ctx.all_vars:
                continue
        emitted_stems.add(stem)
        findings.append(
            Finding(
                detector="fork_dropped_sync",
                symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                path=f_path,
                start_line=f_fn.start_line,
                end_line=f_fn.end_line,
                evidence=(f"omits upstream unconditional state-sync call {u_label}",),
                resolution_source="static_ast",
                confidence="high",
            )
        )

    # Check order flip between unconditional sync call and unconditional transfer in matched functions
    u_sync_lines, u_xfer_lines = _unconditional_sync_and_transfer_lines(u_fn, u_ctx, u_tree)
    f_sync_lines, f_xfer_lines = _unconditional_sync_and_transfer_lines(f_fn, f_ctx, f_tree)
    if u_xfer_lines and f_xfer_lines:
        u_min_xfer = min(ln for _, ln in u_xfer_lines)
        u_max_xfer = max(ln for _, ln in u_xfer_lines)
        f_min_xfer = min(ln for _, ln in f_xfer_lines)
        f_max_xfer = max(ln for _, ln in f_xfer_lines)
        f_xfer_name = f_xfer_lines[0][0]
        for stem in sorted(set(u_sync_lines) & set(f_sync_lines)):
            ulines = u_sync_lines[stem]
            flines = f_sync_lines[stem]
            if len(ulines) != 1 or len(flines) != 1:
                continue
            u_sl = ulines[0]
            f_sl = flines[0]
            u_label = u_uncond_sync[stem][0] if stem in u_uncond_sync else f"{stem}()"
            if u_sl < u_min_xfer and f_sl > f_max_xfer:
                findings.append(
                    Finding(
                        detector="fork_sync_order_shift",
                        symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                        path=f_path,
                        start_line=f_fn.start_line,
                        end_line=f_fn.end_line,
                        evidence=(f"sync {u_label} moved after {f_xfer_name}() relative to upstream",),
                        resolution_source="static_ast",
                        confidence="high",
                    )
                )
            elif u_sl > u_max_xfer and f_sl < f_min_xfer:
                findings.append(
                    Finding(
                        detector="fork_sync_order_shift",
                        symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                        path=f_path,
                        start_line=f_fn.start_line,
                        end_line=f_fn.end_line,
                        evidence=(f"sync {u_label} moved before {f_xfer_name}() relative to upstream",),
                        resolution_source="static_ast",
                        confidence="high",
                    )
                )
    return findings, emitted_stems


def _check_rounding_shift(
    u_fn: SolFunctionFact,
    f_fn: SolFunctionFact,
    f_ctx: _ContractCtx,
    f_path: str,
) -> list[Finding]:
    u_up, u_down, _ = _rounding_polarity(u_fn)
    f_up, f_down, f_plain = _rounding_polarity(f_fn)

    reason: str | None = None
    if u_up and not u_down:
        if f_down and not f_up:
            reason = "rounding flipped UP -> DOWN relative to upstream"
        elif not f_up and not f_down and f_plain:
            reason = "upstream explicit round-UP replaced with unrounded division"
    elif u_down and not u_up:
        if f_up and not f_down:
            reason = "rounding flipped DOWN -> UP relative to upstream"

    if reason is None:
        return []
    return [
        Finding(
            detector="fork_rounding_shift",
            symbol=f"{f_ctx.contract.name}.{f_fn.name}",
            path=f_path,
            start_line=f_fn.start_line,
            end_line=f_fn.end_line,
            evidence=(reason,),
            resolution_source="static_ast",
            confidence="high",
        )
    ]


def _check_safety_param_weakened(
    u_fn: SolFunctionFact,
    f_fn: SolFunctionFact,
    f_ctx: _ContractCtx,
    f_path: str,
) -> list[Finding]:
    findings: list[Finding] = []
    # Check 1: Named struct literal fields (`amountOutMinimum: minOut` -> `amountOutMinimum: 0`, `deadline: d` -> `block.timestamp`/`type(uint256).max`)
    u_slip = {m.group(1): m.group(2).strip() for m in _SLIPPAGE_FIELD_RE.finditer(u_fn.body_masked)}
    f_slip = {m.group(1): m.group(2).strip() for m in _SLIPPAGE_FIELD_RE.finditer(f_fn.body_masked)}
    local_zeros = {
        m.group(1)
        for m in _LOCAL_VAR_SIMPLE_RE.finditer(f_fn.body_masked)
        if re.search(rf"\b{re.escape(m.group(1))}\s*=\s*(?:0+|0x0+)\s*;", f_fn.body_masked)
    }

    for fname, u_val in sorted(u_slip.items()):
        f_val = f_slip.get(fname)
        if f_val is None:
            continue
        if not _ZERO_LITERAL_RE.match(u_val) and (_ZERO_LITERAL_RE.match(f_val) or f_val in local_zeros):
            findings.append(
                Finding(
                    detector="fork_safety_param_weakened",
                    symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                    path=f_path,
                    start_line=f_fn.start_line,
                    end_line=f_fn.end_line,
                    evidence=(f"{fname} weakened from `{u_val}` to `{f_val}` in fork",),
                    resolution_source="static_ast",
                    confidence="high",
                )
            )
            return findings

    u_dl = {m.group(1): m.group(2).strip() for m in _DEADLINE_FIELD_RE.finditer(u_fn.body_masked)}
    f_dl = {m.group(1): m.group(2).strip() for m in _DEADLINE_FIELD_RE.finditer(f_fn.body_masked)}
    for fname, u_val in sorted(u_dl.items()):
        f_val = f_dl.get(fname)
        if f_val is None:
            continue
        if not _WEAK_DEADLINE_RE.match(u_val) and _WEAK_DEADLINE_RE.match(f_val):
            findings.append(
                Finding(
                    detector="fork_safety_param_weakened",
                    symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                    path=f_path,
                    start_line=f_fn.start_line,
                    end_line=f_fn.end_line,
                    evidence=(f"{fname} weakened from `{u_val}` to `{f_val}` in fork",),
                    resolution_source="static_ast",
                    confidence="high",
                )
            )
            return findings

    # Check 2: Positional call arguments to matched callee of same arity
    u_calls_by_sig: dict[tuple[str, int], list[SolCallFact]] = {}
    f_calls_by_sig: dict[tuple[str, int], list[SolCallFact]] = {}
    for c in u_fn.calls:
        u_calls_by_sig.setdefault((c.callee_name, len(c.arg_exprs)), []).append(c)
    for c in f_fn.calls:
        f_calls_by_sig.setdefault((c.callee_name, len(c.arg_exprs)), []).append(c)

    for key, ulist in sorted(u_calls_by_sig.items()):
        flist = f_calls_by_sig.get(key)
        if not flist or len(ulist) != 1 or len(flist) != 1:
            continue
        uc, fc = ulist[0], flist[0]
        for idx, (uarg, farg) in enumerate(zip(uc.arg_exprs, fc.arg_exprs)):
            u_s, f_s = uarg.strip(), farg.strip()
            if _SLIPPAGE_ARG_HINT_RE.search(u_s) and not _ZERO_LITERAL_RE.match(u_s):
                if _ZERO_LITERAL_RE.match(f_s) or f_s in local_zeros:
                    findings.append(
                        Finding(
                            detector="fork_safety_param_weakened",
                            symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                            path=f_path,
                            start_line=f_fn.start_line,
                            end_line=f_fn.end_line,
                            evidence=(f"{uc.callee_name}() arg#{idx + 1} slippage weakened `{u_s}` -> `{f_s}`",),
                            resolution_source="static_ast",
                            confidence="high",
                        )
                    )
                    return findings
            if _DEADLINE_ARG_HINT_RE.search(u_s) and not _WEAK_DEADLINE_RE.match(u_s):
                if _WEAK_DEADLINE_RE.match(f_s):
                    findings.append(
                        Finding(
                            detector="fork_safety_param_weakened",
                            symbol=f"{f_ctx.contract.name}.{f_fn.name}",
                            path=f_path,
                            start_line=f_fn.start_line,
                            end_line=f_fn.end_line,
                            evidence=(f"{uc.callee_name}() arg#{idx + 1} deadline weakened `{u_s}` -> `{f_s}`",),
                            resolution_source="static_ast",
                            confidence="high",
                        )
                    )
                    return findings
    return findings


def analyze_solidity_fork_diff(
    upstream_files: Mapping[str, str],
    fork_files: Mapping[str, str],
    *,
    include_non_prod: bool = False,
) -> tuple[list[Finding], dict[str, object]]:
    """Compare upstream Solidity tree against fork Solidity tree for dropped invariants & safety shifts."""
    u_tree = _build_tree_index(upstream_files, include_non_prod=include_non_prod)
    f_tree = _build_tree_index(fork_files, include_non_prod=include_non_prod)

    matched_contracts = _match_contracts(u_tree, f_tree)
    matched_fn_total = 0
    raw_findings: list[Finding] = []

    for u_c, f_c in matched_contracts:
        u_ctx = u_tree.contexts[id(u_c)]
        f_ctx = f_tree.contexts[id(f_c)]
        fn_pairs = _match_functions(u_c, f_c)
        matched_fn_total += len(fn_pairs)
        f_path = f_ctx.file_facts.path
        direct_u_syncs: dict[int, set[str]] = {}
        matched_f_by_name = {f_fn.name for _, f_fn in fn_pairs}
        contract_findings: list[Finding] = []
        dropped_sync_by_fn: dict[str, set[str]] = {}
        sync_findings_by_fn: list[tuple[SolFunctionFact, Finding, set[str]]] = []

        for u_fn, f_fn in fn_pairs:
            direct_u_syncs[id(u_fn)] = _direct_uncond_sync_stems(u_fn, u_ctx, u_tree)
            contract_findings.extend(_check_dropped_or_widened_guards(u_fn, f_fn, u_ctx, f_ctx, u_tree, f_tree, f_path))
            sync_f, stems = _check_sync_regressions(u_fn, f_fn, u_ctx, f_ctx, u_tree, f_tree, f_path)
            if stems:
                dropped_sync_by_fn[f_fn.name] = stems
            for row in sync_f:
                if row.detector == "fork_dropped_sync":
                    sync_findings_by_fn.append((u_fn, row, stems))
                else:
                    contract_findings.append(row)
            contract_findings.extend(_check_rounding_shift(u_fn, f_fn, f_ctx, f_path))
            contract_findings.extend(_check_safety_param_weakened(u_fn, f_fn, f_ctx, f_path))

        # Deduplicate caller-propagated `fork_dropped_sync` when a matched internal callee already reports the same stem
        for u_fn, row, _ in sync_findings_by_fn:
            callee_reported: set[str] = set()
            for cname in _top_level_unconditional_callees(u_fn.body_masked):
                if cname in matched_f_by_name and cname in dropped_sync_by_fn:
                    callee_reported.update(dropped_sync_by_fn[cname])
            u_direct = direct_u_syncs.get(id(u_fn), set())
            # Match row stem back from evidence label `... call <label>`
            row_stem = row.evidence[0].rsplit(" ", 1)[-1].rstrip("()").rsplit(".", 1)[-1].lstrip("_").lower()
            if row_stem in callee_reported and row_stem not in u_direct:
                continue
            contract_findings.append(row)

        raw_findings.extend(contract_findings)

    ranked = rank_findings(raw_findings)
    by_detector: dict[str, int] = {}
    for row in ranked:
        by_detector[row.detector] = by_detector.get(row.detector, 0) + 1

    summary: dict[str, object] = {
        "upstream_files": len(u_tree.files),
        "fork_files": len(f_tree.files),
        "matched_contracts": len(matched_contracts),
        "matched_functions": matched_fn_total,
        "findings_count": len(ranked),
        "by_detector": by_detector,
    }
    return ranked, summary
