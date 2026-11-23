"""Zero-dependency structural analyzer and contract-delta detector for Solidity (.sol)."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .delta import _path_tier, is_vendored_path
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
_PLACEHOLDER_STATE_RE = re.compile(
    r"(?:^|_)(?:gap|igap|ugap|deprecated|deprecate|reserved|obsolete)(?:_|$|[0-9A-Z])",
    re.IGNORECASE,
)
_BITMASK_CONST_RE = re.compile(r"_(?:FLAG|FLAGS|MASK|OFFSET|SHIFT|BIT|SELECTOR|TYPEHASH|PREFIX)$")
_NATSPEC_UNUSED_RE = re.compile(r"@param\s+([A-Za-z_$][A-Za-z0-9_$]*)\b.*\b(?:not\s+used|unused|ignored|deprecated)\b", re.IGNORECASE)
_PURE_REVERT_BODY_RE = re.compile(r"^\s*revert\b[^;{}]*;\s*$")


def _sol_ident_re(name: str, *, allow_member: bool = True) -> re.Pattern[str]:
    prefix = r"(?<![A-Za-z0-9_$])" if allow_member else r"(?<![A-Za-z0-9_$.])"
    return re.compile(prefix + re.escape(name) + r"(?![A-Za-z0-9_$])")


def _norm_sol_stem(name: str) -> str:
    stem = name.lstrip("_").lower()
    for prefix in ("router", "safe", "internal", "try"):
        if stem.startswith(prefix) and len(stem) > len(prefix) + 2:
            stem = stem[len(prefix) :]
            break
    return stem


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
    pat = _sol_ident_re(name, allow_member=False)
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


def _collect_ancestor_functions(
    contract: SolContractFact,
    contracts_by_name: Mapping[str, list[SolContractFact]],
) -> dict[str, list[SolFunctionFact]]:
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
    return ancestor_fns


def _strip_named_calls(code: str, callee_name: str) -> str:
    """Blank out `callee_name(...)` argument expressions so remaining reads can be tested independently."""
    chars = list(code)
    pat = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(callee_name)}\s*\(")
    for m in pat.finditer(code):
        open_p = code.find("(", m.start())
        close_p = _find_matching(code, open_p, "(", ")") if open_p >= 0 else -1
        if close_p > open_p:
            for idx in range(open_p + 1, close_p):
                if chars[idx] != "\n":
                    chars[idx] = " "
    return "".join(chars)


def _detect_dead_state_vars(
    file_facts: SolFileFacts,
    all_files: Mapping[str, SolFileFacts],
    contracts_by_name: Mapping[str, list[SolContractFact]],
    descendants: Mapping[str, set[str]],
) -> list[Finding]:
    findings: list[Finding] = []
    # Skip `interface` and `library` utility constants; inspect contracts for unread internal/private constants or state.
    all_texts = [f.masked_source for f in all_files.values()]
    raw_lines = file_facts.source.splitlines()
    for contract in file_facts.contracts:
        if contract.kind in ("interface", "library"):
            continue
        gap_count = sum(1 for s in contract.state_vars if "gap" in s.name.lower())
        has_init_or_ctor = any(fn.name in ("constructor", "initialize") for fn in contract.functions)
        # Skip pure multi-gap storage-layout upgrade mirrors (e.g., OUSDResolutionUpgrade with igap + ugap and no init).
        if gap_count >= 2 and not has_init_or_ctor:
            continue
        scope_names = (
            {contract.name} if contract.kind != "abstract contract" else descendants.get(contract.name, {contract.name})
        )
        if contract.kind == "contract":
            # Include derived contracts when checking `internal` visibility, restrict `private` to declaring contract.
            derived_names = descendants.get(contract.name, {contract.name})
        else:
            derived_names = scope_names

        read_texts_cache: dict[str, list[str]] = {}
        contract_consts = [sv for sv in contract.state_vars if sv.is_constant]

        def _texts_for(target_set: set[str]) -> list[str]:
            key = ",".join(sorted(target_set))
            if key not in read_texts_cache:
                texts: list[str] = []
                for target_name in target_set:
                    for c_fact in contracts_by_name.get(target_name, ()):
                        texts.extend(_contract_read_texts(c_fact))
                read_texts_cache[key] = texts
            return read_texts_cache[key]

        has_read_constant = any(
            _ident_positions_excluding_writes(txt, csv.name)
            for csv in contract_consts
            for txt in _texts_for(derived_names)
        )

        for sv in contract.state_vars:
            if sv.visibility == "public":
                continue
            if _PLACEHOLDER_STATE_RE.search(sv.name):
                continue
            raw_decl_line = raw_lines[sv.line - 1] if 0 < sv.line <= len(raw_lines) else ""
            if "deprecated" in raw_decl_line.lower():
                continue
            if sv.is_constant and has_read_constant and _BITMASK_CONST_RE.search(sv.name):
                continue
            # Single-gap storage mirror contract without constructor/initialize skips uninitialized legacy non-constant slots.
            if not sv.is_constant and gap_count >= 1 and not has_init_or_ctor:
                continue
            if sv.is_constant or sv.is_immutable or sv.visibility == "private":
                target_names = {contract.name} if sv.visibility == "private" else derived_names
                candidate_texts = _texts_for(target_names)
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


def _detect_unused_params(
    path: str,
    contract: SolContractFact,
    fn: SolFunctionFact,
    ancestor_fns: Mapping[str, list[SolFunctionFact]],
    raw_lines: list[str],
) -> list[Finding]:
    if not fn.has_body or fn.is_virtual or fn.is_override or contract.kind in ("interface", "library"):
        return []
    if fn.name in ("fallback", "receive") or fn.name in ancestor_fns:
        return []
    combined = fn.body_masked + " " + fn.modifier_clause
    # Require the function body to do real state/call/return work (ignore deliberate empty no-op stubs)
    if not combined.strip() or ("=" not in fn.body_masked and "return" not in fn.body_masked and not fn.calls):
        return []
    natspec_window = "\n".join(raw_lines[max(0, fn.start_line - 36) : max(0, fn.start_line - 1)])
    documented_unused = set(_NATSPEC_UNUSED_RE.findall(natspec_window))
    findings: list[Finding] = []
    for p in fn.params:
        if not p.name or p.name == "_" or p.name.startswith("_unused") or p.name in documented_unused:
            continue
        if not _sol_ident_re(p.name).search(combined):
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
    ancestor_fns: Mapping[str, list[SolFunctionFact]],
) -> list[Finding]:
    findings: list[Finding] = []
    by_name: dict[str, list[SolFunctionFact]] = {}
    for fn in contract.functions:
        by_name.setdefault(fn.name, []).append(fn)

    for fn in contract.functions:
        if not fn.has_body:
            continue
        # 1. Check override modifier drops on state-mutating non-stub implementations when super.<fn> is not invoked
        is_stub_or_readonly = (
            fn.mutability in ("view", "pure")
            or not fn.body_masked.strip()
            or bool(_PURE_REVERT_BODY_RE.match(fn.body_masked))
        )
        if fn.is_override and fn.name in ancestor_fns and not is_stub_or_readonly:
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
        fn_stem = _norm_sol_stem(fn.name)
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
            callee_stem = _norm_sol_stem(callee.name)
            if min(len(fn_stem), len(callee_stem)) >= 4 and (
                callee_stem not in fn_stem and fn_stem not in callee_stem
            ):
                continue
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
                    _sol_ident_re(p).search(arg)
                    for c in callee_calls
                    for arg in c.arg_exprs
                )
            }
            outside_callee = _strip_named_calls(fn.body_masked, callee.name)
            dropped = [
                p
                for p in shared
                if p not in forwarded_ever and not _sol_ident_re(p).search(outside_callee)
            ]
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


_IGNORED_GUARD_MODIFIERS = frozenset(
    {
        "initializer",
        "reinitializer",
        "onlyInitializing",
        "whenPaused",
        "ifPaused",
        "onlyPaused",
        "onlyWhenPaused",
        "onlyProxy",
        "notDelegated",
        "onlyDelegateCall",
        "virtual",
        "override",
        "payable",
        "view",
        "pure",
        "public",
        "external",
        "internal",
        "private",
    }
)
_REENTRANCY_MODIFIERS = frozenset(
    {
        "nonreentrant",
        "nonreentranttransient",
        "noreentrant",
        "noreentrancy",
        "reentrancyguard",
        "lock",
        "locked",
        "mutex",
    }
)
_REENTRANCY_HELPERS = frozenset(
    {
        "_nonReentrantBefore",
        "_enterNonReentrant",
        "_nonReentrant",
        "_lock",
    }
)
_NOT_PAUSED_MODIFIERS = frozenset(
    {
        "whennotpaused",
        "notpaused",
        "ifnotpaused",
        "whenactive",
        "notfrozen",
        "whennotfrozen",
        "notshutdown",
        "whennotshutdown",
        "whenopen",
    }
)
_NOT_PAUSED_HELPERS = frozenset(
    {
        "_requireNotPaused",
        "_whenNotPaused",
        "_checkNotPaused",
        "_ensureNotPaused",
    }
)
_CALLER_AUTH_MODIFIERS = frozenset(
    {
        "onlyowner",
        "onlyadmin",
        "onlyrole",
        "onlygovernance",
        "onlyauthorized",
        "onlyminter",
        "onlyoperator",
        "onlyguardian",
        "onlymanager",
        "onlykeeper",
        "onlycontroller",
        "onlyfactory",
        "onlyrouter",
        "onlypool",
        "onlyvault",
        "onlytimelock",
        "onlyrelayer",
        "onlypauser",
        "onlyexecutor",
        "onlystrategist",
        "onlycurator",
        "onlysentinel",
        "onlytreasury",
        "onlyregistry",
        "onlybridge",
        "onlygateway",
        "onlysequencer",
        "onlyvalidator",
        "onlydeployer",
        "onlymultisig",
        "onlydao",
        "onlyhook",
        "onlyentrypoint",
        "onlyself",
        "onlythis",
        "auth",
        "restricted",
        "requiresauth",
    }
)
_CALLER_AUTH_HELPERS = frozenset(
    {
        "_checkOwner",
        "_checkRole",
        "_onlyOwner",
        "_checkAdmin",
        "_checkAuth",
        "_requireAuth",
        "_authorizeUpgrade",
        "_checkAuthorized",
        "_checkSender",
        "_validateSender",
        "_verifyCaller",
        "_isAuthorized",
        "_requireOwner",
        "_assertOwner",
    }
)
_PAUSE_STATE_IDENTS = frozenset(
    {
        "paused",
        "_paused",
        "isPaused",
        "frozen",
        "_frozen",
        "isFrozen",
        "shutdown",
        "_shutdown",
        "isShutdown",
        "emergencyShutdown",
    }
)
_DEADLINE_IDENTS = frozenset(
    {
        "deadline",
        "expiry",
        "expiration",
        "validUntil",
        "expiresAt",
        "expireAt",
        "maxTimestamp",
        "timeout",
    }
)
_TRANSFER_HELPER_LIBS = frozenset(
    {
        "TransferHelper",
        "SafeTransferLib",
        "SafeERC20",
        "ERC20Helper",
        "TokenHelper",
    }
)
_TOKEN_OUT_METHODS = frozenset(
    {
        "transfer",
        "safeTransfer",
        "approve",
        "safeApprove",
        "forceApprove",
        "safeIncreaseAllowance",
        "burn",
        "burnFrom",
    }
)
_TOKEN_IN_METHODS = frozenset({"transferFrom", "safeTransferFrom"})
_INTERNAL_ACCOUNTING_SINKS = frozenset(
    {
        "_mint",
        "_safeMint",
        "_burn",
        "_transfer",
        "_safeTransfer",
        "_update",
        "_deposit",
        "_withdraw",
        "_stake",
        "_unstake",
        "_borrow",
        "_repay",
        "_liquidate",
        "_claim",
        "_unlock",
        "_lock",
    }
)
_PURE_EXTERNAL_TARGETS = frozenset(
    {
        "this",
        "super",
        "abi",
        "type",
        "block",
        "msg",
        "tx",
        "Math",
        "SignedMath",
        "SafeCast",
        "Strings",
        "FixedPointMathLib",
        "FullMath",
        "TickMath",
        "SqrtPriceMath",
        "BitMath",
        "UnsafeMath",
        "ECDSA",
        "MessageHashUtils",
        "Base64",
        "Bytes",
        "LibString",
        "LibBit",
        "FixedPointMath",
        "UD60x18",
        "SD59x18",
        "StorageSlot",
        "Arrays",
        "BitMaps",
        "Checkpoints",
        "EnumerableSet",
        "EnumerableMap",
        "TransientSlot",
        "SlotDerivation",
        "Panic",
        "ShortStrings",
        "Timers",
        "Nonces",
        "Counters",
        "SafeMath",
    }
)
_NON_EXTERNAL_STATE_TYPES = (
    "mapping",
    "uint",
    "int",
    "bool",
    "bytes",
    "string",
    "enumerableset",
    "enumerablemap",
    "counters",
    "bitmap",
    "checkpoint",
)
_GENERIC_VIEW_HELPERS = frozenset(
    {
        "totalSupply",
        "totalAssets",
        "balanceOf",
        "decimals",
        "name",
        "symbol",
        "owner",
        "paused",
        "asset",
        "DOMAIN_SEPARATOR",
        "clock",
    }
)
_PAIRED_OP_SETS: tuple[tuple[str, str], ...] = (
    ("deposit", "withdraw"),
    ("stake", "unstake"),
    ("mint", "burn"),
    ("lock", "unlock"),
    ("borrow", "repay"),
    ("supply", "redeem"),
    ("addliquidity", "removeliquidity"),
    ("provideliquidity", "withdrawliquidity"),
    ("enter", "exit"),
    ("wrap", "unwrap"),
    ("openposition", "closeposition"),
    ("delegate", "undelegate"),
    ("claim", "emergencywithdraw"),
)
_ACTION_VARIANT_PREFIXES = ("batch", "multi", "safe", "try", "fast", "instant", "direct", "bulk")
_ACTION_VARIANT_SUFFIXES = (
    "withpermit",
    "withsignature",
    "withsig",
    "withapproval",
    "batch",
    "all",
    "eth",
    "native",
    "to",
    "for",
    "from",
    "exact",
    "v2",
)
_PUBLIC_CALLBACK_OR_INIT_NAMES = frozenset(
    {
        "initialize",
        "reinitialize",
        "init",
        "approve",
        "permit",
        "increaseallowance",
        "decreaseallowance",
        "transferfrom",
        "safetransferfrom",
        "multicall",
        "receiveflashloan",
        "onflashloan",
        "onerc721received",
        "onerc1155received",
        "onerc1155batchreceived",
        "uniswapv3swapcallback",
        "uniswapv3mintcallback",
        "flashloancallback",
    }
)
_LOCAL_DECL_RE = re.compile(
    r"\b(?:u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)?|address|bool|bytes(?:\d+)?|string)\s+"
    r"(?:(?:memory|calldata|storage|payable)\s+)*([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_AUTH_CALL_OR_MAP_RE = re.compile(
    r"(?:\b(?:hasRole|isOwner|isAdmin|isAuthorized|isAllowed|isOperator|isKeeper|isGuardian|isMinter|canCall|isApprovedForAll)\s*\(|"
    r"\b(?:authorized|admins|operators|allowed|whitelist|whitelisted|minters|managers|owners|controllers|keepers|guardians|roles|isTrusted|approved|sources|harvesters)\s*\[[^\]]*(?:msg\.sender|_msgSender))"
)
_BOOL_SENDER_MAP_RE = re.compile(
    r"^\s*!?\s*[A-Za-z_$][A-Za-z0-9_$]*\s*\[[^\]]*\b(?:msg\.sender|_msgSender\s*\(\s*\))\b[^\]]*\]\s*$"
)
_SENDER_CMP_RE = re.compile(
    r"(?:\b(?:msg\.sender|_msgSender\s*\(\s*\))\s*(?:==|!=)\s*([^&|;)]+)|"
    r"([^&|;(]+)\s*(?:==|!=)\s*\b(?:msg\.sender|_msgSender\s*\(\s*\)))"
)
_ZERO_ADDR_EXPR_RE = re.compile(r"^\s*address\s*\(\s*0\s*\)\s*$")
_ALLOWANCE_AUTH_RE = re.compile(r"\b(?:_spendAllowance|_checkApproved|isApprovedForAll|isOperator)\s*\(")
_TIMELOCK_MOD_RE = re.compile(r"(?:aftertimelock|timelock|afterdelay)", re.IGNORECASE)
_CONFIG_SETTER_RE = re.compile(
    r"^(?:set|add|remove|delete|clear|update|configure|config|revoke|submit|grant|enable|disable|register|unregister|pause|unpause|freeze|unfreeze|whitelist|blacklist)[A-Z_]"
)
_INIT_OR_CONFIG_EXEMPT_RE = re.compile(
    r"^(?:init|initialize|reinitialize|executeinit|set|submit|accept|configure|config|pause|unpause|freeze|unfreeze|emergency|rescue|forcewithdraw|ragequit|reapprove|sweep)",
    re.IGNORECASE,
)
_LIB_SYNC_EXEMPT_RE = re.compile(
    r"^(?:init|initialize|reinitialize|executeinit|set|configure|config|pause|unpause|freeze|unfreeze|emergency|rescue|forcewithdraw|ragequit)",
    re.IGNORECASE,
)
_FOUNDRY_TEST_FN_RE = re.compile(r"^(?:test(?:_|Fuzz|Fail|Fork)?|invariant_|setUp$)")
_SYNC_HELPER_NAME_RE = re.compile(
    r"^_?(?:sync|accrue|checkpoint|update|settle|refresh|snapshot|reconcile|cumulate|rebase|touch|poke|drip|compound)",
    re.IGNORECASE,
)
_EXTERNAL_INTERFACE_TYPE_RE = re.compile(r"^(?:address|payable|I[A-Z][A-Za-z0-9_$]*)$")
_READONLY_OR_PRIMITIVE_SYNC_RE = re.compile(
    r"^(?:(?:get|is|has|can|to|from|try|at|set|add|sub|mul|div|ray|wad)(?:[A-Z_0-9]|$)|"
    r"(?:preview|calculate|compute|quote|convert|encode|decode|format|parse|view|read|fetch|load|"
    r"check|verify|validate|assert|require|count|len|length|size|index|find|exists|contains|"
    r"latest|current|next|prev|max|min|total|balance|allowance|decimals|name|symbol|owner|paused|"
    r"cache|values|keys|push|pop|remove|clear|delete|increment|decrement|percent|safe|approve|"
    r"transfer|mint|burn|emit))",
)
_STORAGE_LOCAL_REF_RE = re.compile(
    r"\b([A-Za-z_$][A-Za-z0-9_$.]*)\s+storage\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_EXPLICIT_VERSION_RE = re.compile(r"^(.+?)(?:_?[vV])([1-9]\d*)$")
_TRAILING_DIGIT_VERSION_RE = re.compile(r"^([A-Za-z_]{4,})([2-9])$")
_NON_VERSION_DIGIT_STEMS = frozenset(
    {
        "erc",
        "eip",
        "sha",
        "keccak",
        "weth",
        "uint",
        "int",
        "bytes",
        "create",
        "deploycreate",
        "compute",
        "log",
        "token",
        "amount",
        "price",
        "tick",
        "sqrt",
        "pair",
        "pool",
        "hash",
        "slot",
        "word",
        "limb",
    }
)


def _is_caller_auth_cond(cond: str) -> bool:
    if _AUTH_CALL_OR_MAP_RE.search(cond) or _BOOL_SENDER_MAP_RE.match(cond):
        return True
    for m in _SENDER_CMP_RE.finditer(cond):
        other = (m.group(1) or m.group(2) or "").strip()
        if other and not _ZERO_ADDR_EXPR_RE.match(other):
            return True
    return False


def _has_global_pause_ident(cond: str) -> bool:
    for ident in _IDENT_RE.findall(cond):
        if ident in _PAUSE_STATE_IDENTS and not re.search(rf"\b{re.escape(ident)}\s*\[", cond):
            return True
    return False


_USER_SCOPED_RE = re.compile(
    r"(?:\b(?:balances|_balances|stakes|_stakes|deposits|_deposits|shares|_shares|positions|_positions|"
    r"userInfo|users|accounts|debts|borrows|collateral|rewards|staked|locked|pending|allowances|_allowances|"
    r"nonces|_nonces|credits|principal)\s*\[[^\]]*\b(?:msg\.sender|_msgSender\s*\(\s*\))\b|"
    r"\b(?:sender|owner|account|user|caller|from)\s*:\s*(?:msg\.sender|_msgSender\s*\(\s*\))\b|"
    r"\.(?:safeTransferFrom|transferFrom)\s*\(\s*(?:msg\.sender|_msgSender\s*\(\s*\))\b|"
    r"\bmsg\.value\b)",
)
_EMERGENCY_OR_PAUSE_FN_RE = re.compile(
    r"(?:^|_)(?:emergency|forcewithdraw|ragequit|rescue|unpause|pause|setpaused|freeze|unfreeze|shutdown|init|initialize|reinitialize)",
    re.IGNORECASE,
)
_ETH_SEND_RE = re.compile(
    r"(?:\.call\s*\{[^}]*\bvalue\s*:|\bsafeTransferETH\s*\(|\bsendValue\s*\(|payable\s*\([^)]*\)\s*\.(?:transfer|send)\s*\()"
)
_REENTRANT_ETH_SEND_RE = re.compile(
    r"(?:\.call\s*\{[^}]*\bvalue\s*:|\bsafeTransferETH\s*\(|\bsendValue\s*\()"
)
_LOW_LEVEL_EXT_CALL_RE = re.compile(r"\.(?:call|delegatecall|staticcall)\s*(?:\{|\()")


def _collect_ancestor_state_vars(
    contract: SolContractFact,
    contracts_by_name: Mapping[str, list[SolContractFact]],
) -> dict[str, SolStateVarFact]:
    result: dict[str, SolStateVarFact] = {sv.name: sv for sv in contract.state_vars}
    seen: set[str] = {contract.name}
    queue = list(contract.bases)
    while queue:
        base_name = queue.pop(0)
        if base_name in seen:
            continue
        seen.add(base_name)
        for base_c in contracts_by_name.get(base_name, ()):
            for sv in base_c.state_vars:
                result.setdefault(sv.name, sv)
            queue.extend(base_c.bases)
    return result


def _collect_ancestor_modifiers(
    contract: SolContractFact,
    contracts_by_name: Mapping[str, list[SolContractFact]],
) -> dict[str, tuple[tuple[SolParamFact, ...], str]]:
    result: dict[str, tuple[tuple[SolParamFact, ...], str]] = {
        name: (params, body) for name, params, body in contract.modifier_bodies
    }
    seen: set[str] = {contract.name}
    queue = list(contract.bases)
    while queue:
        base_name = queue.pop(0)
        if base_name in seen:
            continue
        seen.add(base_name)
        for base_c in contracts_by_name.get(base_name, ()):
            for name, params, body in base_c.modifier_bodies:
                result.setdefault(name, (params, body))
            queue.extend(base_c.bases)
    return result


def _extract_inline_guard_conditions(body_masked: str) -> list[str]:
    """Extract boolean guard expressions from `require(cond, ...)` and `if (cond) revert ...`."""
    conds: list[str] = []
    n = len(body_masked)
    for m in re.finditer(r"\brequire\s*\(", body_masked):
        open_p = m.end() - 1
        close_p = _find_matching(body_masked, open_p, "(", ")")
        if close_p > open_p:
            parts = _split_top_level(body_masked[open_p + 1 : close_p], ",")
            if parts and parts[0].strip():
                conds.append(parts[0].strip())
    for m in re.finditer(r"\bif\s*\(", body_masked):
        open_p = m.end() - 1
        close_p = _find_matching(body_masked, open_p, "(", ")")
        if close_p <= open_p:
            continue
        cond = body_masked[open_p + 1 : close_p].strip()
        after = _skip_ws(body_masked, close_p + 1)
        if after >= n:
            continue
        if re.match(r"revert\b", body_masked[after:]):
            conds.append(cond)
        elif body_masked[after] == "{":
            close_b = _find_matching(body_masked, after, "{", "}")
            if close_b > after:
                inner = body_masked[after + 1 : close_b]
                if (
                    len(inner) <= 220
                    and re.search(r"\brevert\b", inner)
                    and not re.search(r"\bif\s*\(", inner)
                ):
                    conds.append(cond)
    return conds


def _top_level_unconditional_callees(body_masked: str) -> set[str]:
    """Return bare callee names invoked at brace depth 0 (unconditional control flow)."""
    callees: set[str] = set()
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
    }
    depth = 0
    n = len(body_masked)
    i = 0
    while i < n:
        ch = body_masked[i]
        if ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            i += 1
        elif depth == 0 and (ch.isalpha() or ch in "_$"):
            j = i + 1
            while j < n and (body_masked[j].isalnum() or body_masked[j] in "_$"):
                j += 1
            tok = body_masked[i:j]
            prev = i - 1
            while prev >= 0 and body_masked[prev].isspace():
                prev -= 1
            nxt = _skip_ws(body_masked, j)
            if (
                tok not in skip_kw
                and nxt < n
                and body_masked[nxt] == "("
                and (prev < 0 or body_masked[prev] != ".")
            ):
                callees.add(tok)
            i = j
        else:
            i += 1
    return callees


def _extract_base_ident(expr: str) -> str | None:
    s = expr.strip()
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    cast_m = re.match(r"^(?:address|payable|I?[A-Z][A-Za-z0-9_$]*)\s*\((.+)\)$", s)
    if cast_m:
        s = cast_m.group(1).strip()
    m = re.match(r"^([A-Za-z_$][A-Za-z0-9_$]*)", s)
    return m.group(1) if m else None


def _is_state_var_mutated(body_masked: str, var_name: str) -> bool:
    esc = re.escape(var_name)
    if re.search(rf"\b(?:delete|\+\+|--)\s*{esc}\b", body_masked):
        return True
    pat = _sol_ident_re(var_name, allow_member=False)
    n = len(body_masked)
    for m in pat.finditer(body_masked):
        idx = _skip_ws(body_masked, m.end())
        # Follow optional `[...]` or `.member` chains on the state variable
        while idx < n:
            if body_masked[idx] == "[":
                close_b = _find_matching(body_masked, idx, "[", "]")
                if close_b < 0:
                    break
                idx = _skip_ws(body_masked, close_b + 1)
            elif body_masked[idx] == ".":
                mem_m = re.match(r"\.\s*([A-Za-z_$][A-Za-z0-9_$]*)", body_masked[idx:])
                if not mem_m:
                    break
                mem = mem_m.group(1)
                after_mem = _skip_ws(body_masked, idx + mem_m.end())
                if (
                    mem in {"push", "pop", "add", "remove", "set", "clear", "increment", "decrement"}
                    and after_mem < n
                    and body_masked[after_mem] == "("
                ):
                    return True
                idx = after_mem
            else:
                break
        if idx >= n:
            continue
        rest = body_masked[idx : idx + 3]
        if rest.startswith(("++", "--")):
            return True
        if rest.startswith("=") and not rest.startswith(("==", "=>")):
            return True
        if re.match(r"^(?:[+\-*/%|&^]|<<|>>)=", rest):
            return True
    return False


def _is_external_call_in_fn(
    fn: SolFunctionFact,
    state_vars: Mapping[str, SolStateVarFact],
    workspace_libraries: frozenset[str],
) -> bool:
    if _REENTRANT_ETH_SEND_RE.search(fn.body_masked) or _LOW_LEVEL_EXT_CALL_RE.search(fn.body_masked):
        return True
    for c in fn.calls:
        if not c.target_expr:
            continue
        base = _extract_base_ident(c.target_expr)
        if not base or base in _PURE_EXTERNAL_TARGETS:
            continue
        if base in _TRANSFER_HELPER_LIBS:
            return True
        if base in workspace_libraries:
            continue
        sv = state_vars.get(base)
        if sv is not None:
            tlow = sv.type_str.lower()
            if any(tok in tlow for tok in _NON_EXTERNAL_STATE_TYPES):
                continue
        # Skip value-type method helpers like `.push()`, `.pop()`, `.length`, `.add()`, `.remove()`
        if c.callee_name in {"push", "pop", "add", "remove", "contains", "length", "at", "values", "encode", "decode"}:
            continue
        return True
    return False


def _classify_modifier_guard(
    mod_name: str,
    modifier_defs: Mapping[str, tuple[tuple[SolParamFact, ...], str]],
) -> tuple[str, str, frozenset[str]] | None:
    """Return `(canonical_guard_key, display_label, inline_equiv_idents)` or None if ignored."""
    if mod_name in _IGNORED_GUARD_MODIFIERS:
        return None
    low = mod_name.lower()
    if low in _REENTRANCY_MODIFIERS:
        return ("reentrancy", mod_name, frozenset())
    if low in _NOT_PAUSED_MODIFIERS:
        return ("not_paused", mod_name, frozenset())
    if low in _CALLER_AUTH_MODIFIERS:
        return ("caller_auth", mod_name, frozenset())
    if re.search(r"(?:deadline|notexpired|beforeexpiry)", low):
        return ("deadline", mod_name, frozenset())

    mdef = modifier_defs.get(mod_name)
    if mdef is not None:
        mparams, mbody = mdef
        conds = _extract_inline_guard_conditions(mbody)
        has_revert = bool(_REQUIRE_RE.search(mbody))
        if has_revert:
            # Reentrancy lock pattern: checks a flag and writes it before and after `_;`
            if "_;" in mbody and re.search(r"\b(?:locked|_locked|_status|_entered|reentrancy)\b", mbody, re.IGNORECASE):
                return ("reentrancy", mod_name, frozenset())
            for cond in conds:
                if _is_caller_auth_cond(cond):
                    return ("caller_auth", mod_name, frozenset())
                if _has_global_pause_ident(cond):
                    return ("not_paused", mod_name, frozenset())
                cond_idents = set(_IDENT_RE.findall(cond))
                if "timestamp" in cond_idents and (cond_idents & _DEADLINE_IDENTS):
                    return ("deadline", mod_name, frozenset())
            if re.match(r"^only[A-Z_]", mod_name) and re.search(r"\b(?:msg\.sender|_msgSender|hasRole|owner|admin)\b", mbody):
                return ("caller_auth", mod_name, frozenset())
            # Custom parameterless contract-defined invariant modifier (e.g., `unlockAvailable`)
            if not mparams and not re.search(r"(?:paused|init|proxy|delegate)", low):
                cond_idents = frozenset(
                    ident
                    for cond in conds
                    for ident in _IDENT_RE.findall(cond)
                    if ident not in {"msg", "sender", "block", "timestamp", "address", "true", "false"}
                )
                return (f"mod:{mod_name}", mod_name, cond_idents)

    if re.match(r"^only[A-Z_]", mod_name):
        return ("caller_auth", mod_name, frozenset())
    return None


def _extract_direct_guards(
    fn: SolFunctionFact,
    modifier_defs: Mapping[str, tuple[tuple[SolParamFact, ...], str]],
    zero_arg_view_helpers: frozenset[str],
    custom_mod_inline_idents: Mapping[str, frozenset[str]],
) -> dict[str, str]:
    guards: dict[str, str] = {}
    for mod in fn.modifiers:
        classified = _classify_modifier_guard(mod, modifier_defs)
        if classified is not None:
            gkey, glabel, _ = classified
            guards.setdefault(gkey, glabel)

    conds = _extract_inline_guard_conditions(fn.body_masked)
    all_cond_idents: set[str] = set()
    for cond in conds:
        c_idents = set(_IDENT_RE.findall(cond))
        all_cond_idents.update(c_idents)
        if _is_caller_auth_cond(cond):
            guards.setdefault("caller_auth", "msg.sender auth")
        if _has_global_pause_ident(cond):
            guards.setdefault("not_paused", "not-paused check")
        if "timestamp" in c_idents and (c_idents & _DEADLINE_IDENTS):
            guards.setdefault("deadline", "deadline check")
        # 0-arg contract-local view/pure invariant predicate called inside guard condition
        for cm in re.finditer(r"(?<![A-Za-z0-9_$.])([A-Za-z_$][A-Za-z0-9_$]*)\s*\(\s*\)", cond):
            hname = cm.group(1)
            if hname in zero_arg_view_helpers and hname not in _GENERIC_VIEW_HELPERS:
                guards.setdefault(f"helper:{hname}", f"{hname}() check")

    # Allowance/operator authorization helpers (e.g., ERC-20/4626/7540 `_spendAllowance` or `isOperator`)
    if _ALLOWANCE_AUTH_RE.search(fn.body_masked):
        guards.setdefault("caller_auth", "allowance/operator auth")

    # Satisfy custom state-gate modifiers when the same underlying state flag is checked inline
    for gkey, req_idents in custom_mod_inline_idents.items():
        if req_idents and (req_idents <= all_cond_idents):
            guards.setdefault(gkey, gkey.removeprefix("mod:"))

    for callee in _top_level_unconditional_callees(fn.body_masked):
        if callee in _REENTRANCY_HELPERS:
            guards.setdefault("reentrancy", callee)
        elif callee in _NOT_PAUSED_HELPERS:
            guards.setdefault("not_paused", callee)
        elif callee in _CALLER_AUTH_HELPERS:
            guards.setdefault("caller_auth", callee)
    return guards


def _extract_direct_surfaces(
    fn: SolFunctionFact,
    mutable_vars: Mapping[str, SolStateVarFact],
    all_vars: Mapping[str, SolStateVarFact],
) -> tuple[set[str], set[str]]:
    """Return `(primary_surfaces, reentrancy_only_surfaces)` for one function body."""
    shadowed = {p.name for p in fn.params if p.name} | set(_LOCAL_DECL_RE.findall(fn.body_masked))
    surfaces: set[str] = set()
    reent_only: set[str] = set()

    for var_name in mutable_vars:
        if var_name in shadowed:
            continue
        if _is_state_var_mutated(fn.body_masked, var_name):
            surfaces.add(f"state:{var_name}")

    if _ETH_SEND_RE.search(fn.body_masked):
        surfaces.add("sink:eth_out")

    for call in fn.calls:
        if call.target_expr is None:
            if call.callee_name in _INTERNAL_ACCOUNTING_SINKS:
                surfaces.add(f"sink:{call.callee_name}")
            continue
        base = _extract_base_ident(call.target_expr)
        if not base:
            continue
        if base in _TRANSFER_HELPER_LIBS and call.arg_exprs:
            tok = _extract_base_ident(call.arg_exprs[0])
            if tok and tok in all_vars:
                if call.callee_name in _TOKEN_OUT_METHODS:
                    surfaces.add(f"token_out:{tok}")
                    reent_only.add(f"token_io:{tok}")
                elif call.callee_name in _TOKEN_IN_METHODS:
                    reent_only.add(f"token_io:{tok}")
        elif base in all_vars:
            if call.callee_name in _TOKEN_OUT_METHODS:
                surfaces.add(f"token_out:{base}")
                reent_only.add(f"token_io:{base}")
            elif call.callee_name in _TOKEN_IN_METHODS:
                reent_only.add(f"token_io:{base}")

    return surfaces, reent_only


def _pair_relation(name_a: str, name_b: str) -> str | None:
    """Return `'opposite'` for matched risk-equivalent pairs, `'variant'` for same-verb batch/variant pairs."""
    a = name_a.lstrip("_").lower()
    b = name_b.lstrip("_").lower()
    if a == b:
        return "variant"
    for left, right in _PAIRED_OP_SETS:
        if (a == left and b == right) or (a == right and b == left):
            return "opposite"
        if a.startswith(left) and b.startswith(right) and a[len(left) :] == b[len(right) :]:
            return "opposite"
        if a.startswith(right) and b.startswith(left) and a[len(right) :] == b[len(left) :]:
            return "opposite"

    def _verb_stem(n: str) -> str:
        s = n
        changed = True
        while changed:
            changed = False
            for pfx in _ACTION_VARIANT_PREFIXES:
                if s.startswith(pfx) and len(s) - len(pfx) >= 4:
                    s = s[len(pfx) :]
                    changed = True
                    break
            for sfx in _ACTION_VARIANT_SUFFIXES:
                if s.endswith(sfx) and len(s) - len(sfx) >= 4:
                    s = s[: -len(sfx)]
                    changed = True
                    break
        return s

    sa = _verb_stem(a)
    sb = _verb_stem(b)
    if len(sa) >= 4 and sa == sb:
        return "variant"
    return None


def _normalize_storage_type(type_str: str) -> str | None:
    if not re.search(r"\bstorage\b", type_str):
        return None
    cleaned = re.sub(r"\bstorage\b", "", type_str).strip()
    cleaned = re.sub(r"\s*=>\s*", "=>", cleaned)
    cleaned = re.sub(r"\s*\(\s*", "(", cleaned)
    cleaned = re.sub(r"\s*\)\s*", ")", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or None


def _extract_storage_vars(fn: SolFunctionFact) -> dict[str, str]:
    """Return `{var_name: canonical_storage_type}` for `storage` params and local `storage` aliases."""
    svars: dict[str, str] = {}
    for p in fn.params:
        if not p.name:
            continue
        norm_t = _normalize_storage_type(p.type_str)
        if norm_t:
            svars[p.name] = norm_t
    if svars:
        for m in _STORAGE_LOCAL_REF_RE.finditer(fn.body_masked):
            local_type, local_name, rhs_ident = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            if rhs_ident in svars and local_name not in svars:
                svars[local_name] = local_type
    return svars


def _extract_unconditional_calls(body_masked: str) -> tuple[SolCallFact, ...]:
    """Return calls occurring outside conditional/loop/try blocks (`unchecked { ... }` stays unconditional)."""
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
    # Build top-level unconditional spans by masking out `{ ... }` blocks except `unchecked { ... }`
    chars = list(body_masked)
    n = len(chars)
    i = 0
    while i < n:
        if chars[i] == "{":
            # Look backward for preceding keyword to see if this brace is `unchecked {`
            prev_end = i - 1
            while prev_end >= 0 and body_masked[prev_end].isspace():
                prev_end -= 1
            prev_start = prev_end
            while prev_start >= 0 and (body_masked[prev_start].isalnum() or body_masked[prev_start] in "_$"):
                prev_start -= 1
            prev_word = body_masked[prev_start + 1 : prev_end + 1] if prev_end >= 0 else ""
            if prev_word == "unchecked":
                chars[i] = " "
                close_b = _find_matching(body_masked, i, "{", "}")
                if close_b > i:
                    chars[close_b] = " "
                i += 1
                continue
            close_b = _find_matching(body_masked, i, "{", "}")
            if close_b < 0:
                break
            for k in range(i, close_b + 1):
                if chars[k] != "\n":
                    chars[k] = " "
            i = close_b + 1
            continue
        # Also mask single-statement `if (...) stmt;` where stmt has no `{`
        if (chars[i] == "i" and i + 1 < n and chars[i + 1] == "f"
                and (i == 0 or not (body_masked[i - 1].isalnum() or body_masked[i - 1] in "_$"))
                and (i + 2 >= n or not (body_masked[i + 2].isalnum() or body_masked[i + 2] in "_$"))):
            open_p = _skip_ws(body_masked, i + 2)
            if open_p < n and body_masked[open_p] == "(":
                close_p = _find_matching(body_masked, open_p, "(", ")")
                if close_p > open_p:
                    after_p = _skip_ws(body_masked, close_p + 1)
                    if after_p < n and body_masked[after_p] != "{":
                        semi = body_masked.find(";", after_p)
                        if semi > after_p:
                            for k in range(after_p, semi + 1):
                                if chars[k] != "\n":
                                    chars[k] = " "
                            i = semi + 1
                            continue
        i += 1
    top_text = "".join(chars)
    calls = _extract_calls(top_text, 1)
    return tuple(c for c in calls if c.callee_name not in skip_kw)


def _is_readonly_or_primitive_sync_method(
    callee_name: str,
    target_lib: str | None,
    readonly_lib_methods: Mapping[str, frozenset[str]],
    all_readonly_lib_methods: frozenset[str],
) -> bool:
    if callee_name.startswith("_"):
        stem = callee_name.lstrip("_")
        if _READONLY_OR_PRIMITIVE_SYNC_RE.match(stem):
            return True
    elif _READONLY_OR_PRIMITIVE_SYNC_RE.match(callee_name):
        return True
    if target_lib and target_lib in readonly_lib_methods and callee_name in readonly_lib_methods[target_lib]:
        return True
    if target_lib is None and callee_name in all_readonly_lib_methods:
        return True
    return False


def _is_storage_ref_mutated(body_masked: str, var_name: str) -> bool:
    """True when a `storage` struct/mapping param or local ref has its fields/entries mutated (ignores `Type storage var = rhs;`)."""
    # Strip local `Type storage var_name = ...;` initializer LHS so pointer binding is not miscounted as a write
    cleaned = re.sub(
        rf"\b[A-Za-z_$][A-Za-z0-9_$.]*\s+storage\s+{re.escape(var_name)}\s*=",
        " ",
        body_masked,
    )
    return _is_state_var_mutated(cleaned, var_name)


def _extract_sync_steps(
    calls: Iterable[SolCallFact],
    storage_vars: Mapping[str, str],
    mutable_vars: Mapping[str, SolStateVarFact],
    all_vars: Mapping[str, SolStateVarFact],
    mutating_internal_helpers: frozenset[str],
    readonly_lib_methods: Mapping[str, frozenset[str]],
    all_readonly_lib_methods: frozenset[str],
) -> dict[str, str]:
    """Extract canonical structural state-sync / accounting step calls from a call sequence."""
    steps: dict[str, str] = {}
    for call in calls:
        cname = call.callee_name
        # Solidity events and custom errors are PascalCase; state-sync functions are camelCase or `_`-prefixed
        if not cname or cname[0].isupper():
            continue
        if call.target_expr is None:
            if (
                cname in mutating_internal_helpers
                and _SYNC_HELPER_NAME_RE.match(cname)
                and cname not in _INTERNAL_ACCOUNTING_SINKS
                and cname not in _REENTRANCY_HELPERS
                and cname not in _NOT_PAUSED_HELPERS
                and cname not in _CALLER_AUTH_HELPERS
                and not _READONLY_OR_PRIMITIVE_SYNC_RE.match(cname.lstrip("_"))
            ):
                steps.setdefault(f"sync:self.{cname}", f"{cname}()")
            continue

        base = _extract_base_ident(call.target_expr)
        if not base or base in _PURE_EXTERNAL_TARGETS or base in _TRANSFER_HELPER_LIBS:
            continue

        # Form 1: `storageParam.syncMethod(...)` (library storage struct) or non-interface struct stateVar
        if base in storage_vars:
            if not _is_readonly_or_primitive_sync_method(cname, None, readonly_lib_methods, all_readonly_lib_methods):
                stype = storage_vars[base]
                short_type = stype.rsplit(".", 1)[-1]
                steps.setdefault(f"sync:{stype}.{cname}", f"{short_type}.{cname}()")
            continue
        if base in mutable_vars:
            sv = mutable_vars[base]
            # Never treat external contract interface calls (`IERC20`, `IWETH`, `address`, `IController`) as storage syncs
            t_clean = sv.type_str.strip().split()[0] if sv.type_str.strip() else ""
            if (
                t_clean
                and not _EXTERNAL_INTERFACE_TYPE_RE.match(t_clean)
                and _SYNC_HELPER_NAME_RE.match(cname)
                and not _is_readonly_or_primitive_sync_method(cname, None, readonly_lib_methods, all_readonly_lib_methods)
            ):
                steps.setdefault(f"sync:{base}.{cname}", f"{base}.{cname}()")
            continue

        # Form 2: `SomeLib.syncMethod(storageParam_or_structStateVar, ...)` (`SomeLib` must be PascalCase library, not an immutable/state var)
        if base[0].isupper() and base not in all_vars and call.arg_exprs:
            arg0 = _extract_base_ident(call.arg_exprs[0])
            if arg0 and arg0 in storage_vars:
                if not _is_readonly_or_primitive_sync_method(
                    cname, base, readonly_lib_methods, all_readonly_lib_methods
                ):
                    steps.setdefault(f"sync:{base}.{cname}", f"{base}.{cname}()")
            elif arg0 and arg0 in mutable_vars:
                sv = mutable_vars[arg0]
                t_clean = sv.type_str.strip().split()[0] if sv.type_str.strip() else ""
                if (
                    t_clean
                    and not _EXTERNAL_INTERFACE_TYPE_RE.match(t_clean)
                    and _SYNC_HELPER_NAME_RE.match(cname)
                    and not _is_readonly_or_primitive_sync_method(
                        cname, base, readonly_lib_methods, all_readonly_lib_methods
                    )
                ):
                    steps.setdefault(f"sync:{base}.{cname}", f"{base}.{cname}()")
    return steps


def _split_version_stem(name: str) -> tuple[str, int]:
    """Return `(normalized_stem, version_number)` for explicit versioned flows (`foo` <-> `fooV2`/`foo_v2`/`foo2`)."""
    stripped = name.lstrip("_")
    m = _EXPLICIT_VERSION_RE.match(stripped)
    if m:
        stem = m.group(1).rstrip("_").lower()
        ver = int(m.group(2))
        if len(stem) >= 4 and stem not in _NON_VERSION_DIGIT_STEMS:
            return (stem, ver)
    m2 = _TRAILING_DIGIT_VERSION_RE.match(stripped)
    if m2:
        stem = m2.group(1).rstrip("_").lower()
        ver = int(m2.group(2))
        if (
            len(stem) >= 4
            and stem not in _NON_VERSION_DIGIT_STEMS
            and not stem.endswith("create")
        ):
            return (stem, ver)
    return (stripped.lower(), 1)


def _surface_display(surface: str) -> str:
    for pfx in ("state:", "storage:", "token_out:", "token_io:", "sink:"):
        if surface.startswith(pfx):
            label = surface[len(pfx) :]
            return "ETH transfer" if label == "eth_out" else label
    return surface


_CROSS_LIB_EXCLUDED_SEGMENTS = frozenset({"lib", "node_modules", "vendor", "third_party"})
_UTILITY_STRUCT_CONTAINERS = frozenset(
    {
        "EnumerableSet",
        "EnumerableMap",
        "Checkpoints",
        "BitMaps",
        "Counters",
        "Timers",
        "DoubleEndedQueue",
        "Heap",
        "MerkleTree",
    }
)
_PRIMITIVE_TYPE_PREFIXES = ("uint", "int", "address", "bool", "bytes", "string", "mapping")
_NAMED_IMPORT_RE = re.compile(r"\bimport\s*\{([^}]+)\}\s*from\s*[\"']([^\"']+)[\"']\s*;")
_STAR_IMPORT_RE = re.compile(r"\bimport\s*\*\s*as\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+from\s*[\"']([^\"']+)[\"']\s*;")
_BARE_IMPORT_RE = re.compile(
    r"\bimport\s+[\"']([^\"']+)[\"'](?:\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*))?\s*;"
)
_STRUCT_DECL_RE = re.compile(r"\bstruct\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\{")


def _strip_sol_comments_only(source: str) -> str:
    """Mask `//` and `/* */` comments while preserving string literals (for import path extraction)."""
    chars = list(source)
    n = len(chars)
    i = 0
    while i < n:
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < n else ""
        if ch in ('"', "'"):
            quote = ch
            i += 1
            while i < n:
                if chars[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if chars[i] == quote:
                    i += 1
                    break
                i += 1
            continue
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
        i += 1
    return "".join(chars)


def _normalize_import_target(from_file: str, raw_target: str) -> str:
    norm_from = from_file.replace("\\", "/")
    raw = raw_target.replace("\\", "/").strip()
    if raw.startswith(("./", "../")):
        base_dir = posixpath.dirname(norm_from)
        joined = posixpath.join(base_dir, raw) if base_dir else raw
        return posixpath.normpath(joined)
    return posixpath.normpath(raw)


def _extract_sol_imports(source: str, file_path: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return `(symbol_to_resolved_path, bare_import_paths)` for a Solidity file."""
    no_comments = _strip_sol_comments_only(source)
    sym_map: dict[str, str] = {}
    bare_paths: list[str] = []
    for m in _NAMED_IMPORT_RE.finditer(no_comments):
        target = _normalize_import_target(file_path, m.group(2))
        for item in m.group(1).split(","):
            toks = item.strip().split()
            if not toks:
                continue
            if len(toks) >= 3 and toks[1] == "as":
                sym_map[toks[2]] = target
            else:
                sym_map[toks[0]] = target
    for m in _STAR_IMPORT_RE.finditer(no_comments):
        sym_map[m.group(1)] = _normalize_import_target(file_path, m.group(2))
    for m in _BARE_IMPORT_RE.finditer(no_comments):
        target = _normalize_import_target(file_path, m.group(1))
        if m.group(2):
            sym_map[m.group(2)] = target
        else:
            bare_paths.append(target)
    return sym_map, tuple(bare_paths)


def _is_production_cross_lib_path(path: str) -> bool:
    norm = path.replace("\\", "/")
    if _path_tier(norm) != 0 or is_vendored_path(norm):
        return False
    dirs = [p.lower() for p in norm.split("/")[:-1] if p]
    return not any(d in _CROSS_LIB_EXCLUDED_SEGMENTS for d in dirs)


def _resolve_cross_lib_struct_id(
    file_path: str,
    stype: str,
    file_imports: Mapping[str, tuple[dict[str, str], tuple[str, ...]]],
    file_local_structs: Mapping[str, frozenset[str]],
    file_local_containers: Mapping[str, frozenset[str]],
) -> str | None:
    """Resolve an unambiguous workspace-wide canonical struct identity, or None if unsafe/generic."""
    s = stype.strip()
    if not s or "(" in s or "[" in s or " " in s:
        return None
    low = s.lower()
    if any(low == p or low.startswith(p) and (len(low) == len(p) or low[len(p)].isdigit()) for p in _PRIMITIVE_TYPE_PREFIXES):
        return None

    norm_path = file_path.replace("\\", "/")
    sym_map, bare_imports = file_imports.get(norm_path, ({}, ()))

    if "." in s:
        container, struct_name = s.split(".", 1)
        if not container or not struct_name or container in _UTILITY_STRUCT_CONTAINERS:
            return None
        if container in sym_map:
            return f"struct:{sym_map[container]}::{container}.{struct_name}"
        if container in file_local_containers.get(norm_path, frozenset()):
            return f"struct:{norm_path}::{container}.{struct_name}"
        for bpath in bare_imports:
            if container in file_local_containers.get(bpath, frozenset()):
                return f"struct:{bpath}::{container}.{struct_name}"
        return f"qualified:{container}.{struct_name}"

    # Unqualified StructName: MUST resolve unambiguously to a shared defining file (never pool bare names)
    if s in sym_map:
        return f"struct:{sym_map[s]}::{s}"
    if s in file_local_structs.get(norm_path, frozenset()):
        return f"struct:{norm_path}::{s}"
    for bpath in bare_imports:
        if s in file_local_structs.get(bpath, frozenset()):
            return f"struct:{bpath}::{s}"
    return None


@dataclass(frozen=True)
class _CrossLibFnRecord:
    """Normalized production library function summary for cross-library storage-struct consensus."""

    path: str
    library_name: str
    fn: SolFunctionFact
    struct_surfaces: dict[str, str]  # canonical_struct_id -> display type name
    uncond_sync: dict[str, tuple[str, str]]  # sync_key -> (display_label, method_name)
    any_sync: frozenset[str]  # all sync_keys invoked anywhere (incl. conditional/2-hop)
    external_lib_calls: frozenset[tuple[str, str]]  # (TargetLib, method) calls for 2-hop cross-lib delegation
    exempt_for_sync: bool


def _detect_cross_library_guard_asymmetry(
    records: list[_CrossLibFnRecord],
    defining_libs_by_method: Mapping[str, frozenset[str]],
    intra_emitted: set[tuple[str, str, int, str]],
) -> list[Finding]:
    """Pool tier-0 production library functions across files by unambiguous `<StructType> storage` identity."""
    if len(records) < 3:
        return []

    # Index records by (library_name, fn.name) for 2-hop cross-library delegation check
    by_lib_fn: dict[tuple[str, str], list[_CrossLibFnRecord]] = {}
    for rec in records:
        by_lib_fn.setdefault((rec.library_name, rec.fn.name), []).append(rec)

    # Propagate `any_sync` across 1-2 hops of cross-library delegation so wrappers do not false-positive
    eff_any_sync_by_rec: dict[int, set[str]] = {}
    for rec in records:
        s = set(rec.any_sync)
        visited: set[int] = {id(rec)}
        frontier: list[tuple[_CrossLibFnRecord, int]] = [(rec, 0)]
        while frontier:
            cur, depth = frontier.pop(0)
            if depth >= 2:
                continue
            for target_lib, target_fn in cur.external_lib_calls:
                for cand in by_lib_fn.get((target_lib, target_fn), ()):
                    if id(cand) in visited:
                        continue
                    visited.add(id(cand))
                    s.update(cand.any_sync)
                    frontier.append((cand, depth + 1))
        eff_any_sync_by_rec[id(rec)] = s

    # Group records by canonical struct identity
    groups_by_struct: dict[str, list[_CrossLibFnRecord]] = {}
    for rec in records:
        for sid in rec.struct_surfaces:
            groups_by_struct.setdefault(sid, []).append(rec)

    # Deduplicate findings across struct buckets by (path, symbol, start_line, method_name)
    hits: dict[
        tuple[str, str, int, str],
        tuple[_CrossLibFnRecord, str, set[str], set[str]],
    ] = {}

    for sid, raw_group in sorted(groups_by_struct.items()):
        # Must span at least 2 distinct libraries/files to be a cross-library pool
        if len({(r.path, r.library_name) for r in raw_group}) < 2:
            continue

        # Partition by visibility tier (`external`/`public` entry points vs `internal` helper libraries)
        # so low-level `using Lib for Struct` internal primitives never mix with high-level `external` logic facets.
        ext_group = [r for r in raw_group if r.fn.visibility in ("external", "public")]
        int_group = [r for r in raw_group if r.fn.visibility == "internal"]
        subgroups = [g for g in (ext_group, int_group) if len(g) >= 3 and len({(r.path, r.library_name) for r in g}) >= 2]

        for group in subgroups:
            n = len(group)
            guard_members: dict[str, list[_CrossLibFnRecord]] = {}
            guard_meta: dict[str, tuple[str, str]] = {}
            for rec in group:
                for skey, (slabel, mname) in rec.uncond_sync.items():
                    if not skey.startswith(f"xsync:{sid}."):
                        continue
                    guard_members.setdefault(skey, []).append(rec)
                    guard_meta.setdefault(skey, (slabel, mname))

            for skey, guarded in guard_members.items():
                if len(guarded) < 2:
                    continue
                slabel, mname = guard_meta[skey]
                def_libs = defining_libs_by_method.get(mname, frozenset())
                # Exclude defining primitive libraries (e.g., library that implements `syncRates`) from consensus denominator & targets
                pool = [r for r in group if r.library_name not in def_libs]
                pool_n = len(pool)
                guarded_pool = [r for r in guarded if r.library_name not in def_libs]
                if pool_n < 3 or len(guarded_pool) < 2 or len(guarded_pool) * 3 < pool_n * 2:
                    continue
                guarded_ids = {id(r) for r in guarded_pool}
                peer_names = sorted({f"{r.library_name}.{r.fn.name}" for r in guarded_pool})
                for rec in pool:
                    if id(rec) in guarded_ids:
                        continue
                    if rec.exempt_for_sync:
                        continue
                    if skey in eff_any_sync_by_rec[id(rec)]:
                        continue
                    sym = f"{rec.library_name}.{rec.fn.name}"
                    if (rec.path, sym, rec.fn.start_line, mname) in intra_emitted:
                        continue
                    hkey = (rec.path, sym, rec.fn.start_line, mname)
                    surf_disp = rec.struct_surfaces[sid]
                    if hkey not in hits:
                        hits[hkey] = (rec, slabel, {surf_disp}, set(peer_names))
                    else:
                        hits[hkey][2].add(surf_disp)
                        hits[hkey][3].update(peer_names)

    findings: list[Finding] = []
    for (fpath, sym, sline, mname), (rec, slabel, surfs, peers) in sorted(hits.items()):
        peer_list = ", ".join(sorted(peers)[:3])
        if len(peers) > 3:
            peer_list += f" (+{len(peers) - 3})"
        surf_list = ", ".join(sorted(surfs)[:2])
        findings.append(
            Finding(
                detector="sibling_guard_asymmetry",
                symbol=sym,
                path=fpath,
                start_line=rec.fn.start_line,
                end_line=rec.fn.end_line,
                evidence=(
                    f"omits state-sync call {slabel} invoked by sibling(s) {peer_list} on {surf_list}",
                ),
                resolution_source="static_ast",
                confidence="high",
            )
        )
    return findings


def _detect_sibling_guard_asymmetry(
    path: str,
    contract: SolContractFact,
    contracts_by_name: Mapping[str, list[SolContractFact]],
    ancestor_fns: Mapping[str, list[SolFunctionFact]],
    workspace_libraries: frozenset[str],
    readonly_lib_methods: Mapping[str, frozenset[str]] = {},
    all_readonly_lib_methods: frozenset[str] = frozenset(),
    cross_lib_collector: list[_CrossLibFnRecord] | None = None,
    file_imports: Mapping[str, tuple[dict[str, str], tuple[str, ...]]] = {},
    file_local_structs: Mapping[str, frozenset[str]] = {},
    file_local_containers: Mapping[str, frozenset[str]] = {},
    intra_emitted: set[tuple[str, str, int, str]] | None = None,
) -> list[Finding]:
    if contract.kind == "interface":
        return []
    is_library = contract.kind == "library"

    all_vars = _collect_ancestor_state_vars(contract, contracts_by_name)
    mutable_vars = {
        name: sv for name, sv in all_vars.items() if not sv.is_constant and not sv.is_immutable
    }
    modifier_defs = _collect_ancestor_modifiers(contract, contracts_by_name)

    # Index all contract + ancestor functions by name for helper/delegation resolution
    all_fns_by_name: dict[str, list[SolFunctionFact]] = {}
    for fn in contract.functions:
        all_fns_by_name.setdefault(fn.name, []).append(fn)
    for name, fns in ancestor_fns.items():
        all_fns_by_name.setdefault(name, []).extend(fns)

    zero_arg_view_helpers = frozenset(
        fn.name
        for fns in all_fns_by_name.values()
        for fn in fns
        if fn.has_body and fn.mutability in ("view", "pure") and len(fn.params) == 0
    )

    # Pre-collect inline-equivalent state identifiers for custom contract-defined modifiers
    custom_mod_inline_idents: dict[str, frozenset[str]] = {}
    for mod_name in modifier_defs:
        classified = _classify_modifier_guard(mod_name, modifier_defs)
        if classified is not None and classified[0].startswith("mod:") and classified[2]:
            custom_mod_inline_idents[classified[0]] = classified[2]

    # Identify intra-contract/library internal/private helpers that mutate state or storage params
    mutating_internal_helpers: set[str] = set()
    helper_mutated_state_vars: dict[str, set[str]] = {}
    for fns in all_fns_by_name.values():
        for hfn in fns:
            if not hfn.has_body or hfn.mutability in ("view", "pure"):
                continue
            if hfn.visibility not in ("internal", "private"):
                continue
            h_svars = _extract_storage_vars(hfn)
            h_prim, _ = _extract_direct_surfaces(hfn, mutable_vars, all_vars)
            mutates_storage_param = any(
                _is_storage_ref_mutated(hfn.body_masked, sv_name) for sv_name in h_svars
            )
            if h_prim or mutates_storage_param:
                mutating_internal_helpers.add(hfn.name)
                h_state_only = {s for s in h_prim if s.startswith("state:")}
                if h_state_only and len(h_state_only) == len(h_prim) and not mutates_storage_param:
                    helper_mutated_state_vars.setdefault(hfn.name, set()).update(h_state_only)
    mutating_internal_set = frozenset(mutating_internal_helpers)

    allowed_vis = ("external", "public", "internal") if is_library else ("external", "public")
    raw_eligible: list[SolFunctionFact] = [
        fn
        for fn in contract.functions
        if fn.has_body
        and fn.visibility in allowed_vis
        and fn.name not in ("constructor", "receive", "fallback")
        and not fn.name.lstrip("_").lower().startswith(("init", "initialize", "reinitialize"))
        and not _FOUNDRY_TEST_FN_RE.match(fn.name)
        and fn.mutability not in ("view", "pure")
        and fn.body_masked.strip()
        and not _PURE_REVERT_BODY_RE.match(fn.body_masked)
    ]
    if is_library:
        # In libraries, keep only functions accepting a `storage` parameter, and exclude
        # `_`-prefixed internal sub-helpers that are called by sibling functions in the same library.
        called_within_lib = {c.callee_name for fn in raw_eligible for c in fn.calls if c.target_expr is None}
        eligible = [
            fn
            for fn in raw_eligible
            if _extract_storage_vars(fn)
            and not (fn.visibility == "internal" and fn.name.startswith("_") and fn.name in called_within_lib)
        ]
    else:
        eligible = raw_eligible

    collect_cross_lib = (
        is_library and cross_lib_collector is not None and _is_production_cross_lib_path(path)
    )
    if not eligible or (len(eligible) < 2 and not collect_cross_lib):
        return []

    # Precompute direct facts for every function reachable in contract + ancestors
    direct_guards_cache: dict[int, dict[str, str]] = {}
    direct_surfaces_cache: dict[int, tuple[set[str], set[str]]] = {}
    direct_ext_cache: dict[int, bool] = {}
    direct_user_scoped_cache: dict[int, bool] = {}
    direct_uncond_sync_cache: dict[int, dict[str, str]] = {}
    direct_any_sync_cache: dict[int, set[str]] = {}
    direct_storage_vars_cache: dict[int, dict[str, str]] = {}

    def _storage_mutated_surfaces(fn: SolFunctionFact, svars: Mapping[str, str]) -> set[str]:
        out: set[str] = set()
        if not svars:
            return out
        for vname, stype in svars.items():
            if _is_storage_ref_mutated(fn.body_masked, vname):
                out.add(f"storage:{stype}")
        for call in fn.calls:
            if not call.target_expr or not call.callee_name or call.callee_name[0].isupper():
                continue
            base = _extract_base_ident(call.target_expr)
            if not base or base in _PURE_EXTERNAL_TARGETS or base in _TRANSFER_HELPER_LIBS:
                continue
            if base in svars:
                if not _is_readonly_or_primitive_sync_method(
                    call.callee_name, None, readonly_lib_methods, all_readonly_lib_methods
                ):
                    out.add(f"storage:{svars[base]}")
            elif base[0].isupper() and call.arg_exprs:
                arg0 = _extract_base_ident(call.arg_exprs[0])
                if arg0 and arg0 in svars:
                    if not _is_readonly_or_primitive_sync_method(
                        call.callee_name, base, readonly_lib_methods, all_readonly_lib_methods
                    ):
                        out.add(f"storage:{svars[arg0]}")
        return out

    def _ensure_direct(fn: SolFunctionFact) -> None:
        fid = id(fn)
        if fid in direct_guards_cache:
            return
        svars = _extract_storage_vars(fn)
        direct_storage_vars_cache[fid] = svars
        direct_guards_cache[fid] = _extract_direct_guards(
            fn, modifier_defs, zero_arg_view_helpers, custom_mod_inline_idents
        )
        prim, reent = _extract_direct_surfaces(fn, mutable_vars, all_vars)
        if is_library:
            prim |= _storage_mutated_surfaces(fn, svars)
        direct_surfaces_cache[fid] = (prim, reent)
        direct_ext_cache[fid] = _is_external_call_in_fn(fn, all_vars, workspace_libraries)
        direct_user_scoped_cache[fid] = bool(_USER_SCOPED_RE.search(fn.body_masked))
        uncond_calls = _extract_unconditional_calls(fn.body_masked)
        direct_uncond_sync_cache[fid] = _extract_sync_steps(
            uncond_calls,
            svars,
            mutable_vars,
            all_vars,
            mutating_internal_set,
            readonly_lib_methods,
            all_readonly_lib_methods,
        )
        all_sync = _extract_sync_steps(
            fn.calls,
            svars,
            mutable_vars,
            all_vars,
            mutating_internal_set,
            readonly_lib_methods,
            all_readonly_lib_methods,
        )
        direct_any_sync_cache[fid] = set(all_sync.keys())

    # Resolve effective guards, sync steps, surfaces (2-hop internal helpers), and ext calls
    eff_guards: dict[int, dict[str, str]] = {}
    eff_any_sync: dict[int, set[str]] = {}
    eff_surfaces: dict[int, set[str]] = {}
    eff_reent_surfaces: dict[int, set[str]] = {}
    eff_ext_call: dict[int, bool] = {}
    eff_user_scoped: dict[int, bool] = {}

    for fn in eligible:
        _ensure_direct(fn)
        guards = dict(direct_guards_cache[id(fn)])
        for sk, sl in direct_uncond_sync_cache[id(fn)].items():
            guards.setdefault(sk, sl)
        any_sync = set(direct_any_sync_cache[id(fn)])
        prim, reent = set(direct_surfaces_cache[id(fn)][0]), set(direct_surfaces_cache[id(fn)][1])
        has_ext = direct_ext_cache[id(fn)]
        user_scoped = direct_user_scoped_cache[id(fn)]

        # 1) Guard & unconditional sync inheritance via unconditional intra-contract / inherited calls (depth <= 2)
        g_visited: set[int] = {id(fn)}
        g_frontier: list[tuple[SolFunctionFact, int]] = [(fn, 0)]
        while g_frontier:
            cur, depth = g_frontier.pop(0)
            if depth >= 2:
                continue
            for callee_name in _top_level_unconditional_callees(cur.body_masked):
                for cand in all_fns_by_name.get(callee_name, ()):
                    if not cand.has_body or id(cand) in g_visited:
                        continue
                    g_visited.add(id(cand))
                    _ensure_direct(cand)
                    for gk, gl in direct_guards_cache[id(cand)].items():
                        guards.setdefault(gk, gl)
                    for sk, sl in direct_uncond_sync_cache[id(cand)].items():
                        guards.setdefault(sk, sl)
                    any_sync.update(direct_any_sync_cache[id(cand)])
                    g_frontier.append((cand, depth + 1))

        # 2) Surface, conditional-sync satisfaction & external-call propagation via internal/private helpers (depth <= 2)
        s_visited: set[int] = {id(fn)}
        s_frontier: list[tuple[SolFunctionFact, int]] = [(fn, 0)]
        while s_frontier:
            cur, depth = s_frontier.pop(0)
            if depth >= 2:
                continue
            for call in cur.calls:
                if call.target_expr is not None:
                    continue
                for cand in all_fns_by_name.get(call.callee_name, ()):
                    if (
                        not cand.has_body
                        or cand.visibility not in ("internal", "private")
                        or id(cand) in s_visited
                    ):
                        continue
                    s_visited.add(id(cand))
                    _ensure_direct(cand)
                    c_prim, c_reent = direct_surfaces_cache[id(cand)]
                    prim.update(c_prim)
                    reent.update(c_reent)
                    any_sync.update(direct_any_sync_cache[id(cand)])
                    if direct_ext_cache[id(cand)]:
                        has_ext = True
                    if direct_user_scoped_cache[id(cand)]:
                        user_scoped = True
                    s_frontier.append((cand, depth + 1))

        eff_guards[id(fn)] = guards
        eff_any_sync[id(fn)] = any_sync
        eff_surfaces[id(fn)] = prim
        eff_reent_surfaces[id(fn)] = prim | reent
        eff_ext_call[id(fn)] = has_ext
        eff_user_scoped[id(fn)] = user_scoped

    if collect_cross_lib and cross_lib_collector is not None:
        for fn in eligible:
            struct_surfaces: dict[str, str] = {}
            stype_to_sid: dict[str, str] = {}
            for s in eff_surfaces[id(fn)]:
                if not s.startswith("storage:"):
                    continue
                stype = s[len("storage:") :]
                sid = _resolve_cross_lib_struct_id(
                    path, stype, file_imports, file_local_structs, file_local_containers
                )
                if sid is not None:
                    struct_surfaces[sid] = stype
                    stype_to_sid[stype] = sid
            if not struct_surfaces:
                continue

            # Map unconditional & any-depth sync calls on storage structs to canonical `xsync:<sid>.<method>` keys
            # so Form 1 (`pool.syncRates()`) and Form 2 (`RateLib.syncRates(pool)`) match cleanly across libraries.
            def _map_cross_sync(calls_seq: Iterable[SolCallFact], svars: Mapping[str, str]) -> dict[str, tuple[str, str]]:
                out_sync: dict[str, tuple[str, str]] = {}
                for c in calls_seq:
                    cname = c.callee_name
                    if not cname or cname[0].isupper() or not c.target_expr:
                        continue
                    base = _extract_base_ident(c.target_expr)
                    if not base or base in _PURE_EXTERNAL_TARGETS or base in _TRANSFER_HELPER_LIBS:
                        continue
                    if base in svars:
                        stype = svars[base]
                        sid = stype_to_sid.get(stype) or _resolve_cross_lib_struct_id(
                            path, stype, file_imports, file_local_structs, file_local_containers
                        )
                        if sid and not _is_readonly_or_primitive_sync_method(
                            cname, None, readonly_lib_methods, all_readonly_lib_methods
                        ):
                            short_t = stype.rsplit(".", 1)[-1]
                            out_sync.setdefault(f"xsync:{sid}.{cname}", (f"{short_t}.{cname}()", cname))
                    elif base[0].isupper() and base not in all_vars and c.arg_exprs:
                        arg0 = _extract_base_ident(c.arg_exprs[0])
                        if arg0 and arg0 in svars:
                            stype = svars[arg0]
                            sid = stype_to_sid.get(stype) or _resolve_cross_lib_struct_id(
                                path, stype, file_imports, file_local_structs, file_local_containers
                            )
                            if sid and not _is_readonly_or_primitive_sync_method(
                                cname, base, readonly_lib_methods, all_readonly_lib_methods
                            ):
                                out_sync.setdefault(f"xsync:{sid}.{cname}", (f"{base}.{cname}()", cname))
                return out_sync

            uncond_xsync = _map_cross_sync(_extract_unconditional_calls(fn.body_masked), direct_storage_vars_cache[id(fn)])
            any_xsync_map = _map_cross_sync(fn.calls, direct_storage_vars_cache[id(fn)])
            any_xsync = set(any_xsync_map.keys())

            # Propagate unconditional & conditional cross-sync steps from 1-2 hop intra-library helpers
            x_visited: set[int] = {id(fn)}
            x_frontier: list[tuple[SolFunctionFact, int, bool]] = [(fn, 0, True)]
            ext_lib_calls: set[tuple[str, str]] = set()
            while x_frontier:
                cur, depth, is_uncond_path = x_frontier.pop(0)
                cur_uncond_names = _top_level_unconditional_callees(cur.body_masked) if is_uncond_path else set()
                for c in cur.calls:
                    if c.target_expr is not None:
                        base = _extract_base_ident(c.target_expr)
                        if base and base in workspace_libraries and base != contract.name:
                            ext_lib_calls.add((base, c.callee_name))
                        continue
                    if depth >= 2:
                        continue
                    for cand in all_fns_by_name.get(c.callee_name, ()):
                        if not cand.has_body or cand.visibility not in ("internal", "private") or id(cand) in x_visited:
                            continue
                        x_visited.add(id(cand))
                        _ensure_direct(cand)
                        cand_svars = direct_storage_vars_cache[id(cand)]
                        child_uncond = is_uncond_path and (c.callee_name in cur_uncond_names)
                        if child_uncond:
                            for sk, meta in _map_cross_sync(_extract_unconditional_calls(cand.body_masked), cand_svars).items():
                                uncond_xsync.setdefault(sk, meta)
                        any_xsync.update(_map_cross_sync(cand.calls, cand_svars).keys())
                        x_frontier.append((cand, depth + 1, child_uncond))

            exempt_sync = bool(_LIB_SYNC_EXEMPT_RE.match(fn.name.lstrip("_")))
            cross_lib_collector.append(
                _CrossLibFnRecord(
                    path=path,
                    library_name=contract.name,
                    fn=fn,
                    struct_surfaces=struct_surfaces,
                    uncond_sync=uncond_xsync,
                    any_sync=frozenset(any_xsync),
                    external_lib_calls=frozenset(ext_lib_calls),
                    exempt_for_sync=exempt_sync,
                )
            )

        if len(eligible) < 2:
            return []

    contract_has_deadline_state = any(
        sv.name.lower() in {d.lower() for d in _DEADLINE_IDENTS} for sv in all_vars.values()
    )

    def _is_time_gated_finalizer(fn: SolFunctionFact) -> bool:
        if any(_TIMELOCK_MOD_RE.search(m) for m in fn.modifiers):
            return True
        for cond in _extract_inline_guard_conditions(fn.body_masked):
            if "block.timestamp" in cond and re.search(r"\bblock\.timestamp\s*<", cond):
                return True
        return False

    def _is_exempt(fn: SolFunctionFact, gkey: str, guarded_peers: Iterable[SolFunctionFact] = ()) -> bool:
        low_name = fn.name.lstrip("_").lower()
        if low_name in _PUBLIC_CALLBACK_OR_INIT_NAMES or low_name.startswith(("init", "initialize", "reinitialize")):
            return True
        if gkey == "reentrancy":
            if not eff_ext_call[id(fn)]:
                return True
            if "caller_auth" in eff_guards[id(fn)] and guarded_peers:
                if not any("caller_auth" in eff_guards[id(p)] for p in guarded_peers):
                    return True
            return False
        if gkey == "not_paused":
            return bool(_EMERGENCY_OR_PAUSE_FN_RE.search(fn.name))
        if gkey == "caller_auth":
            if eff_user_scoped[id(fn)]:
                return True
            if _is_time_gated_finalizer(fn):
                return True
            if low_name in {"transfer", "deposit", "lock", "repay", "fund", "donate"}:
                return True
            peers = list(guarded_peers)
            if peers and all(_CONFIG_SETTER_RE.match(p.name) for p in peers) and not _CONFIG_SETTER_RE.match(fn.name):
                return True
            return False
        if gkey == "deadline":
            has_deadline_param = any(
                p.name and p.name.lower() in {d.lower() for d in _DEADLINE_IDENTS} for p in fn.params
            )
            return not (has_deadline_param or contract_has_deadline_state)
        if gkey.startswith("sync:"):
            # Satisfied if target calls the sync step even conditionally or via 2-hop helper
            if gkey in eff_any_sync[id(fn)]:
                return True
            # If sync step is a simple intra-contract state-var helper whose mutated state vars are already
            # directly written inline by target (e.g., `_updateLastTotalAssets` vs inline `lastTotalAssets = ...`), exempt.
            if gkey.startswith("sync:self."):
                hname = gkey.removeprefix("sync:self.")
                h_vars = helper_mutated_state_vars.get(hname)
                if h_vars and h_vars <= eff_surfaces[id(fn)]:
                    return True
            if _INIT_OR_CONFIG_EXEMPT_RE.match(fn.name.lstrip("_")):
                return True
            return False
        if gkey.startswith("mod:"):
            return bool(_EMERGENCY_OR_PAUSE_FN_RE.search(fn.name))
        if gkey.startswith("helper:"):
            return bool(_EMERGENCY_OR_PAUSE_FN_RE.search(fn.name))
        return False

    # Collect missing-guard records keyed by `(fn_id, guard_key)` -> `(fn, guard_label, surfaces, peers)`
    hits: dict[tuple[int, str], tuple[SolFunctionFact, str, set[str], set[str]]] = {}

    def _record_hit(target: SolFunctionFact, gkey: str, glabel: str, surface: str, peers: Iterable[str]) -> None:
        key = (id(target), gkey)
        if key not in hits:
            hits[key] = (target, glabel, {surface}, set(peers))
        else:
            _, _, surfs, pset = hits[key]
            surfs.add(surface)
            pset.update(peers)

    # Rule A: Sibling-group consensus (`len(group) >= 3`, `>= 2` peers and `>= 2/3` of group enforce G)
    surface_groups: dict[str, list[SolFunctionFact]] = {}
    reent_groups: dict[str, list[SolFunctionFact]] = {}
    for fn in eligible:
        for s in eff_surfaces[id(fn)]:
            surface_groups.setdefault(s, []).append(fn)
        if not is_library:
            for s in eff_reent_surfaces[id(fn)]:
                reent_groups.setdefault(s, []).append(fn)

    def _eval_consensus_group(surface: str, group: list[SolFunctionFact], allowed_guards: set[str] | None) -> None:
        n = len(group)
        if n < 3:
            return
        guard_members: dict[str, list[SolFunctionFact]] = {}
        guard_labels: dict[str, str] = {}
        for fn in group:
            for gkey, glabel in eff_guards[id(fn)].items():
                if allowed_guards is not None and gkey not in allowed_guards:
                    continue
                if is_library and not (gkey.startswith("sync:") or gkey in {"caller_auth", "not_paused", "deadline"}):
                    continue
                guard_members.setdefault(gkey, []).append(fn)
                guard_labels.setdefault(gkey, glabel)
        for gkey, guarded in guard_members.items():
            if len(guarded) < 2:
                continue
            guarded_ids = {id(f) for f in guarded}
            if gkey == "deadline":
                applicable = [f for f in group if id(f) in guarded_ids or not _is_exempt(f, gkey, guarded)]
                denom = len(applicable)
                if denom < 3 or len(guarded) * 3 < denom * 2:
                    continue
                targets = [f for f in applicable if id(f) not in guarded_ids]
            else:
                if len(guarded) * 3 < n * 2:
                    continue
                targets = [f for f in group if id(f) not in guarded_ids and not _is_exempt(f, gkey, guarded)]
            peer_names = sorted({f.name for f in guarded})
            for fn in targets:
                _record_hit(fn, gkey, guard_labels[gkey], surface, peer_names)

    for surf, grp in sorted(surface_groups.items()):
        _eval_consensus_group(surf, grp, allowed_guards=None)
    for surf, grp in sorted(reent_groups.items()):
        if surf not in surface_groups:
            _eval_consensus_group(surf, grp, allowed_guards={"reentrancy"})

    # Rule B: Matched risk-equivalent opposite pairs & same-verb variant/batch pairs (contracts only)
    if not is_library:
        for i in range(len(eligible)):
            fa = eligible[i]
            for j in range(i + 1, len(eligible)):
                fb = eligible[j]
                rel = _pair_relation(fa.name, fb.name)
                if rel is None:
                    continue
                shared_prim = eff_surfaces[id(fa)] & eff_surfaces[id(fb)]
                shared_reent = eff_reent_surfaces[id(fa)] & eff_reent_surfaces[id(fb)]
                if not shared_prim and not shared_reent:
                    continue
                ga = eff_guards[id(fa)]
                gb = eff_guards[id(fb)]
                for guarded_fn, target_fn, g_have, g_miss in ((fa, fb, ga, gb), (fb, fa, gb, ga)):
                    for gkey, glabel in g_have.items():
                        if gkey in g_miss:
                            continue
                        if gkey.startswith("sync:"):
                            continue
                        if rel == "opposite":
                            if not (gkey in {"reentrancy", "not_paused"} or gkey.startswith("mod:")):
                                continue
                        else:
                            if gkey.startswith("helper:"):
                                continue
                        shared = shared_reent if gkey == "reentrancy" else shared_prim
                        if not shared:
                            continue
                        if _is_exempt(target_fn, gkey, (guarded_fn,)):
                            continue
                        _record_hit(target_fn, gkey, glabel, sorted(shared)[0], (guarded_fn.name,))

        # Rule C: Versioned-flow sibling pairing (`<stem>` <-> `<stem>V2`/`<stem>_v2`/`<stem>2`, `<stem>V1` <-> `<stem>V2`)
        by_vstem: dict[str, list[tuple[int, SolFunctionFact]]] = {}
        for fn in eligible:
            vstem, ver = _split_version_stem(fn.name)
            by_vstem.setdefault(vstem, []).append((ver, fn))
        for vstem, entries in sorted(by_vstem.items()):
            versions = {v for v, _ in entries}
            if len(versions) < 2:
                continue
            entries.sort(key=lambda pair: (pair[0], pair[1].start_line))
            for idx_a in range(len(entries)):
                ver_a, base_fn = entries[idx_a]
                for idx_b in range(idx_a + 1, len(entries)):
                    ver_b, newer_fn = entries[idx_b]
                    if ver_a >= ver_b:
                        continue
                    g_base = eff_guards[id(base_fn)]
                    g_new = eff_guards[id(newer_fn)]
                    shared = eff_surfaces[id(base_fn)] & eff_surfaces[id(newer_fn)]
                    surf_label = sorted(shared)[0] if shared else f"versioned sibling {base_fn.name}"
                    for gkey, glabel in g_base.items():
                        if gkey in g_new:
                            continue
                        if not (
                            gkey in {"reentrancy", "not_paused", "caller_auth"}
                            or gkey.startswith("mod:")
                            or gkey.startswith("sync:")
                        ):
                            continue
                        if _is_exempt(newer_fn, gkey, (base_fn,)):
                            continue
                        _record_hit(newer_fn, gkey, glabel, surf_label, (base_fn.name,))

    findings: list[Finding] = []
    for (fn_id, gkey), (fn, glabel, surfs, peers) in sorted(
        hits.items(), key=lambda kv: (kv[1][0].start_line, kv[1][0].name, kv[0][1])
    ):
        peer_list = ", ".join(sorted(peers)[:3])
        if len(peers) > 3:
            peer_list += f" (+{len(peers) - 3})"
        surf_list = ", ".join(sorted({_surface_display(s) for s in surfs})[:2])
        if gkey.startswith("sync:"):
            ev_msg = f"omits state-sync call {glabel} invoked by sibling(s) {peer_list} on {surf_list}"
            if intra_emitted is not None:
                mname = gkey.rsplit(".", 1)[-1]
                intra_emitted.add((path, f"{contract.name}.{fn.name}", fn.start_line, mname))
        else:
            ev_msg = f"missing {glabel} enforced by sibling(s) {peer_list} on {surf_list}"
        findings.append(
            Finding(
                detector="sibling_guard_asymmetry",
                symbol=f"{contract.name}.{fn.name}",
                path=path,
                start_line=fn.start_line,
                end_line=fn.end_line,
                evidence=(ev_msg,),
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
    workspace_libraries: set[str] = set()
    readonly_lib_methods_mut: dict[str, set[str]] = {}
    all_readonly_methods: set[str] = set()
    defining_libs_mut: dict[str, set[str]] = {}
    file_imports: dict[str, tuple[dict[str, str], tuple[str, ...]]] = {}
    file_local_structs: dict[str, frozenset[str]] = {}
    file_local_containers: dict[str, frozenset[str]] = {}

    for path, f in parsed.items():
        norm_path = path.replace("\\", "/")
        file_imports[norm_path] = _extract_sol_imports(f.source, norm_path)
        file_local_structs[norm_path] = frozenset(_STRUCT_DECL_RE.findall(f.masked_source))
        file_local_containers[norm_path] = frozenset(c.name for c in f.contracts)
        for c in f.contracts:
            contracts_by_name.setdefault(c.name, []).append(c)
            if c.kind == "library":
                workspace_libraries.add(c.name)
                ro_set = readonly_lib_methods_mut.setdefault(c.name, set())
                for fn in c.functions:
                    if fn.has_body:
                        defining_libs_mut.setdefault(fn.name, set()).add(c.name)
                    if fn.mutability in ("view", "pure"):
                        ro_set.add(fn.name)
                        all_readonly_methods.add(fn.name)
    descendants = _collect_inheritance_descendants(contracts_by_name)
    lib_set = frozenset(workspace_libraries)
    readonly_lib_map = {k: frozenset(v) for k, v in readonly_lib_methods_mut.items()}
    all_readonly_set = frozenset(all_readonly_methods)
    defining_libs_map = {k: frozenset(v) for k, v in defining_libs_mut.items()}

    cross_lib_records: list[_CrossLibFnRecord] = []
    intra_emitted: set[tuple[str, str, int, str]] = set()

    findings: list[Finding] = []
    for path, f_facts in parsed.items():
        raw_lines = f_facts.source.splitlines()
        findings.extend(_detect_dead_state_vars(f_facts, parsed, contracts_by_name, descendants))
        for contract in f_facts.contracts:
            ancestor_fns = _collect_ancestor_functions(contract, contracts_by_name)
            for fn in contract.functions:
                findings.extend(_detect_try_catch_sinks(path, contract, fn))
                findings.extend(_detect_unused_params(path, contract, fn, ancestor_fns, raw_lines))
            findings.extend(_detect_wrapper_and_override_drops(path, contract, ancestor_fns))
            findings.extend(
                _detect_sibling_guard_asymmetry(
                    path,
                    contract,
                    contracts_by_name,
                    ancestor_fns,
                    lib_set,
                    readonly_lib_map,
                    all_readonly_set,
                    cross_lib_records,
                    file_imports,
                    file_local_structs,
                    file_local_containers,
                    intra_emitted,
                )
            )
    findings.extend(_detect_cross_library_guard_asymmetry(cross_lib_records, defining_libs_map, intra_emitted))
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
