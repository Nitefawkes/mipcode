"""Zero-dependency structural analyzer and contract-delta detector for Solidity (.sol)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .model import Finding

_IDENT_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\b")
_CONTRACT_HDR_RE = re.compile(
    r"\b(abstract\s+contract|contract|library|interface)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_FUNC_HDR_RE = re.compile(
    r"\b(function\s+([A-Za-z_$][A-Za-z0-9_$]*)|constructor|fallback|receive)\s*\("
)
_MODIFIER_HDR_RE = re.compile(r"\bmodifier\s+([A-Za-z_$][A-Za-z0-9_$]*)\b")
_STATE_DECL_SKIP_PREFIXES = (
    "using ",
    "event ",
    "error ",
    "struct ",
    "enum ",
    "type ",
    "function ",
    "modifier ",
    "constructor",
    "fallback",
    "receive",
)
_SPECIFIER_KEYWORDS = frozenset(
    {
        "public",
        "external",
        "internal",
        "private",
        "view",
        "pure",
        "payable",
        "virtual",
        "override",
        "returns",
    }
)
_SECURITY_MODIFIERS = frozenset(
    {
        "nonReentrant",
        "whenNotPaused",
        "whenPaused",
        "onlyOwner",
        "onlyAdmin",
        "onlyRole",
        "onlyGovernance",
        "onlyAuthorized",
        "onlyMinter",
        "onlyOperator",
        "initializer",
        "reinitializer",
    }
)
_TRY_KW_RE = re.compile(r"\btry\b")
_ARRAY_ALLOC_RE = re.compile(
    r"(?:\bnew\s+[A-Za-z0-9_$.]+\s*(?:\[[^\]]*\])+\s*\(|\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*new\b)"
)
_LOCAL_ARRAY_DECL_RE = re.compile(
    r"\b[A-Za-z_$][A-Za-z0-9_$.]*\s*\[\s*\]\s*memory\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*new\b"
)
_ARRAY_INDEX_WRITE_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\[\s*([A-Za-z0-9_$]+)\s*\]\s*=")
_SINK_CALL_RE = re.compile(r"\b(keccak256|sha256|abi\.encode(?:Packed|WithSelector|WithSignature|Call)?)\s*\(")
_REQUIRE_RE = re.compile(r"\b(?:require|revert)\b")


@dataclass(frozen=True)
class SolParamFact:
    """Single Solidity function or modifier parameter."""

    name: str | None
    type_str: str
    position: int


@dataclass(frozen=True)
class SolStateVarFact:
    """Contract-level state variable or constant declaration."""

    contract_name: str
    name: str
    type_str: str
    visibility: str  # "public", "internal", "private"
    is_constant: bool
    is_immutable: bool
    line: int


@dataclass(frozen=True)
class SolCallFact:
    """Call expression inside a Solidity function body."""

    callee_name: str
    target_expr: str | None
    arg_exprs: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class SolTryCatchFact:
    """Extracted try/catch block within a function body."""

    line: int
    try_body: str
    catch_bodies: tuple[str, ...]
    tail_body: str


@dataclass(frozen=True)
class SolFunctionFact:
    """Function, constructor, fallback, or receive block within a contract."""

    contract_name: str
    contract_kind: str
    name: str
    params: tuple[SolParamFact, ...]
    visibility: str
    mutability: str
    modifiers: tuple[str, ...]
    modifier_clause: str
    is_virtual: bool
    is_override: bool
    has_body: bool
    start_line: int
    end_line: int
    body_masked: str
    calls: tuple[SolCallFact, ...]
    try_catches: tuple[SolTryCatchFact, ...]
    guard_idents: frozenset[str]


@dataclass(frozen=True)
class SolContractFact:
    """Top-level contract, library, or interface facts."""

    path: str
    kind: str  # "contract", "abstract contract", "library", "interface"
    name: str
    bases: tuple[str, ...]
    start_line: int
    end_line: int
    state_vars: tuple[SolStateVarFact, ...]
    functions: tuple[SolFunctionFact, ...]
    modifier_bodies: tuple[tuple[str, tuple[SolParamFact, ...], str], ...]


@dataclass(frozen=True)
class SolFileFacts:
    """All extracted Solidity facts for one file."""

    path: str
    source: str
    masked_source: str
    contracts: tuple[SolContractFact, ...] = field(default_factory=tuple)


def strip_comments_and_strings(source: str) -> str:
    """Mask Solidity comments and string/hex literals with spaces while keeping newlines verbatim."""
    chars = list(source)
    n = len(chars)
    i = 0
    while i < n:
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            chars[i] = " "
            chars[i + 1] = " "
            i += 2
            while i < n and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        if ch == "/" and nxt == "*":
            chars[i] = " "
            chars[i + 1] = " "
            i += 2
            while i < n:
                if chars[i] == "*" and i + 1 < n and chars[i + 1] == "/":
                    chars[i] = " "
                    chars[i + 1] = " "
                    i += 2
                    break
                if chars[i] != "\n":
                    chars[i] = " "
                i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            chars[i] = " "
            i += 1
            while i < n:
                cur = chars[i]
                if cur == "\\" and i + 1 < n:
                    if cur != "\n":
                        chars[i] = " "
                    if chars[i + 1] != "\n":
                        chars[i + 1] = " "
                    i += 2
                    continue
                if cur != "\n":
                    chars[i] = " "
                i += 1
                if cur == quote:
                    break
            continue
        i += 1
    return "".join(chars)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _find_matching(text: str, open_pos: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    n = len(text)
    for idx in range(open_pos, n):
        c = text[idx]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    paren = 0
    bracket = 0
    brace = 0
    start = 0
    for idx, ch in enumerate(text):
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace = max(0, brace - 1)
        elif ch == sep and paren == 0 and bracket == 0 and brace == 0:
            parts.append(text[start:idx].strip())
            start = idx + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_params(param_text: str) -> tuple[SolParamFact, ...]:
    if not param_text.strip():
        return ()
    out: list[SolParamFact] = []
    for pos, raw in enumerate(_split_top_level(param_text, ",")):
        tokens = raw.strip().split()
        if not tokens:
            continue
        last = tokens[-1]
        # Bare type declarations like `(address, uint256)` or `(uint256[] memory)` have no identifier name.
        data_locs = {"memory", "calldata", "storage", "payable"}
        if len(tokens) >= 2 and last not in data_locs and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", last):
            # Also ignore cases like `mapping(address => uint256)` where `)` ends the token
            type_str = " ".join(tokens[:-1])
            name: str | None = last
        else:
            type_str = " ".join(tokens)
            name = None
        out.append(SolParamFact(name=name, type_str=type_str, position=pos))
    return tuple(out)


def _extract_bases(header_rest: str) -> tuple[str, ...]:
    match = re.search(r"\bis\b(.*)$", header_rest, re.DOTALL)
    if not match:
        return ()
    bases: list[str] = []
    for item in _split_top_level(match.group(1), ","):
        name_match = re.match(r"^\s*([A-Za-z_$][A-Za-z0-9_$]*)", item)
        if name_match:
            bases.append(name_match.group(1))
    return tuple(bases)


def _parse_state_var_stmt(contract_name: str, stmt: str, line: int) -> SolStateVarFact | None:
    stripped = stmt.strip()
    if not stripped or any(stripped.startswith(prefix) for prefix in _STATE_DECL_SKIP_PREFIXES):
        return None
    if stripped.startswith("("):
        return None
    lhs = _split_top_level(stripped, "=")[0].strip()
    if "(" in lhs and not lhs.startswith("mapping") and not re.search(r"\bfunction\s*\(", lhs):
        return None
    tokens = lhs.split()
    if len(tokens) < 2:
        return None
    var_name = tokens[-1]
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", var_name):
        return None
    visibility = "internal"
    for vis in ("public", "internal", "private"):
        if vis in tokens[:-1]:
            visibility = vis
            break
    is_constant = "constant" in tokens[:-1]
    is_immutable = "immutable" in tokens[:-1]
    type_tokens = [t for t in tokens[:-1] if t not in {"public", "internal", "private", "constant", "immutable"}]
    return SolStateVarFact(
        contract_name=contract_name,
        name=var_name,
        type_str=" ".join(type_tokens),
        visibility=visibility,
        is_constant=is_constant,
        is_immutable=is_immutable,
        line=line,
    )


def _extract_calls(body_masked: str, body_start_line: int) -> tuple[SolCallFact, ...]:
    calls: list[SolCallFact] = []
    # Scan for callee names followed by `(` outside control-flow keywords
    skip_kw = {
        "if",
        "for",
        "while",
        "catch",
        "switch",
        "return",
        "require",
        "assert",
        "revert",
        "emit",
        "new",
        "delete",
        "unchecked",
        "assembly",
        "try",
        "function",
        "mapping",
    }
    call_re = re.compile(r"(?:([A-Za-z0-9_$.()\[\]]+?)\.)?\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
    for m in call_re.finditer(body_masked):
        name = m.group(2)
        if name in skip_kw:
            continue
        open_paren = m.end() - 1
        close_paren = _find_matching(body_masked, open_paren, "(", ")")
        if close_paren < 0:
            continue
        arg_text = body_masked[open_paren + 1 : close_paren]
        args = tuple(_split_top_level(arg_text, ",")) if arg_text.strip() else ()
        target = m.group(1).strip() if m.group(1) else None
        line = body_start_line + body_masked.count("\n", 0, m.start())
        calls.append(SolCallFact(callee_name=name, target_expr=target, arg_exprs=args, line=line))
    return tuple(calls)


def _skip_ws(text: str, pos: int) -> int:
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


def _extract_try_catches(body_masked: str, body_start_line: int) -> tuple[SolTryCatchFact, ...]:
    results: list[SolTryCatchFact] = []
    n = len(body_masked)
    pos = 0
    while True:
        m = _TRY_KW_RE.search(body_masked, pos)
        if not m:
            break
        # Find the `try { ... }` opening brace at paren depth 0 after `try`
        idx = m.end()
        paren = 0
        try_open = -1
        while idx < n:
            c = body_masked[idx]
            if c == "(":
                paren += 1
            elif c == ")":
                paren = max(0, paren - 1)
            elif c == "{" and paren == 0:
                try_open = idx
                break
            elif c == ";" and paren == 0:
                break
            idx += 1
        if try_open < 0:
            pos = m.end()
            continue
        try_close = _find_matching(body_masked, try_open, "{", "}")
        if try_close < 0:
            pos = try_open + 1
            continue
        try_body = body_masked[try_open + 1 : try_close]
        catch_bodies: list[str] = []
        cursor = _skip_ws(body_masked, try_close + 1)
        while body_masked.startswith("catch", cursor) and (
            cursor + 5 >= n or not (body_masked[cursor + 5].isalnum() or body_masked[cursor + 5] in "_$")
        ):
            scan = cursor + 5
            c_paren = 0
            catch_open = -1
            while scan < n:
                ch = body_masked[scan]
                if ch == "(":
                    c_paren += 1
                elif ch == ")":
                    c_paren = max(0, c_paren - 1)
                elif ch == "{" and c_paren == 0:
                    catch_open = scan
                    break
                elif ch == ";" and c_paren == 0:
                    break
                scan += 1
            if catch_open < 0:
                break
            catch_close = _find_matching(body_masked, catch_open, "{", "}")
            if catch_close < 0:
                break
            catch_bodies.append(body_masked[catch_open + 1 : catch_close])
            cursor = _skip_ws(body_masked, catch_close + 1)
        if catch_bodies:
            line = body_start_line + body_masked.count("\n", 0, m.start())
            results.append(
                SolTryCatchFact(
                    line=line,
                    try_body=try_body,
                    catch_bodies=tuple(catch_bodies),
                    tail_body=body_masked[cursor:],
                )
            )
        pos = cursor if cursor > try_close else try_close + 1
    return tuple(results)


def _extract_guard_idents(body_masked: str) -> frozenset[str]:
    idents: set[str] = set()
    for m in _REQUIRE_RE.finditer(body_masked):
        if m.group(0) == "require":
            open_p = body_masked.find("(", m.end())
            if open_p >= 0:
                close_p = _find_matching(body_masked, open_p, "(", ")")
                if close_p > open_p:
                    cond = _split_top_level(body_masked[open_p + 1 : close_p], ",")[0]
                    idents.update(_IDENT_RE.findall(cond))
        else:
            # Scan preceding `if (...)` line or condition around `revert`
            line_start = body_masked.rfind("\n", 0, m.start())
            ctx_start = max(0, line_start - 180)
            snippet = body_masked[ctx_start : m.end()]
            if_match = None
            for im in re.finditer(r"\bif\s*\(", snippet):
                if_match = im
            if if_match:
                open_p = ctx_start + if_match.end() - 1
                close_p = _find_matching(body_masked, open_p, "(", ")")
                if close_p > open_p:
                    idents.update(_IDENT_RE.findall(body_masked[open_p + 1 : close_p]))
    return frozenset(idents)


def _parse_specifiers(spec_text: str) -> tuple[str, str, tuple[str, ...], bool, bool]:
    # Strip `returns (...)` so return types do not pollute modifier detection.
    cleaned = spec_text
    ret_m = re.search(r"\breturns\s*\(", cleaned)
    if ret_m:
        close_p = _find_matching(cleaned, ret_m.end() - 1, "(", ")")
        if close_p > 0:
            cleaned = cleaned[: ret_m.start()] + " " + cleaned[close_p + 1 :]
    # Strip parenthesized arguments from modifiers/override(A, B) while preserving base names
    visibility = "public"
    for vis in ("external", "public", "internal", "private"):
        if re.search(rf"\b{vis}\b", cleaned):
            visibility = vis
            break
    mutability = ""
    for mut in ("pure", "view", "payable"):
        if re.search(rf"\b{mut}\b", cleaned):
            mutability = mut
            break
    is_virtual = bool(re.search(r"\bvirtual\b", cleaned))
    is_override = bool(re.search(r"\boverride\b", cleaned))

    modifiers: list[str] = []
    i = 0
    n = len(cleaned)
    while i < n:
        if cleaned[i].isspace():
            i += 1
            continue
        m = re.match(r"([A-Za-z_$][A-Za-z0-9_$]*)", cleaned[i:])
        if m:
            tok = m.group(1)
            i += len(tok)
            i = _skip_ws(cleaned, i)
            if i < n and cleaned[i] == "(":
                close_p = _find_matching(cleaned, i, "(", ")")
                i = close_p + 1 if close_p > 0 else i + 1
            if tok not in _SPECIFIER_KEYWORDS:
                modifiers.append(tok)
        else:
            i += 1
    return visibility, mutability, tuple(modifiers), is_virtual, is_override


def _parse_contract_members(
    masked: str,
    contract_name: str,
    contract_kind: str,
    body_start: int,
    body_end: int,
) -> tuple[
    tuple[SolStateVarFact, ...],
    tuple[SolFunctionFact, ...],
    tuple[tuple[str, tuple[SolParamFact, ...], str], ...],
]:
    state_vars: list[SolStateVarFact] = []
    functions: list[SolFunctionFact] = []
    modifier_bodies: list[tuple[str, tuple[SolParamFact, ...], str]] = []

    depth = 0
    stmt_start = body_start
    idx = body_start
    while idx < body_end:
        ch = masked[idx]
        if ch == "{":
            if depth == 0:
                header = masked[stmt_start:idx].strip()
                header_pos = stmt_start + (len(masked[stmt_start:idx]) - len(masked[stmt_start:idx].lstrip()))
                close_b = _find_matching(masked, idx, "{", "}")
                if close_b < 0 or close_b > body_end:
                    break
                f_m = _FUNC_HDR_RE.match(header)
                m_m = _MODIFIER_HDR_RE.match(header) if not f_m else None
                if f_m:
                    fn_fact = _build_function_fact(
                        masked=masked,
                        header_start=header_pos,
                        term_pos=close_b,
                        body_open=idx,
                        body_close=close_b,
                        contract_name=contract_name,
                        contract_kind=contract_kind,
                    )
                    if fn_fact is not None:
                        functions.append(fn_fact)
                    idx = close_b + 1
                    stmt_start = idx
                    continue
                if m_m:
                    mod_name = m_m.group(1)
                    open_p = header.find("(", m_m.end())
                    params: tuple[SolParamFact, ...] = ()
                    if open_p >= 0:
                        close_p = _find_matching(header, open_p, "(", ")")
                        if close_p > open_p:
                            params = _parse_params(header[open_p + 1 : close_p])
                    modifier_bodies.append((mod_name, params, masked[idx + 1 : close_b]))
                    idx = close_b + 1
                    stmt_start = idx
                    continue
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                stmt_start = idx + 1
        elif ch == ";" and depth == 0:
            raw_stmt = masked[stmt_start:idx]
            stripped = raw_stmt.strip()
            header_pos = stmt_start + (len(raw_stmt) - len(raw_stmt.lstrip()))
            f_m = _FUNC_HDR_RE.match(stripped)
            if f_m:
                fn_fact = _build_function_fact(
                    masked=masked,
                    header_start=header_pos,
                    term_pos=idx,
                    body_open=None,
                    body_close=None,
                    contract_name=contract_name,
                    contract_kind=contract_kind,
                )
                if fn_fact is not None:
                    functions.append(fn_fact)
            else:
                sv = _parse_state_var_stmt(contract_name, stripped, _line_of(masked, header_pos))
                if sv is not None:
                    state_vars.append(sv)
            stmt_start = idx + 1
        idx += 1

    return tuple(state_vars), tuple(functions), tuple(modifier_bodies)


def _build_function_fact(
    masked: str,
    header_start: int,
    term_pos: int,
    body_open: int | None,
    body_close: int | None,
    contract_name: str,
    contract_kind: str,
) -> SolFunctionFact | None:
    hdr_end = body_open if body_open is not None else term_pos
    header = masked[header_start:hdr_end]
    m = _FUNC_HDR_RE.match(header.lstrip())
    if not m:
        return None
    lstrip_off = len(header) - len(header.lstrip())
    fn_name = m.group(2) if m.group(2) else m.group(1)
    open_p = header.find("(", lstrip_off + m.start())
    if open_p < 0:
        return None
    close_p = _find_matching(header, open_p, "(", ")")
    if close_p < 0:
        return None
    params = _parse_params(header[open_p + 1 : close_p])
    spec_text = header[close_p + 1 :]
    visibility, mutability, modifiers, is_virtual, is_override = _parse_specifiers(spec_text)
    if contract_kind == "interface":
        is_virtual = True
    start_line = _line_of(masked, header_start)
    end_line = _line_of(masked, term_pos)
    has_body = body_open is not None and body_close is not None
    body_masked = masked[body_open + 1 : body_close] if has_body and body_open is not None and body_close is not None else ""
    body_start_line = _line_of(masked, body_open + 1) if body_open is not None else start_line
    calls = _extract_calls(body_masked, body_start_line) if has_body else ()
    try_catches = _extract_try_catches(body_masked, body_start_line) if has_body else ()
    guard_idents = _extract_guard_idents(body_masked) if has_body else frozenset()
    return SolFunctionFact(
        contract_name=contract_name,
        contract_kind=contract_kind,
        name=fn_name,
        params=params,
        visibility=visibility,
        mutability=mutability,
        modifiers=modifiers,
        modifier_clause=spec_text,
        is_virtual=is_virtual,
        is_override=is_override,
        has_body=has_body,
        start_line=start_line,
        end_line=end_line,
        body_masked=body_masked,
        calls=calls,
        try_catches=try_catches,
        guard_idents=guard_idents,
    )


def parse_solidity_file(path: str, source: str) -> SolFileFacts:
    """Extract contract, state variable, function, call, and try/catch facts from a Solidity file."""
    masked = strip_comments_and_strings(source)
    contracts: list[SolContractFact] = []
    pos = 0
    n = len(masked)
    while pos < n:
        m = _CONTRACT_HDR_RE.search(masked, pos)
        if not m:
            break
        # Ensure we are at top level (not a keyword comment or expression nested inside braces)
        kind = " ".join(m.group(1).split())
        name = m.group(2)
        brace_pos = masked.find("{", m.end())
        semi_pos = masked.find(";", m.end())
        if brace_pos < 0 or (0 <= semi_pos < brace_pos):
            pos = m.end()
            continue
        close_pos = _find_matching(masked, brace_pos, "{", "}")
        if close_pos < 0:
            pos = brace_pos + 1
            continue
        bases = _extract_bases(masked[m.end() : brace_pos])
        state_vars, functions, modifier_bodies = _parse_contract_members(
            masked, name, kind, brace_pos + 1, close_pos
        )
        contracts.append(
            SolContractFact(
                path=path,
                kind=kind,
                name=name,
                bases=bases,
                start_line=_line_of(masked, m.start()),
                end_line=_line_of(masked, close_pos),
                state_vars=state_vars,
                functions=functions,
                modifier_bodies=modifier_bodies,
            )
        )
        pos = close_pos + 1
    return SolFileFacts(path=path, source=source, masked_source=masked, contracts=tuple(contracts))


def _ident_positions_excluding_writes(code: str, name: str) -> list[int]:
    """Return positions where `name` is read (excluding pure LHS assignment `name = expr`)."""
    positions: list[int] = []
    pat = re.compile(rf"(?<![.])\b{re.escape(name)}\b")
    n = len(code)
    for m in pat.finditer(code):
        after = _skip_ws(code, m.end())
        # Exclude plain assignment `name = ...` (but keep `name == ...`, `name += ...`)
        if after < n and code[after] == "=" and (after + 1 >= n or code[after + 1] != "="):
            continue
        positions.append(m.start())
    return positions


def _detect_try_catch_sinks(path: str, contract: SolContractFact, fn: SolFunctionFact) -> list[Finding]:
    if not fn.try_catches:
        return []
    findings: list[Finding] = []
    declared_mem_arrays = set(_LOCAL_ARRAY_DECL_RE.findall(fn.body_masked))
    for tc in fn.try_catches:
        written_arrays = {
            arr
            for arr, _idx in _ARRAY_INDEX_WRITE_RE.findall(tc.try_body)
            if arr in declared_mem_arrays
        }
        if not written_arrays:
            continue
        # Only inspect catch clauses that continue execution into `tail_body` (neither revert nor return).
        non_terminating_catches = [
            cb for cb in tc.catch_bodies if not re.search(r"\b(?:revert|return)\b", cb)
        ]
        if not non_terminating_catches:
            continue
        for arr in sorted(written_arrays):
            # If any non-terminating catch fails to populate arr[idx] or shrink/reassign arr...
            leaves_unpopulated = any(
                not re.search(rf"\b{re.escape(arr)}\s*\[", cb)
                and not re.search(rf"\b{re.escape(arr)}\s*=", cb)
                for cb in non_terminating_catches
            )
            if not leaves_unpopulated:
                continue
            # Check if tail_body compacts/resizes `arr` (e.g. `mstore(arr, ...)`) before consuming it.
            if re.search(rf"\bmstore\s*\(\s*{re.escape(arr)}\b", tc.tail_body):
                continue
            consumed = (
                any(
                    re.search(rf"\b{re.escape(arr)}\b", tc.tail_body[cm.end() : cm.end() + 160])
                    for cm in _SINK_CALL_RE.finditer(tc.tail_body)
                )
                or bool(re.search(rf"\b(?:emit|return)\b[^\n;]*\b{re.escape(arr)}\b", tc.tail_body))
            )
            if consumed:
                findings.append(
                    Finding(
                        detector="try_catch_sink_review",
                        symbol=f"{contract.name}.{fn.name}",
                        path=path,
                        start_line=tc.line,
                        end_line=fn.end_line,
                        evidence=(
                            f"try populates {arr}[i] while non-reverting catch leaves entry zero-initialized "
                            f"before {arr} reaches hash/emit/return sink",
                        ),
                        resolution_source="static_ast",
                        confidence="high",
                    )
                )
    return findings


def _collect_inheritance_descendants(
    contracts_by_name: Mapping[str, list[SolContractFact]],
) -> dict[str, set[str]]:
    children: dict[str, set[str]] = {name: set() for name in contracts_by_name}
    for name, c_list in contracts_by_name.items():
        for c in c_list:
            for base in c.bases:
                children.setdefault(base, set()).add(name)

    descendants: dict[str, set[str]] = {}
    for name in contracts_by_name:
        seen: set[str] = {name}
        stack = [name]
        while stack:
            cur = stack.pop()
            for ch in children.get(cur, ()):
                if ch not in seen:
                    seen.add(ch)
                    stack.append(ch)
        descendants[name] = seen
    return descendants


def _contract_read_texts(c: SolContractFact) -> list[str]:
    texts = [fn.body_masked + " " + fn.modifier_clause for fn in c.functions]
    texts.extend(mod_body for _, _, mod_body in c.modifier_bodies)
    return texts


def _detect_dead_state_vars(
    file_facts: SolFileFacts,
    all_files: Mapping[str, SolFileFacts],
    contracts_by_name: Mapping[str, list[SolContractFact]],
    descendants: Mapping[str, set[str]],
) -> list[Finding]:
    findings: list[Finding] = []
    # Skip `interface` and `library` utility constants; inspect contracts for unread internal/private constants or state.
    all_texts = [f.masked_source for f in all_files.values()]
    for contract in file_facts.contracts:
        if contract.kind in ("interface", "library"):
            continue
        scope_names = (
            {contract.name} if contract.kind != "abstract contract" else descendants.get(contract.name, {contract.name})
        )
        if contract.kind == "contract":
            # Include derived contracts when checking `internal` visibility, restrict `private` to declaring contract.
            derived_names = descendants.get(contract.name, {contract.name})
        else:
            derived_names = scope_names

        for sv in contract.state_vars:
            if sv.visibility == "public":
                continue
            if sv.name.startswith("__gap") or sv.name.startswith("_gap"):
                continue
            if sv.is_constant or sv.is_immutable or sv.visibility == "private":
                target_names = {contract.name} if sv.visibility == "private" else derived_names
                candidate_texts: list[str] = []
                for target_name in target_names:
                    for c_fact in contracts_by_name.get(target_name, ()):
                        candidate_texts.extend(_contract_read_texts(c_fact))
                if not any(_ident_positions_excluding_writes(txt, sv.name) for txt in candidate_texts):
                    # Double check external qualified `ContractName.CONST` across all workspace files if non-private
                    if sv.visibility != "private" and any(
                        re.search(rf"\b{re.escape(contract.name)}\.{re.escape(sv.name)}\b", txt)
                        for txt in all_texts
                    ):
                        continue
                    kind_label = "constant" if sv.is_constant else "state variable"
                    findings.append(
                        Finding(
                            detector="state_sink_review",
                            symbol=f"{contract.name}.{sv.name}",
                            path=file_facts.path,
                            start_line=sv.line,
                            end_line=sv.line,
                            evidence=(
                                f"{sv.visibility} {kind_label} {sv.name} is declared in {contract.name} "
                                "and never read in any method or modifier",
                            ),
                            resolution_source="static_ast",
                            confidence="high",
                        )
                    )
    return findings


def _detect_unused_params(path: str, contract: SolContractFact, fn: SolFunctionFact) -> list[Finding]:
    if not fn.has_body or fn.is_virtual or fn.is_override or contract.kind in ("interface", "library"):
        return []
    if fn.name in ("fallback", "receive"):
        return []
    combined = fn.body_masked + " " + fn.modifier_clause
    # Require the function body to do real state/call/return work (ignore deliberate empty no-op stubs)
    if not combined.strip() or ("=" not in fn.body_masked and "return" not in fn.body_masked and not fn.calls):
        return []
    findings: list[Finding] = []
    for p in fn.params:
        if not p.name or p.name == "_" or p.name.startswith("_unused"):
            continue
        if not re.search(rf"\b{re.escape(p.name)}\b", combined):
            findings.append(
                Finding(
                    detector="parameter_sink_review",
                    symbol=f"{contract.name}.{fn.name}",
                    path=path,
                    start_line=fn.start_line,
                    end_line=fn.end_line,
                    evidence=(
                        f"parameter {p.name} of {contract.name}.{fn.name} is never read in body or modifiers",
                    ),
                    resolution_source="static_ast",
                    confidence="high",
                )
            )
    return findings


def _detect_wrapper_and_override_drops(
    path: str,
    contract: SolContractFact,
    contracts_by_name: Mapping[str, list[SolContractFact]],
) -> list[Finding]:
    findings: list[Finding] = []
    by_name: dict[str, list[SolFunctionFact]] = {}
    for fn in contract.functions:
        by_name.setdefault(fn.name, []).append(fn)
    # Include ancestor functions for internal delegate / override modifier comparison
    ancestor_fns: dict[str, list[SolFunctionFact]] = {}
    seen_anc: set[str] = set()
    stack = list(contract.bases)
    while stack:
        b = stack.pop()
        if b in seen_anc:
            continue
        seen_anc.add(b)
        for bc in contracts_by_name.get(b, ()):
            for bfn in bc.functions:
                ancestor_fns.setdefault(bfn.name, []).append(bfn)
            stack.extend(bc.bases)

    for fn in contract.functions:
        if not fn.has_body:
            continue
        # 1. Check override modifier drops when super.<fn> is not invoked
        if fn.is_override and fn.name in ancestor_fns:
            calls_super = any(c.target_expr == "super" and c.callee_name == fn.name for c in fn.calls)
            if not calls_super:
                for base_fn in ancestor_fns[fn.name]:
                    base_sec = [m for m in base_fn.modifiers if m in _SECURITY_MODIFIERS]
                    missing = [m for m in base_sec if m not in fn.modifiers]
                    if missing:
                        findings.append(
                            Finding(
                                detector="override_modifier_review",
                                symbol=f"{contract.name}.{fn.name}",
                                path=path,
                                start_line=fn.start_line,
                                end_line=fn.end_line,
                                evidence=(
                                    f"override drops security modifier(s) {', '.join(missing)} present on "
                                    f"{base_fn.contract_name}.{base_fn.name}",
                                ),
                                resolution_source="static_ast",
                                confidence="high",
                            )
                        )
                        break

        # 2. Check delegating wrapper dropping shared named parameter to overload/internal core
        caller_param_names = {p.name: p for p in fn.params if p.name}
        if not caller_param_names:
            continue
        # Aggregate forwarded params per resolved callee so branch-specific overrides
        # (e.g. `if (unwrap) pull(this) else pull(to)`) do not false-positive when another branch forwards `to`.
        per_callee_calls: dict[int, tuple[SolFunctionFact, list[SolCallFact]]] = {}
        for call in fn.calls:
            if call.target_expr not in (None, "this"):
                continue
            candidates = [
                cand
                for cand in [*by_name.get(call.callee_name, ()), *ancestor_fns.get(call.callee_name, ())]
                if cand is not fn and cand.has_body and len(cand.params) >= len(call.arg_exprs)
            ]
            if len(candidates) != 1:
                continue
            callee = candidates[0]
            per_callee_calls.setdefault(id(callee), (callee, []))[1].append(call)

        for callee, callee_calls in per_callee_calls.values():
            callee_names = [p.name for p in callee.params]
            shared = [p for p in caller_param_names if p in callee_names]
            if len(shared) < 2:
                continue
            forwarded_ever = {
                p
                for p in shared
                if any(
                    re.search(rf"\b{re.escape(p)}\b", arg)
                    for c in callee_calls
                    for arg in c.arg_exprs
                )
            }
            dropped = [p for p in shared if p not in forwarded_ever]
            if forwarded_ever and dropped:
                first_call = callee_calls[0]
                findings.append(
                    Finding(
                        detector="delegating_wrapper_review",
                        symbol=f"{contract.name}.{fn.name}",
                        path=path,
                        start_line=first_call.line,
                        end_line=fn.end_line,
                        evidence=(
                            f"{contract.name}.{fn.name} calls {callee.contract_name}.{callee.name} "
                            f"without forwarding shared parameter(s): {', '.join(sorted(dropped))}",
                        ),
                        resolution_source="static_ast",
                        confidence="high",
                    )
                )
    return findings


def audit_solidity_workspace(files: Mapping[str, str]) -> list[Finding]:
    """Run static structural contract auditing across a mapping of relative .sol paths to sources."""
    parsed: dict[str, SolFileFacts] = {
        path: parse_solidity_file(path, text) for path, text in sorted(files.items())
    }
    contracts_by_name: dict[str, list[SolContractFact]] = {}
    for f in parsed.values():
        for c in f.contracts:
            contracts_by_name.setdefault(c.name, []).append(c)
    descendants = _collect_inheritance_descendants(contracts_by_name)

    findings: list[Finding] = []
    for path, f_facts in parsed.items():
        findings.extend(_detect_dead_state_vars(f_facts, parsed, contracts_by_name, descendants))
        for contract in f_facts.contracts:
            for fn in contract.functions:
                findings.extend(_detect_try_catch_sinks(path, contract, fn))
                findings.extend(_detect_unused_params(path, contract, fn))
            findings.extend(_detect_wrapper_and_override_drops(path, contract, contracts_by_name))
    return findings


def analyze_solidity_delta(
    before_files: Mapping[str, str],
    after_files: Mapping[str, str],
) -> tuple[list[Finding], dict[str, object]]:
    """Compare before/after Solidity trees, returning newly introduced findings plus structural guard deltas."""
    before_findings = audit_solidity_workspace(before_files)
    after_findings = audit_solidity_workspace(after_files)
    before_keys = {(f.path, f.detector, f.symbol, f.evidence) for f in before_findings}
    new_findings = [f for f in after_findings if (f.path, f.detector, f.symbol, f.evidence) not in before_keys]

    before_parsed = {p: parse_solidity_file(p, t) for p, t in before_files.items()}
    after_parsed = {p: parse_solidity_file(p, t) for p, t in after_files.items()}

    before_vars = {
        f"{c.name}.{sv.name}"
        for pf in before_parsed.values()
        for c in pf.contracts
        for sv in c.state_vars
    }
    after_vars = {
        f"{c.name}.{sv.name}"
        for pf in after_parsed.values()
        for c in pf.contracts
        for sv in c.state_vars
    }
    before_guards = {
        (c.name, fn.name): fn.guard_idents
        for pf in before_parsed.values()
        for c in pf.contracts
        for fn in c.functions
    }
    added_guards: dict[str, list[str]] = {}
    for pf in after_parsed.values():
        for c in pf.contracts:
            for fn in c.functions:
                prev = before_guards.get((c.name, fn.name), frozenset())
                diff = sorted(fn.guard_idents - prev)
                if diff:
                    added_guards[f"{c.name}.{fn.name}"] = diff

    summary: dict[str, object] = {
        "before_findings": len(before_findings),
        "after_findings": len(after_findings),
        "new_findings": len(new_findings),
        "added_state_vars": sorted(after_vars - before_vars),
        "added_guards": added_guards,
    }
    return new_findings, summary
