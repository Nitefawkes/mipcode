"""Zero-dependency structural Anchor/Solana `#[derive(Accounts)]` sibling constraint analyzer."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .delta import _path_tier, is_vendored_path, rank_findings
from .model import Finding

_SPL_AND_FRAMEWORK_TYPES = frozenset(
    {
        "TokenAccount",
        "Mint",
        "Token",
        "TokenInterface",
        "AssociatedToken",
        "System",
        "Rent",
        "Clock",
        "Instructions",
        "SlotHashes",
        "StakeHistory",
        "EpochSchedule",
        "Sysvar",
        "ProgramData",
        "IdlAccount",
    }
)
_INIT_OR_CONFIG_STRUCT_RE = re.compile(
    r"^(?:Init|Initialize|Create|Register|Setup|New[A-Z]|OpenPool|OpenMarket)"
)
_LIQUIDATE_OR_CRANK_STRUCT_RE = re.compile(
    r"(?:Liquidate|Liquidation|Refresh|Sync|Crank|Settle|Deleverage|Check)",
    re.IGNORECASE,
)
_ADMIN_OR_EMERGENCY_EXEMPT_RE = re.compile(
    r"(?:Pause|Unpause|Freeze|Unfreeze|Emergency|Admin|Toggle|Close|Rescue|Recover)",
    re.IGNORECASE,
)
_READONLY_ACTION_STRUCT_RE = re.compile(
    r"^(?:Calculate|Compute|View|Query|Get|Read|Preview|Estimate|Simulate)[A-Z_]"
)
_PAIRED_OP_STEMS: tuple[tuple[str, str], ...] = (
    ("depositto", "withdrawfrom"),
    ("addto", "removefrom"),
    ("supplyto", "redeemfrom"),
    ("deposit", "withdraw"),
    ("borrow", "repay"),
    ("stake", "unstake"),
    ("lock", "unlock"),
    ("mint", "burn"),
    ("supply", "redeem"),
    ("open", "close"),
    ("add", "remove"),
    ("marginborrow", "marginrepay"),
    ("depositcollateral", "withdrawcollateral"),
)
_OPPOSITE_DIRECTION_WORDS_RE = re.compile(
    r"(?:deposit|withdraw(?:al)?|borrow|repay|stake|unstake|lock|unlock|mint|burn|supply|redeem)",
    re.IGNORECASE,
)
_VERSION_SUFFIX_RE = re.compile(r"^(.+?)(?:_?[vV])([1-9]\d*)$")

_STRUCT_HDR_RE = re.compile(
    r"\b(?:pub(?:\s*\([^)]*\))?\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)
_FN_CONTEXT_RE = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_IMPL_FOR_RE = re.compile(
    r"\bimpl\b(?:<[^>]*>)?\s+(?:[A-Za-z0-9_:]+\s+(?:<[^>]*>\s+)?for\s+)?([A-Za-z_][A-Za-z0-9_]*)(?:\s*<[^>]*>)?\s*\{"
)
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


@dataclass(frozen=True)
class AnchorAccountField:
    """Single field inside a `#[derive(Accounts)]` struct."""

    name: str
    wrapper_kind: str  # Account, AccountLoader, InterfaceAccount, Signer, Program, Interface, SystemAccount, UncheckedAccount, AccountInfo, Sysvar, Other
    inner_type: str
    raw_type: str
    is_optional: bool
    is_mut: bool
    is_init: bool
    is_signer: bool
    has_close: bool
    has_seeds: bool
    has_bump: bool
    has_executable: bool
    has_check_comment: bool
    has_check_cpi: bool
    has_ones: tuple[str, ...]
    address_expr: str | None
    owner_expr: str | None
    token_mint: str | None
    token_authority: str | None
    constraints: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class AnchorAccountsStruct:
    """Parsed `#[derive(Accounts)]` instruction context struct."""

    path: str
    name: str
    start_line: int
    end_line: int
    fields: tuple[AnchorAccountField, ...]
    instruction_params: str


@dataclass(frozen=True)
class AnchorFileFacts:
    """All extracted Anchor facts for one `.rs` file."""

    path: str
    source: str
    masked_source: str
    structs: tuple[AnchorAccountsStruct, ...]
    handler_bodies: Mapping[str, str]
    free_fns: Mapping[str, str]


def _find_matching(text: str, open_pos: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    n = len(text)
    i = open_pos
    while i < n:
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    paren = bracket = brace = angle = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Avoid confusing `->` or `>=` or `=>` with angle brackets
        nxt = text[i + 1] if i + 1 < n else ""
        prev = text[i - 1] if i > 0 else ""
        if ch == "(":
            paren += 1
        elif ch == ")" and paren > 0:
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]" and bracket > 0:
            bracket -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace > 0:
            brace -= 1
        elif ch == "<" and nxt not in ("<", "=") and prev != "-":
            angle += 1
        elif ch == ">" and angle > 0 and prev not in ("=", "-") and nxt != "=":
            angle -= 1
        if ch == sep and paren == 0 and bracket == 0 and brace == 0 and angle == 0:
            piece = "".join(cur).strip()
            if piece:
                parts.append(piece)
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def strip_rust_comments_and_strings(source: str) -> str:
    """Replace Rust comments, strings, raw strings, and char literals with spaces while keeping newlines and lifetimes."""
    chars = list(source)
    n = len(chars)
    i = 0
    while i < n:
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < n else ""
        # Line comment `// ...`
        if ch == "/" and nxt == "/":
            chars[i] = chars[i + 1] = " "
            i += 2
            while i < n and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        # Nested block comment `/* ... */`
        if ch == "/" and nxt == "*":
            chars[i] = chars[i + 1] = " "
            i += 2
            depth = 1
            while i < n and depth > 0:
                if chars[i] == "/" and i + 1 < n and chars[i + 1] == "*":
                    chars[i] = chars[i + 1] = " "
                    depth += 1
                    i += 2
                    continue
                if chars[i] == "*" and i + 1 < n and chars[i + 1] == "/":
                    chars[i] = chars[i + 1] = " "
                    depth -= 1
                    i += 2
                    continue
                if chars[i] != "\n":
                    chars[i] = " "
                i += 1
            continue
        # Raw strings `r#"..."#`, `br#"..."#`, `cr"..."`
        raw_m = None
        if ch in ("r", "b", "c") and (i == 0 or not (chars[i - 1].isalnum() or chars[i - 1] == "_")):
            raw_m = re.match(r'(?:[bc])?r(#*)"', source[i:])
        if raw_m is not None:
            hashes = raw_m.group(1)
            end_tok = '"' + hashes
            start_quote_end = i + raw_m.end()
            for k in range(i, start_quote_end):
                if chars[k] != "\n":
                    chars[k] = " "
            close_idx = source.find(end_tok, start_quote_end)
            end_pos = (close_idx + len(end_tok)) if close_idx >= 0 else n
            for k in range(start_quote_end, end_pos):
                if chars[k] != "\n":
                    chars[k] = " "
            i = end_pos
            continue
        # Byte/C string literal prefix `b"` or `c"`
        if ch in ("b", "c") and nxt == '"' and (i == 0 or not (chars[i - 1].isalnum() or chars[i - 1] == "_")):
            chars[i] = chars[i + 1] = " "
            i += 2
            while i < n:
                if chars[i] == "\\" and i + 1 < n:
                    if chars[i] != "\n":
                        chars[i] = " "
                    if chars[i + 1] != "\n":
                        chars[i + 1] = " "
                    i += 2
                    continue
                c = chars[i]
                if c != "\n":
                    chars[i] = " "
                i += 1
                if c == '"':
                    break
            continue
        # Normal string `"..."`
        if ch == '"':
            chars[i] = " "
            i += 1
            while i < n:
                if chars[i] == "\\" and i + 1 < n:
                    if chars[i] != "\n":
                        chars[i] = " "
                    if chars[i + 1] != "\n":
                        chars[i + 1] = " "
                    i += 2
                    continue
                c = chars[i]
                if c != "\n":
                    chars[i] = " "
                i += 1
                if c == '"':
                    break
            continue
        # Byte char literal `b'x'` or normal char literal `'x'` (must NOT eat lifetimes `'info`, `'a`, `'static`, `'_,`)
        if ch == "b" and nxt == "'" and (i == 0 or not (chars[i - 1].isalnum() or chars[i - 1] == "_")):
            bm = re.match(r"b'(?:[^'\\\n]|\\(?:x[0-9a-fA-F]{2}|.))'", source[i:])
            if bm is not None:
                for k in range(i, i + bm.end()):
                    if chars[k] != "\n":
                        chars[k] = " "
                i += bm.end()
                continue
        if ch == "'":
            cm = re.match(r"'(?:[^'\\\n]|\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]+\}|.))'", source[i:])
            if cm is not None:
                for k in range(i, i + cm.end()):
                    if chars[k] != "\n":
                        chars[k] = " "
                i += cm.end()
                continue
        i += 1

    # Mask `#[cfg(test)] mod ... { ... }` blocks so unit tests never pollute consensus
    masked = "".join(chars)
    chars2 = list(masked)
    for tm in re.finditer(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]", masked):
        mod_m = re.search(r"\bmod\s+[A-Za-z_][A-Za-z0-9_]*\s*\{", masked[tm.end() :])
        if mod_m and masked[tm.end() : tm.end() + mod_m.start()].count(";") == 0:
            open_b = tm.end() + mod_m.end() - 1
            close_b = _find_matching(masked, open_b, "{", "}")
            if close_b > open_b:
                for k in range(tm.start(), close_b + 1):
                    if chars2[k] != "\n":
                        chars2[k] = " "
    return "".join(chars2)


def _strip_at_error(clause: str) -> str:
    """Remove trailing `@ ErrorExpr` outside brackets from an `#[account(...)]` clause."""
    paren = bracket = brace = 0
    for idx, ch in enumerate(clause):
        if ch == "(":
            paren += 1
        elif ch == ")" and paren > 0:
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]" and bracket > 0:
            bracket -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace > 0:
            brace -= 1
        elif ch == "@" and paren == 0 and bracket == 0 and brace == 0:
            return clause[:idx].strip()
    return clause.strip()


def _split_kv_clause(clause: str) -> tuple[str, str | None]:
    """Split `key = value` (respecting `==`, `!=`, `<=`, `>=`, `=>`) into `(key, value_or_None)`."""
    paren = bracket = brace = angle = 0
    n = len(clause)
    for i, ch in enumerate(clause):
        if ch == "(":
            paren += 1
        elif ch == ")" and paren > 0:
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]" and bracket > 0:
            bracket -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace > 0:
            brace -= 1
        elif ch == "<":
            angle += 1
        elif ch == ">" and angle > 0:
            angle -= 1
        elif ch == "=" and paren == 0 and bracket == 0 and brace == 0 and angle == 0:
            prev = clause[i - 1] if i > 0 else ""
            nxt = clause[i + 1] if i + 1 < n else ""
            if prev in ("!", "=", "<", ">") or nxt in ("=", ">"):
                continue
            return clause[:i].strip(), clause[i + 1 :].strip()
    return clause.strip(), None


def _unwrap_anchor_type(type_str: str) -> tuple[str, str, bool]:
    """Return `(wrapper_kind, inner_type, is_optional)` for an Anchor account field type."""
    cleaned = re.sub(r"\s+", " ", type_str).strip()
    is_optional = False
    while True:
        opt_m = re.match(r"^(?:Option|core::option::Option|std::option::Option)\s*<(.+)>$", cleaned)
        if opt_m:
            is_optional = True
            cleaned = opt_m.group(1).strip()
            continue
        box_m = re.match(r"^(?:Box|std::boxed::Box|alloc::boxed::Box)\s*<(.+)>$", cleaned)
        if box_m:
            cleaned = box_m.group(1).strip()
            continue
        break

    lt_idx = cleaned.find("<")
    if lt_idx < 0:
        wrapper = cleaned.rsplit("::", 1)[-1].strip()
        return wrapper, "", is_optional

    outer = cleaned[:lt_idx].rsplit("::", 1)[-1].strip()
    gt_idx = _find_matching(cleaned, lt_idx, "<", ">")
    inner_full = cleaned[lt_idx + 1 : gt_idx] if gt_idx > lt_idx else cleaned[lt_idx + 1 :]
    args = _split_top_level(inner_full, ",")
    non_lt = [a for a in args if not a.strip().startswith("'")]
    target = non_lt[-1].strip() if non_lt else ""
    if "<" in target:
        target = target.split("<", 1)[0].strip()
    inner_short = target.rsplit("::", 1)[-1].strip()
    if outer in {
        "Account",
        "AccountLoader",
        "InterfaceAccount",
        "Signer",
        "Program",
        "Interface",
        "SystemAccount",
        "UncheckedAccount",
        "AccountInfo",
        "Sysvar",
    }:
        return outer, inner_short, is_optional
    return "Other", outer, is_optional


def _parse_struct_attributes(masked: str, struct_start: int) -> tuple[list[tuple[int, int, str]], bool, str]:
    """Walk backwards over contiguous `#[...]` blocks before `struct` and check `#[derive(...Accounts...)]`."""
    pos = struct_start
    attrs: list[tuple[int, int, str]] = []
    while pos > 0:
        j = pos - 1
        while j >= 0 and masked[j].isspace():
            j -= 1
        if j < 0 or masked[j] != "]":
            break
        # Find matching `[` walking backward
        depth = 0
        k = j
        open_b = -1
        while k >= 0:
            if masked[k] == "]":
                depth += 1
            elif masked[k] == "[":
                depth -= 1
                if depth == 0:
                    open_b = k
                    break
            k -= 1
        if open_b < 0:
            break
        h = open_b - 1
        while h >= 0 and masked[h].isspace():
            h -= 1
        if h >= 0 and masked[h] == "#":
            attrs.append((h, j + 1, masked[open_b + 1 : j]))
            pos = h
            continue
        break
    attrs.reverse()
    is_accounts = False
    instr_params = ""
    for _, _, content in attrs:
        if re.search(r"\bderive\s*\([^)]*\bAccounts\b", content):
            is_accounts = True
        im = re.match(r"^\s*instruction\s*\((.*)\)\s*$", content, flags=re.DOTALL)
        if im:
            instr_params = im.group(1).strip()
    return attrs, is_accounts, instr_params


def _split_struct_fields(body: str) -> list[tuple[int, int, str]]:
    """Split `{ field1: Type1, #[account(...)] field2: Type2 }` into `(start_rel, end_rel, chunk)`."""
    chunks: list[tuple[int, int, str]] = []
    n = len(body)
    i = 0
    paren = bracket = brace = angle = 0
    start = 0
    while i < n:
        ch = body[i]
        nxt = body[i + 1] if i + 1 < n else ""
        prev = body[i - 1] if i > 0 else ""
        if ch == "(":
            paren += 1
        elif ch == ")" and paren > 0:
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]" and bracket > 0:
            bracket -= 1
        elif ch == "{":
            brace += 1
        elif ch == "}" and brace > 0:
            brace -= 1
        elif ch == "<" and nxt not in ("<", "=") and prev != "-":
            angle += 1
        elif ch == ">" and angle > 0 and prev not in ("=", "-") and nxt != "=":
            angle -= 1
        elif ch == "," and paren == 0 and bracket == 0 and brace == 0 and angle == 0:
            chunk = body[start:i]
            if chunk.strip():
                chunks.append((start, i, chunk))
            start = i + 1
        i += 1
    if start < n and body[start:].strip():
        chunks.append((start, n, body[start:]))
    return chunks


def _parse_account_field(
    chunk: str,
    chunk_abs_start: int,
    masked: str,
    raw_lines: list[str],
) -> AnchorAccountField | None:
    # Consume leading `#[...]` attribute blocks
    pos = 0
    n = len(chunk)
    account_clauses: list[str] = []
    while pos < n:
        while pos < n and chunk[pos].isspace():
            pos += 1
        if pos >= n or not chunk.startswith("#", pos):
            break
        open_b = chunk.find("[", pos + 1)
        if open_b < 0 or chunk[pos + 1 : open_b].strip():
            break
        close_b = _find_matching(chunk, open_b, "[", "]")
        if close_b < 0:
            break
        inner = chunk[open_b + 1 : close_b].strip()
        am = re.match(r"^account\s*\((.*)\)\s*$", inner, flags=re.DOTALL)
        if am:
            account_clauses.extend(_split_top_level(am.group(1), ","))
        pos = close_b + 1

    decl = chunk[pos:].strip()
    fm = re.match(r"^(?:pub(?:\s*\([^)]*\))?\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$", decl, flags=re.DOTALL)
    if not fm:
        return None
    fname = fm.group(1)
    ftype = fm.group(2).strip().rstrip(",")
    decl_line = masked[: chunk_abs_start + pos].count("\n") + 1
    attr_start_line = masked[: chunk_abs_start + (len(chunk) - len(chunk.lstrip()))].count("\n") + 1

    # Inspect raw comments between attr_start_line-3 and decl_line for `/// CHECK:`
    has_check_comment = False
    has_check_cpi = False
    lo = max(0, attr_start_line - 4)
    hi = min(len(raw_lines), decl_line)
    comment_ctx = "\n".join(raw_lines[lo:hi])
    if re.search(r"///\s*CHECK\s*:", comment_ctx):
        has_check_comment = True
        if re.search(r"\bCPI\b", comment_ctx, re.IGNORECASE):
            has_check_cpi = True

    is_mut = False
    is_init = False
    is_signer = False
    has_close = False
    has_seeds = False
    has_bump = False
    has_executable = False
    has_ones: list[str] = []
    address_expr: str | None = None
    owner_expr: str | None = None
    token_mint: str | None = None
    token_authority: str | None = None
    constraints: list[str] = []

    for raw_clause in account_clauses:
        core = _strip_at_error(raw_clause)
        if not core:
            continue
        key, val = _split_kv_clause(core)
        k_low = key.lower().replace(" ", "")
        if val is None:
            if k_low == "mut":
                is_mut = True
            elif k_low in ("init", "init_if_needed"):
                is_init = True
                is_mut = True
            elif k_low == "signer":
                is_signer = True
            elif k_low == "bump":
                has_bump = True
            elif k_low == "executable":
                has_executable = True
            elif k_low == "seeds":
                has_seeds = True
        else:
            if k_low == "has_one":
                has_ones.append(val.strip())
            elif k_low == "seeds" or k_low.startswith("seeds::"):
                has_seeds = True
            elif k_low == "bump":
                has_bump = True
            elif k_low == "address":
                address_expr = val.strip()
            elif k_low == "owner":
                owner_expr = val.strip()
            elif k_low == "close":
                has_close = True
                is_mut = True
            elif k_low == "realloc":
                is_mut = True
            elif k_low in ("token::mint", "associated_token::mint"):
                token_mint = val.strip()
            elif k_low in ("token::authority", "associated_token::authority"):
                token_authority = val.strip()
            elif k_low == "constraint":
                constraints.append(val.strip())

    wrapper_kind, inner_type, is_optional = _unwrap_anchor_type(ftype)
    if wrapper_kind == "Signer":
        is_signer = True

    return AnchorAccountField(
        name=fname,
        wrapper_kind=wrapper_kind,
        inner_type=inner_type,
        raw_type=ftype,
        is_optional=is_optional,
        is_mut=is_mut,
        is_init=is_init,
        is_signer=is_signer,
        has_close=has_close,
        has_seeds=has_seeds,
        has_bump=has_bump,
        has_executable=has_executable,
        has_check_comment=has_check_comment,
        has_check_cpi=has_check_cpi,
        has_ones=tuple(has_ones),
        address_expr=address_expr,
        owner_expr=owner_expr,
        token_mint=token_mint,
        token_authority=token_authority,
        constraints=tuple(constraints),
        line=decl_line,
    )


def _extract_handler_and_fn_bodies(masked: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return `(struct_name -> combined_handler_and_impl_body, fn_name -> body)`."""
    handler_chunks: dict[str, list[str]] = {}
    free_fns: dict[str, str] = {}

    for m in _FN_CONTEXT_RE.finditer(masked):
        fn_name = m.group(1)
        paren_pos = masked.find("(", m.end())
        if paren_pos < 0:
            continue
        close_paren = _find_matching(masked, paren_pos, "(", ")")
        if close_paren < 0:
            continue
        sig_params = masked[paren_pos + 1 : close_paren]
        # Skip trait/signature declarations without body
        brace_search = re.search(r"[{;]", masked[close_paren + 1 :])
        if not brace_search or brace_search.group(0) != "{":
            continue
        open_b = close_paren + 1 + brace_search.start()
        close_b = _find_matching(masked, open_b, "{", "}")
        if close_b <= open_b:
            continue
        body = masked[open_b + 1 : close_b]
        free_fns[fn_name] = body

        ctx_m = re.search(r"\bContext\s*<", sig_params)
        if ctx_m:
            lt_pos = ctx_m.end() - 1
            gt_pos = _find_matching(sig_params, lt_pos, "<", ">")
            if gt_pos > lt_pos:
                ctx_args = _split_top_level(sig_params[lt_pos + 1 : gt_pos], ",")
                non_lt = [a for a in ctx_args if not a.strip().startswith("'")]
                if non_lt:
                    sname = non_lt[-1].split("<", 1)[0].rsplit("::", 1)[-1].strip()
                    if sname:
                        handler_chunks.setdefault(sname, []).append(body)

    for im in _IMPL_FOR_RE.finditer(masked):
        sname = im.group(1)
        open_b = im.end() - 1
        close_b = _find_matching(masked, open_b, "{", "}")
        if close_b > open_b:
            handler_chunks.setdefault(sname, []).append(masked[open_b + 1 : close_b])

    return {k: "\n".join(v) for k, v in handler_chunks.items()}, free_fns


def parse_anchor_file(path: str, source: str) -> AnchorFileFacts:
    """Parse `#[derive(Accounts)]` structs and linked handlers from a Rust/Anchor `.rs` file."""
    masked = strip_rust_comments_and_strings(source)
    raw_lines = source.splitlines()
    structs: list[AnchorAccountsStruct] = []

    for m in _STRUCT_HDR_RE.finditer(masked):
        sname = m.group(1)
        _, is_accounts, instr_params = _parse_struct_attributes(masked, m.start())
        if not is_accounts:
            continue
        # Ensure `{` precedes any `;` (skip tuple/unit structs)
        sep_m = re.search(r"[{;]", masked[m.end() :])
        if not sep_m or sep_m.group(0) != "{":
            continue
        open_b = m.end() + sep_m.start()
        close_b = _find_matching(masked, open_b, "{", "}")
        if close_b <= open_b:
            continue
        body = masked[open_b + 1 : close_b]
        fields: list[AnchorAccountField] = []
        for rel_start, _, chunk in _split_struct_fields(body):
            fld = _parse_account_field(chunk, open_b + 1 + rel_start, masked, raw_lines)
            if fld is not None:
                fields.append(fld)
        structs.append(
            AnchorAccountsStruct(
                path=path,
                name=sname,
                start_line=masked[: m.start()].count("\n") + 1,
                end_line=masked[:close_b].count("\n") + 1,
                fields=tuple(fields),
                instruction_params=instr_params,
            )
        )

    handler_bodies, free_fns = _extract_handler_and_fn_bodies(masked)
    return AnchorFileFacts(
        path=path,
        source=source,
        masked_source=masked,
        structs=tuple(structs),
        handler_bodies=handler_bodies,
        free_fns=free_fns,
    )


def _program_scope_key(path: str) -> str:
    norm = path.replace("\\", "/")
    pm = re.search(r"(?:^|/)programs/([^/]+)/", norm)
    if pm:
        return f"programs/{pm.group(1)}"
    if "/src/" in norm:
        return norm.split("/src/", 1)[0]
    parts = [p for p in norm.split("/") if p]
    return parts[0] if len(parts) > 1 else ""


def _struct_pair_relation(name_a: str, name_b: str) -> str | None:
    a = name_a.lower()
    b = name_b.lower()
    if a == b:
        return None
    for left, right in _PAIRED_OP_STEMS:
        if (a.startswith(left) and b.startswith(right) and a[len(left) :] == b[len(right) :]):
            return "opposite"
        if (a.startswith(right) and b.startswith(left) and a[len(right) :] == b[len(left) :]):
            return "opposite"
    ma = _VERSION_SUFFIX_RE.match(name_a)
    mb = _VERSION_SUFFIX_RE.match(name_b)
    sa = ma.group(1).lower() if ma else a
    sb = mb.group(1).lower() if mb else b
    va = int(ma.group(2)) if ma else 1
    vb = int(mb.group(2)) if mb else 1
    if sa == sb and va != vb:
        return f"version:{va}->{vb}"
    return None


def _resolve_handler_text(struct: AnchorAccountsStruct, parsed_files: Mapping[str, AnchorFileFacts], prog_free_fns: Mapping[str, str]) -> str:
    direct = parsed_files[struct.path].handler_bodies.get(struct.name, "")
    if not direct:
        return ""
    chunks = [direct]
    seen: set[str] = set()
    for cm in re.finditer(r"\b([a-z_][A-Za-z0-9_]*)\s*\(", direct):
        cname = cm.group(1)
        if cname in seen or cname in {"require", "msg", "err", "ok", "some", "none"}:
            continue
        seen.add(cname)
        if cname in prog_free_fns:
            chunks.append(prog_free_fns[cname])
    return "\n".join(chunks)


_FLAG_PAUSED_RE = re.compile(r"(?:^|[^A-Za-z0-9]|_)paused(?:$|[^A-Za-z0-9]|_)", re.IGNORECASE)
_FLAG_LIQUIDATING_RE = re.compile(
    r"(?:^|[^A-Za-z0-9]|_)(?:liquidating|in_liquidation)(?:$|[^A-Za-z0-9]|_)", re.IGNORECASE
)
_FLAG_FROZEN_RE = re.compile(
    r"(?:^|[^A-Za-z0-9]|_)(?:frozen|disabled|is_active)(?:$|[^A-Za-z0-9]|_)", re.IGNORECASE
)
_OPT_SOME_KEY_RE = re.compile(
    r"(?:Option\s*::\s*)?Some\s*\(\s*&?\s*(\$SELF|[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*key\s*\(\s*\)\s*\)"
)
_OPT_MAP_CLOSURE_KEY_RE = re.compile(
    r"(\$SELF|[A-Za-z_][A-Za-z0-9_]*)\s*(?:\.\s*as_ref\s*\(\s*\))?\s*\.\s*map\s*\(\s*\|\s*([A-Za-z_][A-Za-z0-9_]*)\s*\|\s*&?\s*\2\s*\.\s*key\s*\(\s*\)\s*\)"
)
_OPT_MAP_FN_KEY_RE = re.compile(
    r"(\$SELF|[A-Za-z_][A-Za-z0-9_]*)\s*(?:\.\s*as_ref\s*\(\s*\))?\s*\.\s*map\s*\(\s*(?:anchor_lang\s*::\s*prelude\s*::\s*)?Key\s*::\s*key\s*\)"
)


def _normalize_opt_key_expr(text: str) -> str:
    out = _OPT_SOME_KEY_RE.sub(r"$OPTKEY(\1)", text)
    out = _OPT_MAP_CLOSURE_KEY_RE.sub(r"$OPTKEY(\1)", out)
    out = _OPT_MAP_FN_KEY_RE.sub(r"$OPTKEY(\1)", out)
    return re.sub(r"\s+", "", out)


def _normalize_constraint_expr(expr: str, self_var: str) -> str:
    replaced = re.sub(rf"\b{re.escape(self_var)}\b", "$SELF", expr)
    return _normalize_opt_key_expr(replaced)


def _extract_canonical_flag_guards(texts: Iterable[str]) -> dict[str, str]:
    joined = "\n".join(texts)
    out: dict[str, str] = {}
    if _FLAG_PAUSED_RE.search(joined):
        out["cond:not_paused"] = "not-paused guard"
    if _FLAG_LIQUIDATING_RE.search(joined):
        out["cond:not_liquidating"] = "not-liquidating guard"
    if _FLAG_FROZEN_RE.search(joined):
        out["cond:not_frozen"] = "not-frozen/active guard"
    return out


def _is_init_struct(s: AnchorAccountsStruct) -> bool:
    if _INIT_OR_CONFIG_STRUCT_RE.match(s.name):
        return True
    return any(f.is_init for f in s.fields)


def _field_verified_address_or_pda(
    field: AnchorAccountField,
    struct: AnchorAccountsStruct,
    handler_text: str,
) -> bool:
    if field.has_seeds or field.address_expr is not None or field.has_executable:
        return True
    if field.token_authority is not None:
        return True
    if any(field.name in other.has_ones for other in struct.fields):
        return True
    if field.has_ones and any(
        f.name in field.has_ones and f.is_signer for f in struct.fields
    ):
        return True
    # Any explicit constraint mentioning this field name alongside `.key()` or another verification
    for c in field.constraints:
        if field.name in c:
            return True
    for other in struct.fields:
        for c in other.constraints:
            if field.name in c:
                return True
    if handler_text and field.name in handler_text:
        if re.search(r"\b(?:find_program_address|create_program_address|require_keys_eq)\b", handler_text):
            return True
    return False


def _audit_program_structs(
    structs: list[AnchorAccountsStruct],
    parsed_files: Mapping[str, AnchorFileFacts],
    prog_free_fns: Mapping[str, str],
) -> list[Finding]:
    if len(structs) < 2:
        return []
    struct_names = {s.name for s in structs}
    handler_by_struct = {id(s): _resolve_handler_text(s, parsed_files, prog_free_fns) for s in structs}

    # Skip composite wrapper structs (containing a field whose type is another parsed Accounts struct)
    non_composite = [
        s for s in structs if not any(f.wrapper_kind == "Other" and f.inner_type in struct_names for f in s.fields)
    ]
    if len(non_composite) < 2:
        return []

    non_init = [s for s in non_composite if not _is_init_struct(s)]
    if len(non_init) < 2:
        return []

    hits: dict[tuple[int, str], tuple[AnchorAccountsStruct, str, set[str], int]] = {}

    def _record(s: AnchorAccountsStruct, key: str, msg: str, peers: Iterable[str], line: int) -> None:
        k = (id(s), key)
        if k not in hits:
            hits[k] = (s, msg, set(peers), line)
        else:
            hits[k][2].update(peers)

    # Index custom state account occurrences `(struct, field)` by state type `T`
    by_state_type: dict[str, list[tuple[AnchorAccountsStruct, AnchorAccountField]]] = {}
    for s in non_init:
        for f in s.fields:
            if (
                f.wrapper_kind in ("Account", "AccountLoader", "InterfaceAccount")
                and f.inner_type
                and f.inner_type not in _SPL_AND_FRAMEWORK_TYPES
                and not f.is_optional
            ):
                by_state_type.setdefault(f.inner_type, []).append((s, f))

    # Precompute field map per struct
    fields_by_struct = {id(s): {f.name: f for f in s.fields} for s in non_init}

    # 1) Check `has_one`, canonical `constraint` / state flags, and missing `mut` on shared state `T`
    for state_t, occs in sorted(by_state_type.items()):
        # Unique structs per `T` using first matching field for struct-level consensus, while
        # retaining all `T`-typed fields per struct so multi-field structs (e.g. `user` + `referrer_user`) stay suppressed
        by_s: dict[int, tuple[AnchorAccountsStruct, AnchorAccountField]] = {}
        occs_by_s: dict[int, list[AnchorAccountField]] = {}
        for s, f in occs:
            by_s.setdefault(id(s), (s, f))
            occs_by_s.setdefault(id(s), []).append(f)
        group = list(by_s.values())
        if len(group) < 2:
            continue

        # 1a. `has_one:<X>`
        # Separate explicit `has_one` obligations (driving consensus) from broader equivalent satisfactions
        has_one_enforced: dict[str, list[tuple[AnchorAccountsStruct, AnchorAccountField]]] = {}
        explicit_has_ones: dict[tuple[int, str], set[str]] = {}
        satisfied_has_ones: dict[tuple[int, str], set[str]] = {}
        for s, tf in group:
            fmap = fields_by_struct[id(s)]
            htext = handler_by_struct[id(s)]
            explicit: set[str] = set(tf.has_ones)
            sat: set[str] = set(tf.has_ones)
            all_constraints = list(tf.constraints)
            for fname, of in fmap.items():
                if fname == tf.name:
                    continue
                if of.address_expr and tf.name in of.address_expr:
                    sat.add(fname)
                if of.has_seeds or _field_verified_address_or_pda(of, s, htext):
                    sat.add(fname)
                all_constraints.extend(of.constraints)
            for cond in all_constraints:
                if tf.name in cond:
                    for tok in _IDENT_RE.findall(cond):
                        if tok in fmap and tok != tf.name:
                            sat.add(tok)
                            if re.search(rf"\b{re.escape(tf.name)}\.(?:[A-Za-z0-9_]+\.)*{re.escape(tok)}\s*==\s*{re.escape(tok)}\.key", cond) or re.search(rf"\b{re.escape(tok)}\.key\s*\(\s*\)\s*==\s*{re.escape(tf.name)}\.", cond):
                                explicit.add(tok)
            if htext and tf.name in htext:
                for tok in _IDENT_RE.findall(htext):
                    if tok in fmap and tok != tf.name and re.search(
                        rf"(?:require_keys_eq!\s*\(|!=|==)[^\n;]*\b{re.escape(tok)}\b",
                        htext,
                    ):
                        sat.add(tok)
            explicit_has_ones[(id(s), tf.name)] = explicit
            satisfied_has_ones[(id(s), tf.name)] = sat
            for target in explicit:
                if target in fmap and not fmap[target].is_optional:
                    has_one_enforced.setdefault(target, []).append((s, tf))

        for target, guarded_pairs in sorted(has_one_enforced.items()):
            applicable = [
                (s, tf)
                for s, tf in group
                if target in fields_by_struct[id(s)] and not fields_by_struct[id(s)][target].is_optional
            ]
            # Count any equivalent check toward numerator so inline-verified handlers don't dilute consensus
            sat_pairs = [(s, tf) for s, tf in applicable if target in satisfied_has_ones[(id(s), tf.name)]]
            if len(applicable) >= 3 and len(guarded_pairs) >= 2 and len(sat_pairs) * 3 >= len(applicable) * 2:
                sat_ids = {id(s) for s, _ in sat_pairs}
                peer_names = sorted({s.name for s, _ in guarded_pairs})
                for s, tf in applicable:
                    if id(s) not in sat_ids:
                        _record(
                            s,
                            f"has_one:{state_t}.{target}",
                            f"missing `has_one = {target}` on `{tf.name}: {state_t}` enforced by sibling(s) {{PEERS}}",
                            peer_names,
                            tf.line,
                        )
            else:
                # Opposite / versioned pair check
                for i in range(len(applicable)):
                    sa, ta = applicable[i]
                    for j in range(i + 1, len(applicable)):
                        sb, tb = applicable[j]
                        if _struct_pair_relation(sa.name, sb.name) is None:
                            continue
                        a_exp = target in explicit_has_ones[(id(sa), ta.name)]
                        b_exp = target in explicit_has_ones[(id(sb), tb.name)]
                        a_sat = target in satisfied_has_ones[(id(sa), ta.name)]
                        b_sat = target in satisfied_has_ones[(id(sb), tb.name)]
                        if a_exp and not b_sat:
                            _record(
                                sb,
                                f"has_one:{state_t}.{target}",
                                f"missing `has_one = {target}` on `{tb.name}: {state_t}` enforced by sibling(s) {{PEERS}}",
                                (sa.name,),
                                tb.line,
                            )
                        elif b_exp and not a_sat:
                            _record(
                                sa,
                                f"has_one:{state_t}.{target}",
                                f"missing `has_one = {target}` on `{ta.name}: {state_t}` enforced by sibling(s) {{PEERS}}",
                                (sb.name,),
                                ta.line,
                            )

        # 1b. Canonical `constraint` / state-flag guards (`not_paused`, `not_liquidating`, `not_frozen`, exact predicates)
        struct_flags: dict[int, dict[str, str]] = {}
        struct_preds: dict[int, dict[str, tuple[str, tuple[str, ...]]]] = {}
        all_flag_keys: dict[str, tuple[str, list[AnchorAccountsStruct]]] = {}
        all_pred_keys: dict[str, tuple[str, tuple[str, ...], list[AnchorAccountsStruct]]] = {}

        for s, tf in group:
            fmap = fields_by_struct[id(s)]
            htext = handler_by_struct[id(s)]
            all_tfs = occs_by_s[id(s)]
            all_tf_constraints = [c for f_t in all_tfs for c in f_t.constraints]
            f_texts = [*all_tf_constraints, htext]
            flags = _extract_canonical_flag_guards(f_texts)
            struct_flags[id(s)] = flags
            for fk, flabel in flags.items():
                all_flag_keys.setdefault(fk, (flabel, []))[1].append(s)

            preds: dict[str, tuple[str, tuple[str, ...]]] = {}
            for f_t in all_tfs:
                for expr in f_t.constraints:
                    if _extract_canonical_flag_guards((expr,)):
                        continue
                    norm_e = _normalize_constraint_expr(expr, f_t.name)
                    if len(norm_e) < 6:
                        continue
                    partner_fields = tuple(
                        sorted(tok for tok in _IDENT_RE.findall(norm_e) if tok in fmap and tok != f_t.name)
                    )
                    if not partner_fields:
                        continue
                    pkey = f"cond:pred:{norm_e}"
                    if pkey not in preds:
                        preds[pkey] = (expr, partner_fields)
                        all_pred_keys.setdefault(pkey, (expr, partner_fields, []))[2].append(s)
            struct_preds[id(s)] = preds

        for fk, (flabel, guarded) in sorted(all_flag_keys.items()):
            if len(group) < 3 or len(guarded) < 2 or len(guarded) * 3 < len(group) * 2:
                continue
            guarded_ids = {id(s) for s in guarded}
            peer_names = sorted({s.name for s in guarded})
            for s, tf in group:
                if id(s) in guarded_ids:
                    continue
                if _ADMIN_OR_EMERGENCY_EXEMPT_RE.search(s.name):
                    continue
                if fk == "cond:not_liquidating" and _LIQUIDATE_OR_CRANK_STRUCT_RE.search(s.name):
                    continue
                _record(
                    s,
                    f"{state_t}:{fk}",
                    f"missing `{flabel}` on `{tf.name}: {state_t}` enforced by sibling(s) {{PEERS}}",
                    peer_names,
                    tf.line,
                )

        for pkey, (raw_expr, partner_fields, guarded) in sorted(all_pred_keys.items()):
            applicable = [
                (s, tf)
                for s, tf in group
                if all(pf in fields_by_struct[id(s)] for pf in partner_fields)
            ]
            def _pred_satisfied_in_struct(s: AnchorAccountsStruct, tf: AnchorAccountField) -> bool:
                if pkey in struct_preds[id(s)]:
                    return True
                htext = handler_by_struct[id(s)]
                if not htext:
                    return False
                norm_h = _normalize_opt_key_expr(htext)
                return any(
                    pkey[len("cond:pred:") :].replace("$SELF.", f"{f_t.name}.").replace("$OPTKEY($SELF)", f"$OPTKEY({f_t.name})") in norm_h
                    for f_t in occs_by_s[id(s)]
                )

            if len(applicable) >= 3 and len(guarded) >= 2 and len(guarded) * 3 >= len(applicable) * 2:
                peer_names = sorted({s.name for s in guarded})
                for s, tf in applicable:
                    if _ADMIN_OR_EMERGENCY_EXEMPT_RE.search(s.name) or _pred_satisfied_in_struct(s, tf):
                        continue
                    _record(
                        s,
                        f"{state_t}:{pkey}",
                        f"missing constraint `{raw_expr}` on `{tf.name}: {state_t}` enforced by sibling(s) {{PEERS}}",
                        peer_names,
                        tf.line,
                    )
            else:
                # Skip predicates naming a direction-specific action (`is_deposit_allowed` vs `is_withdrawal_allowed`)
                # on opposite pairs (`Deposit*` <-> `Withdraw*`), keeping direction-invariant relation checks.
                if _OPPOSITE_DIRECTION_WORDS_RE.search(raw_expr):
                    continue
                guarded_ids = {id(s) for s in guarded}
                for i in range(len(applicable)):
                    sa, ta = applicable[i]
                    for j in range(i + 1, len(applicable)):
                        sb, tb = applicable[j]
                        if _struct_pair_relation(sa.name, sb.name) is None:
                            continue
                        if id(sa) in guarded_ids and not _pred_satisfied_in_struct(sb, tb) and not _ADMIN_OR_EMERGENCY_EXEMPT_RE.search(sb.name):
                            _record(
                                sb,
                                f"{state_t}:{pkey}",
                                f"missing constraint `{raw_expr}` on `{tb.name}: {state_t}` enforced by sibling(s) {{PEERS}}",
                                (sa.name,),
                                tb.line,
                            )
                        elif id(sb) in guarded_ids and not _pred_satisfied_in_struct(sa, ta) and not _ADMIN_OR_EMERGENCY_EXEMPT_RE.search(sa.name):
                            _record(
                                sa,
                                f"{state_t}:{pkey}",
                                f"missing constraint `{raw_expr}` on `{ta.name}: {state_t}` enforced by sibling(s) {{PEERS}}",
                                (sb.name,),
                                ta.line,
                            )

        # 1c. Missing `mut` on primary state `T` alongside `#[account(mut)]` TokenAccount/vault
        fund_cohort: list[tuple[AnchorAccountsStruct, AnchorAccountField]] = []
        for s, tf in group:
            has_mut_token = any(
                f.is_mut and f.inner_type == "TokenAccount" for f in s.fields if f.name != tf.name
            )
            if has_mut_token:
                fund_cohort.append((s, tf))
        if len(fund_cohort) >= 3:
            mut_peers = [(s, tf) for s, tf in fund_cohort if tf.is_mut]
            if len(mut_peers) >= 2 and len(mut_peers) * 3 >= len(fund_cohort) * 2:
                peer_names = sorted({s.name for s, _ in mut_peers})
                for s, tf in fund_cohort:
                    if not tf.is_mut:
                        _record(
                            s,
                            f"mut:{state_t}",
                            f"missing `mut` on `{tf.name}: {state_t}` while mutating linked token/vault unlike sibling(s) {{PEERS}}",
                            peer_names,
                            tf.line,
                        )

    # 2) PDA `seeds` verification asymmetry (non-init consensus >= 2/3 OR opposite pair)
    by_pda_target: dict[tuple[str, str], list[tuple[AnchorAccountsStruct, AnchorAccountField]]] = {}
    for s in non_init:
        for f in s.fields:
            if f.is_optional:
                continue
            if f.wrapper_kind in ("Account", "AccountLoader", "InterfaceAccount") and f.inner_type not in _SPL_AND_FRAMEWORK_TYPES:
                by_pda_target.setdefault(("state", f.inner_type), []).append((s, f))
            by_pda_target.setdefault(("field", f.name), []).append((s, f))

    for (kind_tag, tname), occs in sorted(by_pda_target.items()):
        by_s: dict[int, tuple[AnchorAccountsStruct, AnchorAccountField]] = {}
        for s, f in occs:
            by_s.setdefault(id(s), (s, f))
        group = list(by_s.values())
        if len(group) < 2:
            continue
        seeds_peers = [(s, f) for s, f in group if f.has_seeds]
        if not seeds_peers:
            continue

        def _unverified(s: AnchorAccountsStruct, f: AnchorAccountField) -> bool:
            if f.wrapper_kind in ("Signer", "Program", "Interface", "Sysvar", "SystemAccount", "Other"):
                return False
            return not _field_verified_address_or_pda(f, s, handler_by_struct[id(s)])

        if len(group) >= 3 and len(seeds_peers) >= 2 and len(seeds_peers) * 3 >= len(group) * 2:
            peer_names = sorted({s.name for s, _ in seeds_peers})
            for s, f in group:
                if _unverified(s, f):
                    _record(
                        s,
                        f"seeds:{kind_tag}:{tname}",
                        f"missing `seeds` PDA verification on `{f.name}` enforced by sibling(s) {{PEERS}}",
                        peer_names,
                        f.line,
                    )
        else:
            for i in range(len(group)):
                sa, fa = group[i]
                for j in range(i + 1, len(group)):
                    sb, fb = group[j]
                    if _struct_pair_relation(sa.name, sb.name) is None:
                        continue
                    if fa.has_seeds and not fb.has_seeds and _unverified(sb, fb):
                        _record(
                            sb,
                            f"seeds:{kind_tag}:{tname}",
                            f"missing `seeds` PDA verification on `{fb.name}` enforced by sibling(s) {{PEERS}}",
                            (sa.name,),
                            fb.line,
                        )
                    elif fb.has_seeds and not fa.has_seeds and _unverified(sa, fa):
                        _record(
                            sa,
                            f"seeds:{kind_tag}:{tname}",
                            f"missing `seeds` PDA verification on `{fa.name}` enforced by sibling(s) {{PEERS}}",
                            (sb.name,),
                            fa.line,
                        )

    # 3) Account type weakening (`Program`/`Interface` and `Signer`) across same-named fields
    by_fname: dict[str, list[tuple[AnchorAccountsStruct, AnchorAccountField]]] = {}
    for s in non_init:
        for f in s.fields:
            if not f.is_optional:
                by_fname.setdefault(f.name, []).append((s, f))

    for fname, occs in sorted(by_fname.items()):
        if len(occs) < 3:
            continue
        # 3a. `Program` / `Interface` downgraded to `AccountInfo` / `UncheckedAccount`
        prog_peers = [(s, f) for s, f in occs if f.wrapper_kind in ("Program", "Interface")]
        if len(prog_peers) >= 2 and len(prog_peers) * 3 >= len(occs) * 2:
            peer_names = sorted({s.name for s, _ in prog_peers})
            for s, f in occs:
                if f.wrapper_kind in ("AccountInfo", "UncheckedAccount"):
                    if f.address_expr or f.has_executable or f.constraints:
                        continue
                    htext = handler_by_struct[id(s)]
                    if htext and re.search(rf"\b{re.escape(fname)}\s*\.\s*(?:key|executable)\b", htext):
                        continue
                    _record(
                        s,
                        f"program_type:{fname}",
                        f"unchecked `{f.name}: {f.wrapper_kind}` typed as `Program`/`Interface` by sibling(s) {{PEERS}}",
                        peer_names,
                        f.line,
                    )

        # 3b. `Signer` downgraded on mutating non-crank/non-liquidation structs lacking any Signer
        mut_occs = [(s, f) for s, f in occs if any(sf.is_mut for sf in s.fields)]
        if len(mut_occs) >= 3:
            signer_peers = [(s, f) for s, f in mut_occs if f.is_signer]
            if len(signer_peers) >= 2 and len(signer_peers) * 3 >= len(mut_occs) * 2:
                peer_names = sorted({s.name for s, _ in signer_peers})
                for s, f in mut_occs:
                    if f.is_signer:
                        continue
                    if f.wrapper_kind not in ("AccountInfo", "UncheckedAccount", "SystemAccount"):
                        continue
                    if f.has_check_cpi or f.address_expr or f.has_seeds:
                        continue
                    if _LIQUIDATE_OR_CRANK_STRUCT_RE.search(s.name) or _READONLY_ACTION_STRUCT_RE.match(s.name):
                        continue
                    htext = handler_by_struct[id(s)]
                    if htext and (
                        re.search(rf"\b{re.escape(fname)}\s*\.\s*is_signer\b", htext)
                        or "CpiAuthentication" in htext
                    ):
                        continue
                    has_any_other_signer = any(sf.is_signer and sf.name != fname for sf in s.fields)
                    if has_any_other_signer and not any(
                        fname in sf.has_ones for sf in s.fields if sf.is_mut
                    ):
                        continue
                    _record(
                        s,
                        f"signer_type:{fname}",
                        f"missing `Signer` on `{f.name}: {f.wrapper_kind}` required by sibling(s) {{PEERS}}",
                        peer_names,
                        f.line,
                    )

    findings: list[Finding] = []
    for (_, _), (s, tmpl, peers, line) in sorted(
        hits.items(), key=lambda kv: (kv[1][0].path, kv[1][3], kv[1][0].name)
    ):
        plist = sorted(peers)
        pstr = ", ".join(plist[:3])
        if len(plist) > 3:
            pstr += f" (+{len(plist) - 3})"
        msg = tmpl.replace("{PEERS}", pstr)
        findings.append(
            Finding(
                detector="anchor_sibling_constraint_asymmetry",
                symbol=s.name,
                path=s.path,
                start_line=line,
                end_line=s.end_line,
                evidence=(msg,),
                resolution_source="static_ast",
                confidence="high",
            )
        )
    return findings


def audit_anchor_workspace(files: Mapping[str, str]) -> list[Finding]:
    """Run static sibling constraint asymmetry auditing across Anchor `.rs` sources."""
    multi_file = len(files) > 1
    parsed_files: dict[str, AnchorFileFacts] = {}
    by_program: dict[str, list[AnchorFileFacts]] = {}

    for path in sorted(files):
        norm = path.replace("\\", "/")
        if is_vendored_path(norm):
            continue
        if multi_file and _path_tier(norm) > 0:
            continue
        facts = parse_anchor_file(norm, files[path])
        parsed_files[norm] = facts
        by_program.setdefault(_program_scope_key(norm), []).append(facts)

    findings: list[Finding] = []
    for _, prog_files in sorted(by_program.items()):
        prog_structs = [s for pf in prog_files for s in pf.structs]
        prog_free_fns: dict[str, str] = {}
        for pf in prog_files:
            prog_free_fns.update(pf.free_fns)
        findings.extend(_audit_program_structs(prog_structs, parsed_files, prog_free_fns))

    return rank_findings(findings)
