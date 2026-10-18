#!/usr/bin/env python3
"""
lod_build.py — Semantic LOD Container Compiler (v0.3)
Supports Polyglot Repositories: Rust (.rs), Python (.py), and TypeScript/JavaScript (.ts, .tsx, .js, .jsx).

Key Capabilities:
  1. Dual-Plane Container: Working Context (goals, decisions, constraints) + System Map (L0-L3).
  2. Self-Contained Portability + Live SHA-256 Freshness: Embeds zlib+base64 source blobs.
  3. Token-Budgeted L0 World Map with Centrality Ranking & Exact Omission Accounting.
  4. Stateful Brace-Depth & Comment/String-Stripping Lexers for Rust & TypeScript/JS + AST for Python.
  5. Three-Tier Epistemic Typing: EXTRACTED_FACT vs. DECLARED_RULE vs. HEURISTIC_HAZARD.
  6. Cross-Language Symbol-Reference Graph for Transitive Blast Radius (Rust <-> Python <-> TS/JS).
  7. Prerequisite Gate Metadata for Stateful Fog-of-War Semantic Page Faults.
"""

import ast
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple

# --- Optional Hybrid Tree-Sitter AST Backend (Task 1) ---
try:
    import tree_sitter as _ts
    import tree_sitter_rust as _ts_rust
    import tree_sitter_python as _ts_python
    import tree_sitter_typescript as _ts_typescript

    _TS_LANG_RUST = _ts.Language(_ts_rust.language())
    _TS_LANG_PYTHON = _ts.Language(_ts_python.language())
    _TS_LANG_TS = _ts.Language(_ts_typescript.language_typescript())
    _TS_LANG_TSX = _ts.Language(_ts_typescript.language_tsx())

    RUST_TS_ITEM_QUERY = _ts.Query(_TS_LANG_RUST, """
      (struct_item name: (type_identifier) @name) @item
      (enum_item name: (type_identifier) @name) @item
      (trait_item name: (type_identifier) @name) @item
      (function_item name: (identifier) @name) @item
      (type_item name: (type_identifier) @name) @item
      (impl_item) @impl_item
      (mod_item name: (identifier) @name) @item
    """)
    RUST_TS_BOUND_QUERY = _ts.Query(_TS_LANG_RUST, """
      (trait_bounds) @bounds
      (where_clause) @where_pred
      (type_parameters) @type_param
    """)
    RUST_TS_CALL_QUERY = _ts.Query(_TS_LANG_RUST, """
      (call_expression function: (_) @call_fn)
      (macro_invocation macro: (_) @macro_fn)
    """)
    RUST_TS_IDENT_QUERY = _ts.Query(_TS_LANG_RUST, """
      (identifier) @ident
      (type_identifier) @ident
      (field_identifier) @ident
    """)

    TS_TS_CALL_QUERY = _ts.Query(_TS_LANG_TS, """
      (call_expression function: (_) @call_fn)
      (new_expression constructor: (_) @call_fn)
    """)
    TSX_TS_CALL_QUERY = _ts.Query(_TS_LANG_TSX, """
      (call_expression function: (_) @call_fn)
      (new_expression constructor: (_) @call_fn)
    """)
    PYTHON_TS_CALL_QUERY = _ts.Query(_TS_LANG_PYTHON, """
      (call function: (_) @call_fn)
    """)

    HAS_TREE_SITTER = True
except Exception:
    HAS_TREE_SITTER = False


def _run_ts_query(query_obj: Any, node: Any) -> Dict[str, List[Any]]:
    """Executes a compiled Tree-Sitter Query across both tree-sitter >=0.25 (QueryCursor) and 0.21-0.24."""
    if hasattr(_ts, "QueryCursor"):
        return _ts.QueryCursor(query_obj).captures(node)
    return query_obj.captures(node)


def _node_text(raw_bytes: bytes, node: Any) -> str:
    return raw_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


IGNORE_DIRS = {
    ".git", "target", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".turbo", "coverage"
}

# --- Rust Regex Patterns ---
RUST_ITEM_START_RE = re.compile(
    r"^\s*(?P<vis>pub(?:\([^)]+\))?\s+)?(?P<async>async\s+)?(?P<unsafe>unsafe\s+)?"
    r"(?P<kind>struct|enum|trait|fn|type|impl|mod)\s+(?P<rest>.+)$"
)
RUST_USE_RE = re.compile(r"^\s*(?:pub\s+)?use\s+([^;]+);", re.MULTILINE)
RUST_LIFETIME_RE = re.compile(r"'[a-z_][a-z0-9_]*")
RUST_CONST_RE = re.compile(
    r"^\s*(?P<vis>pub(?:\([^)]+\))?\s+)?(?P<kind>const|static)\s+(?P<name>[A-Z][A-Z0-9_]*)\s*:\s*(?P<type>[^=]+)=\s*(?P<val>[^;]+);"
)

# --- TypeScript / JavaScript Regex Patterns ---
TS_IMPORT_RE = re.compile(
    r"^\s*(?:import|export)\s+(?:type\s+)?(?:[^;\n]*?\s+from\s+)?['\"]([^'\"]+)['\"]",
    re.MULTILINE
)
TS_ITEM_START_RE = re.compile(
    r"^\s*(?P<export>export\s+(?:default\s+)?)?(?P<declare>declare\s+)?(?P<async>async\s+)?"
    r"(?P<kind>interface|type|class|function|enum|const|let)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)(?P<rest>.*)$"
)
TS_CONST_RE = re.compile(
    r"^\s*(?P<export>export\s+)?const\s+(?P<name>[A-Z][A-Z0-9_]*)\s*(?::\s*(?P<type>[^=]+))?=\s*(?P<val>[^;]+);?"
)

IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def estimate_tokens(text_or_obj: Any) -> int:
    """Conservative token estimator (~4 chars per token for structured JSON/code)."""
    raw = text_or_obj if isinstance(text_or_obj, str) else json.dumps(text_or_obj)
    return max(1, (len(raw) + 3) // 4)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_source_blob(text: str) -> str:
    compressed = zlib.compress(text.encode("utf-8"), level=9)
    return base64.b64encode(compressed).decode("ascii")


def get_git_commit(repo_path: Path) -> str:
    git_dir = repo_path / ".git"
    head_file = git_dir / "HEAD"
    if head_file.is_file():
        try:
            head_txt = head_file.read_text(encoding="utf-8").strip()
            if head_txt.startswith("ref:"):
                ref_rel = head_txt.split(":", 1)[1].strip()
                ref_file = git_dir / ref_rel
                if ref_file.is_file():
                    return ref_file.read_text(encoding="utf-8").strip()
            elif len(head_txt) >= 7:
                return head_txt
        except Exception:
            pass
    if not git_dir.exists():
        return "unversioned-snapshot"
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return "unversioned-snapshot"


def strip_c_style_comments_and_strings(line: str) -> str:
    """Removes string literals (single, double, template) and // line comments before brace counting."""
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])*'", "''", line)
    line = re.sub(r"`(?:\\.|[^`\\])*`", "``", line)
    line = re.sub(r"/\*.*?\*/", "", line)
    idx = line.find("//")
    if idx != -1:
        line = line[:idx]
    return line


def extract_rust_symbol_name(kind: str, header: str) -> str:
    cleaned = header.split("{")[0].split(";")[0].split("where")[0].strip()
    if kind == "impl":
        if " for " in cleaned:
            parts = cleaned.split(" for ", 1)
            trait_part = re.sub(r"^impl(?:<[^>]+>)?\s*", "", parts[0]).strip()
            type_part = parts[1].strip().split("<")[0].strip()
            return f"impl:{type_part}({trait_part})"
        no_impl = re.sub(r"^impl(?:<[^>]+>)?\s*", "", cleaned)
        return f"impl:{no_impl.split('<')[0].strip()}"
    token = cleaned.split("<")[0].split("(")[0].split(":")[0].split("=")[0].strip()
    return token or kind


def _extract_rust_leading_docs(lines: List[str], start_line: int) -> List[str]:
    docs: List[str] = []
    idx = start_line - 2
    while idx >= 0:
        s = lines[idx].strip()
        if s.startswith("///"):
            docs.insert(0, s[3:].strip())
            idx -= 1
        elif s.startswith("#["):
            idx -= 1
        else:
            break
    return docs


def _parse_rust_treesitter(raw_bytes: bytes, text: str, lines: List[str], rel_path: str) -> Dict[str, Any]:
    parser = _ts.Parser(_TS_LANG_RUST)
    tree = parser.parse(raw_bytes)
    root = tree.root_node

    imports: List[str] = []
    extracted_facts: List[Dict[str, Any]] = []
    declared_rules: List[Dict[str, Any]] = []
    heuristic_hazards: List[Dict[str, Any]] = []
    contracts: List[str] = []
    mechanics: Dict[str, Any] = {}
    defined_symbols: Set[str] = set()
    module_constants: Dict[str, Dict[str, Any]] = {}

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("//!"):
            doc_line = stripped[3:].strip()
            if doc_line:
                declared_rules.append({
                    "epistemic_type": "DECLARED_RULE",
                    "source": f"{rel_path}:{line_no}",
                    "statement": doc_line
                })
        cm = RUST_CONST_RE.match(strip_c_style_comments_and_strings(line))
        if cm:
            cname = cm.group("name").strip()
            cval = cm.group("val").strip()
            ctype = cm.group("type").strip()
            defined_symbols.add(cname)
            module_constants[cname] = {
                "value": cval,
                "type": ctype,
                "line": line_no,
                "defined_in": f"{rel_path}:{line_no}",
                "source_line": f"{line_no}: {stripped}"
            }
            if cm.group("vis"):
                contracts.append(re.sub(r"\s+", " ", stripped.rstrip(";")))

    KIND_MAP = {
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "function_item": "fn",
        "type_item": "type",
        "impl_item": "impl",
        "mod_item": "mod"
    }

    candidate_nodes: List[Tuple[Any, Optional[str]]] = []
    for child in root.children:
        if child.type == "use_declaration":
            u_txt = _node_text(raw_bytes, child).strip()
            u_txt = re.sub(r"^(?:pub\s+)?use\s+", "", u_txt).rstrip(";").strip()
            imports.append(u_txt)
        elif child.type in KIND_MAP:
            if child.type == "impl_item":
                header_preview = _node_text(raw_bytes, child).split("{")[0].strip()
                has_generic_impl = header_preview.startswith("impl<")
                if not has_generic_impl:
                    candidate_nodes.append((child, None))
                body_node = child.child_by_field_name("body")
                if body_node and has_generic_impl:
                    for sub in body_node.children:
                        if sub.type == "function_item":
                            candidate_nodes.append((sub, header_preview))
            else:
                candidate_nodes.append((child, None))

    RUST_STOPWORDS = {
        "pub", "fn", "let", "mut", "self", "Self", "impl", "struct", "enum", "trait",
        "match", "if", "else", "for", "while", "loop", "return", "true", "false",
        "Result", "Ok", "Err", "Option", "Some", "None", "u8", "u16", "u32", "u64",
        "usize", "i32", "i64", "String", "Vec", "str", "bool"
    }

    for node, parent_impl_header in candidate_nodes:
        kind = KIND_MAP[node.type]
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        node_src = _node_text(raw_bytes, node)
        sig_clean = re.sub(r"\s+", " ", node_src.split("{")[0].rstrip(";")).strip()
        is_pub = sig_clean.startswith("pub ") or sig_clean.startswith("pub(")
        is_async = "async fn " in sig_clean or sig_clean.startswith("async ")
        is_unsafe = "unsafe " in sig_clean

        name_node = node.child_by_field_name("name")
        if kind == "impl":
            sym_name = extract_rust_symbol_name("impl", sig_clean)
        elif name_node is not None:
            sym_name = _node_text(raw_bytes, name_node).strip()
        else:
            sym_name = extract_rust_symbol_name(kind, sig_clean)

        sym_id = f"sym://{rel_path}#{sym_name}"
        if not sym_name.startswith("impl:"):
            defined_symbols.add(sym_name)

        if is_pub or kind in ("struct", "enum", "trait", "impl", "type"):
            contracts.append(sig_clean)
            extracted_facts.append({
                "epistemic_type": "EXTRACTED_FACT",
                "symbol": sym_id,
                "fact": f"Declares {kind} `{sym_name}` at lines {start_line}-{end_line}: `{sig_clean}`"
            })

        pending_docs = _extract_rust_leading_docs(lines, start_line)
        for doc_str in pending_docs:
            if any(k in doc_str.lower() for k in ("invariant", "must", "never", "safety", "contract", "requires")):
                declared_rules.append({
                    "epistemic_type": "DECLARED_RULE",
                    "source": f"{sym_id} (docstring)",
                    "statement": doc_str
                })

        # Extract trait bounds via Tree-Sitter AST query
        bound_captures = _run_ts_query(RUST_TS_BOUND_QUERY, node)
        trait_bounds: List[str] = []
        for cap_list in bound_captures.values():
            for bnode in cap_list:
                btxt = re.sub(r"\s+", " ", _node_text(raw_bytes, bnode).lstrip(":").strip())
                if btxt and btxt not in trait_bounds:
                    trait_bounds.append(btxt)
        if parent_impl_header and ":" in parent_impl_header:
            trait_bounds.append(parent_impl_header)

        # Extract call-graph identifiers via Tree-Sitter AST query
        call_captures = _run_ts_query(RUST_TS_CALL_QUERY, node)
        call_idents: List[str] = []
        for cap_list in call_captures.values():
            for cnode in cap_list:
                ctxt = re.sub(r"\s+", "", _node_text(raw_bytes, cnode))
                if ctxt:
                    call_idents.append(ctxt)

        # Extract AST identifiers inside node
        ident_captures = _run_ts_query(RUST_TS_IDENT_QUERY, node)
        ast_idents = {
            _node_text(raw_bytes, inode)
            for cap_list in ident_captures.values()
            for inode in cap_list
        }
        body_idents = sorted((ast_idents | set(IDENT_RE.findall(node_src))) - RUST_STOPWORDS - {sym_name})

        lifetimes = sorted(set(RUST_LIFETIME_RE.findall(sig_clean)))
        item_hazards = []
        if lifetimes:
            h_msg = f"Symbol `{sym_name}` uses explicit lifetime parameter(s) {', '.join(lifetimes)}; verify borrow scope."
            if is_async or ".await" in node_src:
                h_msg += " Contains async/.await with borrowed lifetimes (check Send / borrow-across-await constraints)."
            item_hazards.append(h_msg)

        if is_unsafe or re.search(r"\bunsafe\b", node_src):
            item_hazards.append(f"Symbol `{sym_name}` contains `unsafe` code; memory/aliasing invariants are manually upheld.")

        for h in item_hazards:
            heuristic_hazards.append({
                "epistemic_type": "HEURISTIC_HAZARD",
                "symbol": sym_id,
                "warning": h,
                "verification_required": "Inspect L3 source and run `cargo check`."
            })

        mechanics[sym_id] = {
            "signature": sig_clean,
            "kind": kind,
            "is_public": is_pub,
            "lines": [start_line, end_line],
            "line_count": end_line - start_line + 1,
            "docstring": " ".join(pending_docs) if pending_docs else None,
            "trait_bounds": trait_bounds,
            "call_graph_identifiers": sorted(set(call_idents))[:30],
            "referenced_identifiers": body_idents[:40],
            "omitted_in_l2": {
                "omitted_categories": ["statement-level expressions", "exact macro expansion tokens"],
                "omitted_identifier_count": max(0, len(body_idents) - 40)
            },
            "gate_policy": {
                "requires_l1_mounted": True,
                "expand_to_l3_when": (
                    item_hazards[0] if item_hazards else f"Modifying implementation body of `{sym_name}`"
                )
            }
        }

        if kind == "impl":
            impl_type = sym_name.replace("impl:", "").split("<", 1)[0].strip()
            body_node = node.child_by_field_name("body")
            if body_node is not None:
                for sub in body_node.children:
                    if sub.type == "function_item":
                        m_name_node = sub.child_by_field_name("name")
                        if m_name_node is None:
                            continue
                        m_name = _node_text(raw_bytes, m_name_node).strip()
                        m_qual = f"{impl_type}.{m_name}"
                        m_sym_id = f"sym://{rel_path}#{m_qual}"
                        m_start = sub.start_point[0] + 1
                        m_end = sub.end_point[0] + 1
                        m_src = _node_text(raw_bytes, sub)
                        m_sig = re.sub(r"\s+", " ", m_src.split("{")[0].rstrip(";")).strip()
                        m_calls = [
                            _node_text(raw_bytes, cnode).strip()
                            for cap_list in _run_ts_query(RUST_TS_CALL_QUERY, sub).values()
                            for cnode in cap_list
                        ]
                        m_idents = sorted((set(IDENT_RE.findall(m_src)) - RUST_STOPWORDS - {m_name}))
                        mechanics[m_sym_id] = {
                            "signature": m_sig,
                            "kind": "method",
                            "is_public": m_sig.startswith("pub "),
                            "lines": [m_start, m_end],
                            "line_count": m_end - m_start + 1,
                            "docstring": None,
                            "trait_bounds": [],
                            "call_graph_identifiers": sorted(set(m_calls))[:30],
                            "referenced_identifiers": m_idents[:40],
                            "omitted_in_l2": {
                                "omitted_categories": ["statement-level expressions"],
                                "omitted_identifier_count": max(0, len(m_idents) - 40)
                            },
                            "gate_policy": {
                                "requires_l1_mounted": True,
                                "expand_to_l3_when": f"Modifying method `{m_qual}`"
                            }
                        }

    root_idents = {
        _node_text(raw_bytes, inode)
        for cap_list in _run_ts_query(RUST_TS_IDENT_QUERY, root).values()
        for inode in cap_list
    }
    all_identifiers = (root_idents | set(IDENT_RE.findall(text))) - set(defined_symbols)

    return {
        "language": "rust",
        "parser_backend": "tree_sitter",
        "sha256": sha256_bytes(raw_bytes),
        "line_count": len(lines),
        "embedded_source_b64_zlib": encode_source_blob(text),
        "imports": imports,
        "defined_symbols": sorted(defined_symbols),
        "module_constants": module_constants,
        "referenced_identifiers": sorted(all_identifiers),
        "contracts": contracts,
        "epistemics": {
            "extracted_facts": extracted_facts,
            "declared_rules": list({r["statement"]: r for r in declared_rules}.values()),
            "heuristic_hazards": heuristic_hazards
        },
        "loss": {
            "omitted_at_l1": [
                f"{len(mechanics)} symbol implementation bodies ({len(lines)} lines of raw Rust source)",
                "local control-flow branches and private expressions"
            ],
            "truncated_contracts_count": 0,
            "mandatory_expand_triggers": [
                "Before modifying any public struct, trait, or fn signature",
                "Before editing any symbol flagged with a HEURISTIC_HAZARD or DECLARED_RULE"
            ]
        },
        "l2_mechanics": mechanics
    }


def parse_rust_file(path: Path, rel_path: str, use_tree_sitter: Optional[bool] = None) -> Dict[str, Any]:
    raw_bytes = path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if (use_tree_sitter if use_tree_sitter is not None else HAS_TREE_SITTER) and HAS_TREE_SITTER:
        return _parse_rust_treesitter(raw_bytes, text, lines, rel_path)
    imports = [m.group(1).strip() for m in RUST_USE_RE.finditer(text)]

    extracted_facts: List[Dict[str, Any]] = []
    declared_rules: List[Dict[str, Any]] = []
    heuristic_hazards: List[Dict[str, Any]] = []
    contracts: List[str] = []
    mechanics: Dict[str, Any] = {}
    defined_symbols: Set[str] = set()
    module_constants: Dict[str, Dict[str, Any]] = {}

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("//!"):
            doc_line = stripped[3:].strip()
            if doc_line:
                declared_rules.append({
                    "epistemic_type": "DECLARED_RULE",
                    "source": f"{rel_path}:{line_no}",
                    "statement": doc_line
                })
        cm = RUST_CONST_RE.match(strip_c_style_comments_and_strings(line))
        if cm:
            cname = cm.group("name").strip()
            cval = cm.group("val").strip()
            ctype = cm.group("type").strip()
            defined_symbols.add(cname)
            module_constants[cname] = {
                "value": cval,
                "type": ctype,
                "line": line_no,
                "defined_in": f"{rel_path}:{line_no}",
                "source_line": f"{line_no}: {stripped}"
            }
            if cm.group("vis"):
                contracts.append(re.sub(r"\s+", " ", stripped.rstrip(";")))

    i = 0
    n = len(lines)
    pending_docs: List[str] = []

    while i < n:
        stripped = lines[i].strip()
        if stripped.startswith("///"):
            pending_docs.append(stripped[3:].strip())
            i += 1
            continue
        elif stripped.startswith("#["):
            i += 1
            continue

        m = RUST_ITEM_START_RE.match(lines[i])
        if not m:
            if stripped and not stripped.startswith("//"):
                pending_docs.clear()
            i += 1
            continue

        start_line = i + 1
        kind = m.group("kind")
        is_pub = bool(m.group("vis"))
        is_async = bool(m.group("async"))
        is_unsafe = bool(m.group("unsafe"))

        header_lines = []
        brace_depth = 0
        opened_brace = False
        end_line = start_line

        j = i
        while j < n:
            code_only = strip_c_style_comments_and_strings(lines[j])
            if not opened_brace:
                header_lines.append(lines[j].strip())
                if ";" in code_only and "{" not in code_only:
                    end_line = j + 1
                    break
            if "{" in code_only:
                opened_brace = True
            if opened_brace:
                brace_depth += code_only.count("{") - code_only.count("}")
                if brace_depth <= 0:
                    end_line = j + 1
                    break
            j += 1
        else:
            end_line = n

        header_text = " ".join(header_lines)
        sig_clean = re.sub(r"\s+", " ", header_text.split("{")[0].rstrip(";")).strip()
        sym_name = extract_rust_symbol_name(kind, m.group("rest"))
        sym_id = f"sym://{rel_path}#{sym_name}"

        if not sym_name.startswith("impl:"):
            defined_symbols.add(sym_name)

        if is_pub or kind in ("struct", "enum", "trait", "impl", "type"):
            contracts.append(sig_clean)
            extracted_facts.append({
                "epistemic_type": "EXTRACTED_FACT",
                "symbol": sym_id,
                "fact": f"Declares {kind} `{sym_name}` at lines {start_line}-{end_line}: `{sig_clean}`"
            })

        for doc_str in pending_docs:
            if any(k in doc_str.lower() for k in ("invariant", "must", "never", "safety", "contract", "requires")):
                declared_rules.append({
                    "epistemic_type": "DECLARED_RULE",
                    "source": f"{sym_id} (docstring)",
                    "statement": doc_str
                })

        item_body_text = "\n".join(lines[start_line - 1:end_line])
        lifetimes = sorted(set(RUST_LIFETIME_RE.findall(sig_clean)))
        item_hazards = []

        if lifetimes:
            h_msg = f"Symbol `{sym_name}` uses explicit lifetime parameter(s) {', '.join(lifetimes)}; verify borrow scope."
            if is_async or ".await" in item_body_text:
                h_msg += " Contains async/.await with borrowed lifetimes (check Send / borrow-across-await constraints)."
            item_hazards.append(h_msg)

        if is_unsafe or re.search(r"\bunsafe\b", item_body_text):
            item_hazards.append(f"Symbol `{sym_name}` contains `unsafe` code; memory/aliasing invariants are manually upheld.")

        for h in item_hazards:
            heuristic_hazards.append({
                "epistemic_type": "HEURISTIC_HAZARD",
                "symbol": sym_id,
                "warning": h,
                "verification_required": "Inspect L3 source and run `cargo check`."
            })

        body_idents = sorted(set(IDENT_RE.findall(item_body_text)) - {
            "pub", "fn", "let", "mut", "self", "Self", "impl", "struct", "enum", "trait",
            "match", "if", "else", "for", "while", "loop", "return", "true", "false",
            "Result", "Ok", "Err", "Option", "Some", "None", "u8", "u16", "u32", "u64",
            "usize", "i32", "i64", "String", "Vec", "str", "bool", sym_name
        })

        mechanics[sym_id] = {
            "signature": sig_clean,
            "kind": kind,
            "is_public": is_pub,
            "lines": [start_line, end_line],
            "line_count": end_line - start_line + 1,
            "docstring": " ".join(pending_docs) if pending_docs else None,
            "referenced_identifiers": body_idents[:40],
            "omitted_in_l2": {
                "omitted_categories": ["statement-level expressions", "exact macro expansion tokens"],
                "omitted_identifier_count": max(0, len(body_idents) - 40)
            },
            "gate_policy": {
                "requires_l1_mounted": True,
                "expand_to_l3_when": (
                    item_hazards[0] if item_hazards else f"Modifying implementation body of `{sym_name}`"
                )
            }
        }

        pending_docs.clear()
        i = max(i + 1, end_line)

    all_identifiers = set(IDENT_RE.findall(text)) - set(defined_symbols)

    return {
        "language": "rust",
        "parser_backend": "stateful_lexer",
        "sha256": sha256_bytes(raw_bytes),
        "line_count": len(lines),
        "embedded_source_b64_zlib": encode_source_blob(text),
        "imports": imports,
        "defined_symbols": sorted(defined_symbols),
        "module_constants": module_constants,
        "referenced_identifiers": sorted(all_identifiers),
        "contracts": contracts,
        "epistemics": {
            "extracted_facts": extracted_facts,
            "declared_rules": list({r["statement"]: r for r in declared_rules}.values()),
            "heuristic_hazards": heuristic_hazards
        },
        "loss": {
            "omitted_at_l1": [
                f"{len(mechanics)} symbol implementation bodies ({len(lines)} lines of raw Rust source)",
                "local control-flow branches and private expressions"
            ],
            "truncated_contracts_count": 0,
            "mandatory_expand_triggers": [
                "Before modifying any public struct, trait, or fn signature",
                "Before editing any symbol flagged with a HEURISTIC_HAZARD or DECLARED_RULE"
            ]
        },
        "l2_mechanics": mechanics
    }


def parse_ts_js_file(path: Path, rel_path: str, use_tree_sitter: Optional[bool] = None) -> Dict[str, Any]:
    """
    Hybrid Tree-Sitter AST + Stateful Lexer parser for TypeScript & JavaScript (.ts, .tsx, .js, .jsx).
    Extracts JSDoc/module rules, exported interfaces/types/classes/functions/hooks,
    exact AST spans/bounds/calls when tree-sitter is installed, and flags TS/JS-specific heuristic hazards.
    """
    raw_bytes = path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    ext = path.suffix.lower()
    lang_label = "typescript" if ext in (".ts", ".tsx") else "javascript"
    ts_enabled = (use_tree_sitter if use_tree_sitter is not None else HAS_TREE_SITTER) and HAS_TREE_SITTER

    ts_tree = None
    ts_call_query = None
    if ts_enabled:
        ts_lang = _TS_LANG_TSX if ext in (".tsx", ".jsx") else _TS_LANG_TS
        ts_call_query = TSX_TS_CALL_QUERY if ext in (".tsx", ".jsx") else TS_TS_CALL_QUERY
        ts_tree = _ts.Parser(ts_lang).parse(raw_bytes)

    imports = [m.group(1).strip() for m in TS_IMPORT_RE.finditer(text)]
    extracted_facts: List[Dict[str, Any]] = []
    declared_rules: List[Dict[str, Any]] = []
    heuristic_hazards: List[Dict[str, Any]] = []
    contracts: List[str] = []
    mechanics: Dict[str, Any] = {}
    defined_symbols: Set[str] = set()
    module_constants: Dict[str, Dict[str, Any]] = {}

    # 1. Extract top-of-file module rules from leading comments (`//` or `/** ... */`) and module constants
    in_block_comment = False
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not line.startswith((" ", "\t")):
            cm = TS_CONST_RE.match(strip_c_style_comments_and_strings(line))
            if cm and "=>" not in cm.group("val"):
                cname = cm.group("name").strip()
                cval = cm.group("val").strip()
                ctype = (cm.group("type") or "").strip() or None
                defined_symbols.add(cname)
                module_constants[cname] = {
                    "value": cval,
                    "type": ctype,
                    "line": line_no,
                    "defined_in": f"{rel_path}:{line_no}",
                    "source_line": f"{line_no}: {stripped}"
                }
        if line_no <= 25:
            if stripped.startswith("//"):
                c_text = stripped[2:].strip()
                if any(k in c_text.lower() for k in ("invariant", "must", "never", "rule", "contract", "canonical")):
                    declared_rules.append({
                        "epistemic_type": "DECLARED_RULE",
                        "source": f"{rel_path}:{line_no}",
                        "statement": c_text
                    })
            elif stripped.startswith("/*"):
                in_block_comment = True
            if in_block_comment:
                clean_doc = stripped.lstrip("/*").rstrip("*/").strip()
                if any(k in clean_doc.lower() for k in ("invariant", "must", "never", "rule", "contract", "canonical")):
                    declared_rules.append({
                        "epistemic_type": "DECLARED_RULE",
                        "source": f"{rel_path}:{line_no}",
                        "statement": clean_doc
                    })
                if "*/" in stripped:
                    in_block_comment = False

    # Map line -> Tree-Sitter declaration node for exact AST span, heritage bounds, and call-graph extraction
    ts_nodes_by_start_line: Dict[int, Any] = {}
    if ts_tree is not None:
        for child in ts_tree.root_node.children:
            ts_nodes_by_start_line[child.start_point[0] + 1] = child

    # 2. Hybrid Tree-Sitter AST / Stateful brace-semicolon scanner for TS/JS declarations
    i = 0
    n = len(lines)
    pending_jsdoc: List[str] = []
    in_jsdoc = False

    while i < n:
        stripped = lines[i].strip()

        # Collect JSDoc `/** ... */` or `//` immediately preceding declarations
        if stripped.startswith("/**"):
            in_jsdoc = True
            cleaned = stripped.lstrip("/*").rstrip("*/").strip()
            if cleaned:
                pending_jsdoc.append(cleaned)
            if "*/" in stripped:
                in_jsdoc = False
            i += 1
            continue
        elif in_jsdoc:
            cleaned = stripped.lstrip("*").rstrip("*/").strip()
            if cleaned:
                pending_jsdoc.append(cleaned)
            if "*/" in stripped:
                in_jsdoc = False
            i += 1
            continue
        elif stripped.startswith("//"):
            c_line = stripped[2:].strip()
            if any(k in c_line.lower() for k in ("invariant", "must", "never", "contract", "safety", "requires")):
                pending_jsdoc.append(c_line)
            i += 1
            continue
        elif stripped.startswith("@"):  # Decorator
            i += 1
            continue

        m = TS_ITEM_START_RE.match(lines[i])
        if not m:
            if stripped:
                pending_jsdoc.clear()
            i += 1
            continue

        kind = m.group("kind")
        sym_name = m.group("name")
        is_export = bool(m.group("export"))
        is_async = bool(m.group("async"))
        rest = m.group("rest") or ""

        # For `const` or `let`, keep exported constants, arrow functions, or React components/hooks
        if kind in ("const", "let") and not (
            is_export or "=>" in rest or sym_name[0].isupper() or sym_name.startswith("use")
        ):
            pending_jsdoc.clear()
            i += 1
            continue

        start_line = i + 1
        header_lines = []
        brace_depth = 0
        paren_depth = 0
        opened_brace = False
        end_line = start_line

        j = i
        while j < n:
            code_only = strip_c_style_comments_and_strings(lines[j])
            paren_depth += code_only.count("(") - code_only.count(")")
            if not opened_brace:
                header_lines.append(lines[j].strip())
                # Type alias or single-line const ending in `;` outside parens/braces
                if ";" in code_only and "{" not in code_only and paren_depth <= 0:
                    end_line = j + 1
                    break
            if "{" in code_only:
                opened_brace = True
            if opened_brace:
                brace_depth += code_only.count("{") - code_only.count("}")
                if brace_depth <= 0 and paren_depth <= 0:
                    end_line = j + 1
                    break
            j += 1
        else:
            end_line = n

        ts_node = ts_nodes_by_start_line.get(start_line)
        call_graph_idents: List[str] = []
        trait_bounds: List[str] = []
        if ts_node is not None and ts_call_query is not None:
            end_line = ts_node.end_point[0] + 1
            for cap_list in _run_ts_query(ts_call_query, ts_node).values():
                for cnode in cap_list:
                    ctxt = re.sub(r"\s+", "", _node_text(raw_bytes, cnode))
                    if ctxt:
                        call_graph_idents.append(ctxt)

        header_text = " ".join(header_lines)
        if kind == "type" and "=" in header_text:
            sig_clean = re.sub(r"\s+", " ", header_text.rstrip(";")).strip()
        else:
            sig_clean = re.sub(r"\s+", " ", header_text.split("{")[0].rstrip(";")).strip()

        for bound_match in re.finditer(r"\b(?:extends|implements)\s+([^{;]+)", sig_clean):
            trait_bounds.append(bound_match.group(1).strip())

        sym_id = f"sym://{rel_path}#{sym_name}"
        defined_symbols.add(sym_name)

        if is_export or kind in ("interface", "type", "class", "enum", "function"):
            contracts.append(sig_clean)
            extracted_facts.append({
                "epistemic_type": "EXTRACTED_FACT",
                "symbol": sym_id,
                "fact": f"Declares {kind} `{sym_name}` at lines {start_line}-{end_line}: `{sig_clean}`"
            })

        for doc_str in pending_jsdoc:
            if any(k in doc_str.lower() for k in ("invariant", "must", "never", "contract", "safety", "requires")):
                declared_rules.append({
                    "epistemic_type": "DECLARED_RULE",
                    "source": f"{sym_id} (JSDoc)",
                    "statement": doc_str
                })

        item_body_text = "\n".join(lines[start_line - 1:end_line])
        item_hazards = []

        if re.search(r":\s*any\b|<any>|\bas\s+any\b", item_body_text):
            item_hazards.append(f"Symbol `{sym_name}` uses explicit `any` type escape; static type guarantees bypassed.")
        if "dangerouslySetInnerHTML" in item_body_text or re.search(r"\beval\s*\(", item_body_text):
            item_hazards.append(f"Symbol `{sym_name}` uses `dangerouslySetInnerHTML` or `eval()`; verify XSS/injection sanitization.")
        if is_async and "catch" not in item_body_text and "try" not in item_body_text:
            item_hazards.append(f"Async symbol `{sym_name}` has no local try/catch block; verify caller handles rejected promises.")

        for h in item_hazards:
            heuristic_hazards.append({
                "epistemic_type": "HEURISTIC_HAZARD",
                "symbol": sym_id,
                "warning": h,
                "verification_required": "Inspect L3 source and run `tsc --noEmit`."
            })

        body_idents = sorted(set(IDENT_RE.findall(item_body_text)) - {
            "export", "default", "interface", "type", "class", "function", "const", "let", "var",
            "return", "if", "else", "for", "while", "switch", "case", "break", "continue", "async",
            "await", "try", "catch", "finally", "new", "this", "true", "false", "null", "undefined",
            "string", "number", "boolean", "void", "any", "unknown", "never", "Promise", "Record",
            "Array", "Readonly", sym_name
        })

        mechanics[sym_id] = {
            "signature": sig_clean,
            "kind": kind,
            "is_public": is_export,
            "lines": [start_line, end_line],
            "line_count": end_line - start_line + 1,
            "docstring": " ".join(pending_jsdoc) if pending_jsdoc else None,
            "trait_bounds": trait_bounds,
            "call_graph_identifiers": sorted(set(call_graph_idents))[:30],
            "referenced_identifiers": body_idents[:40],
            "omitted_in_l2": {
                "omitted_categories": ["statement bodies", "JSX/template markup details"],
                "omitted_identifier_count": max(0, len(body_idents) - 40)
            },
            "gate_policy": {
                "requires_l1_mounted": True,
                "expand_to_l3_when": (
                    item_hazards[0] if item_hazards else f"Modifying implementation or type contract of `{sym_name}`"
                )
            }
        }

        pending_jsdoc.clear()
        i = max(i + 1, end_line)

    all_identifiers = set(IDENT_RE.findall(text)) - set(defined_symbols)

    return {
        "language": lang_label,
        "parser_backend": "tree_sitter" if ts_enabled else "stateful_lexer",
        "sha256": sha256_bytes(raw_bytes),
        "line_count": len(lines),
        "embedded_source_b64_zlib": encode_source_blob(text),
        "imports": imports,
        "defined_symbols": sorted(defined_symbols),
        "module_constants": module_constants,
        "referenced_identifiers": sorted(all_identifiers),
        "contracts": contracts,
        "epistemics": {
            "extracted_facts": extracted_facts,
            "declared_rules": list({r["statement"]: r for r in declared_rules}.values()),
            "heuristic_hazards": heuristic_hazards
        },
        "loss": {
            "omitted_at_l1": [
                f"{len(mechanics)} symbol bodies ({len(lines)} lines of {lang_label} source)",
                "internal closures, JSX trees, and local state transitions"
            ],
            "truncated_contracts_count": 0,
            "mandatory_expand_triggers": [
                "Before modifying any exported interface, type alias, class, or function signature",
                "Before editing any symbol flagged with a HEURISTIC_HAZARD or DECLARED_RULE"
            ]
        },
        "l2_mechanics": mechanics
    }


def parse_python_file(path: Path, rel_path: str, use_tree_sitter: Optional[bool] = None) -> Dict[str, Any]:
    raw_bytes = path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    ts_enabled = (use_tree_sitter if use_tree_sitter is not None else HAS_TREE_SITTER) and HAS_TREE_SITTER

    ts_nodes_by_line: Dict[int, Any] = {}
    if ts_enabled:
        ts_tree = _ts.Parser(_TS_LANG_PYTHON).parse(raw_bytes)
        for child in ts_tree.root_node.children:
            ts_nodes_by_line[child.start_point[0] + 1] = child

    extracted_facts: List[Dict[str, Any]] = []
    declared_rules: List[Dict[str, Any]] = []
    heuristic_hazards: List[Dict[str, Any]] = []
    imports: List[str] = []
    contracts: List[str] = []
    mechanics: Dict[str, Any] = {}
    defined_symbols: Set[str] = set()
    module_constants: Dict[str, Dict[str, Any]] = {}

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return {
            "language": "python",
            "parser_backend": "tree_sitter" if ts_enabled else "python_ast",
            "sha256": sha256_bytes(raw_bytes),
            "line_count": len(lines),
            "embedded_source_b64_zlib": encode_source_blob(text),
            "parse_error": str(exc),
            "imports": [],
            "defined_symbols": [],
            "module_constants": {},
            "referenced_identifiers": [],
            "contracts": [],
            "epistemics": {"extracted_facts": [], "declared_rules": [], "heuristic_hazards": []},
            "loss": {"omitted_at_l1": ["File had SyntaxError; inspect L3 directly"]},
            "l2_mechanics": {}
        }

    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        for line in mod_doc.splitlines():
            clean = line.strip()
            if any(k in clean.lower() for k in ("invariant", "must", "never", "rule", "contract", "canonical")):
                declared_rules.append({
                    "epistemic_type": "DECLARED_RULE",
                    "source": f"{rel_path}:module_docstring",
                    "statement": clean
                })

    for sub_imp in ast.walk(tree):
        if isinstance(sub_imp, (ast.Import, ast.ImportFrom)):
            unparsed_imp = ast.unparse(sub_imp)
            if unparsed_imp not in imports:
                imports.append(unparsed_imp)

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and re.match(r"^_?[A-Z][A-Z0-9_]+$", tgt.id):
                    cname = tgt.id
                    cval = re.sub(r"\s+", " ", ast.unparse(node.value)).strip()[:120]
                    defined_symbols.add(cname)
                    src_ln = lines[node.lineno - 1].strip() if 1 <= node.lineno <= len(lines) else f"{cname} = {cval}"
                    module_constants[cname] = {
                        "value": cval,
                        "line": node.lineno,
                        "defined_in": f"{rel_path}:{node.lineno}",
                        "source_line": f"{node.lineno}: {src_ln[:140]}"
                    }
                    contracts.append(f"{cname} = {cval}")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            if re.match(r"^_?[A-Z][A-Z0-9_]+$", node.target.id):
                cname = node.target.id
                cval = re.sub(r"\s+", " ", ast.unparse(node.value)).strip()[:120]
                ctype = ast.unparse(node.annotation)
                defined_symbols.add(cname)
                src_ln = lines[node.lineno - 1].strip() if 1 <= node.lineno <= len(lines) else f"{cname}: {ctype} = {cval}"
                module_constants[cname] = {
                    "value": cval,
                    "type": ctype,
                    "line": node.lineno,
                    "defined_in": f"{rel_path}:{node.lineno}",
                    "source_line": f"{node.lineno}: {src_ln[:140]}"
                }
                contracts.append(f"{cname}: {ctype} = {cval}")
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            defined_symbols.add(name)
            sym_id = f"sym://{rel_path}#{name}"
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", start_line + 15)
            ts_node = ts_nodes_by_line.get(start_line)
            if ts_node is not None:
                end_line = ts_node.end_point[0] + 1

            trait_bounds: List[str] = []
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(b) for b in node.bases]
                trait_bounds = list(bases)
                methods = [
                    n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                sig = f"class {name}({', '.join(bases)}) [methods: {', '.join(methods)}]"
                kind = "class"
            else:
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                args = ast.unparse(node.args)
                ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                sig = f"{prefix} {name}({args}){ret}"
                kind = "async_fn" if isinstance(node, ast.AsyncFunctionDef) else "fn"

            contracts.append(sig)
            extracted_facts.append({
                "epistemic_type": "EXTRACTED_FACT",
                "symbol": sym_id,
                "fact": f"Defines {kind} `{name}` at lines {start_line}-{end_line}: `{sig}`"
            })

            doc = ast.get_docstring(node) or ""
            for dline in doc.splitlines():
                if any(k in dline.lower() for k in ("invariant", "must", "never", "requires", "contract")):
                    declared_rules.append({
                        "epistemic_type": "DECLARED_RULE",
                        "source": f"{sym_id} (docstring)",
                        "statement": dline.strip()
                    })

            calls_in_node = []
            if ts_node is not None:
                for cap_list in _run_ts_query(PYTHON_TS_CALL_QUERY, ts_node).values():
                    for cnode in cap_list:
                        calls_in_node.append(_node_text(raw_bytes, cnode).strip())
            has_global = False
            has_bare_except = False
            node_idents: Set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    try:
                        calls_in_node.append(ast.unparse(sub.func))
                    except Exception:
                        pass
                elif isinstance(sub, ast.Name):
                    node_idents.add(sub.id)
                elif isinstance(sub, ast.Global):
                    has_global = True
                elif isinstance(sub, ast.ExceptHandler) and sub.type is None:
                    has_bare_except = True

            if has_global:
                heuristic_hazards.append({
                    "epistemic_type": "HEURISTIC_HAZARD",
                    "symbol": sym_id,
                    "warning": f"`{name}` mutates module-level `global` state.",
                    "verification_required": "Check concurrency and caller assumptions in L3."
                })
            if has_bare_except:
                heuristic_hazards.append({
                    "epistemic_type": "HEURISTIC_HAZARD",
                    "symbol": sym_id,
                    "warning": f"`{name}` contains a bare `except:` clause that may swallow system exits or errors.",
                    "verification_required": "Inspect exception handling in L3."
                })

            unique_calls = sorted(set(calls_in_node))
            sorted_node_idents = sorted(node_idents - {"self", "cls", "True", "False", "None", name})
            mechanics[sym_id] = {
                "signature": sig,
                "kind": kind,
                "is_public": not name.startswith("_"),
                "lines": [start_line, end_line],
                "line_count": end_line - start_line + 1,
                "docstring": doc.splitlines()[0] if doc else None,
                "trait_bounds": trait_bounds,
                "outgoing_calls": unique_calls[:200],
                "referenced_identifiers": sorted_node_idents[:200],
                "omitted_in_l2": {
                    "omitted_categories": ["statement bodies", "local variable assignments"],
                    "omitted_call_count": max(0, len(unique_calls) - 200)
                },
                "gate_policy": {
                    "requires_l1_mounted": True,
                    "expand_to_l3_when": f"Editing control flow or return contract of `{name}`"
                }
            }

            # Also index individual class methods as qualified symbols (`sym://path#Class.method`)
            # for Codex-style cross-file symbol discovery and targeted L3 method slicing.
            if isinstance(node, ast.ClassDef):
                for mnode in node.body:
                    if isinstance(mnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        m_qualname = f"{name}.{mnode.name}"
                        m_sym_id = f"sym://{rel_path}#{m_qualname}"
                        m_start = mnode.lineno
                        m_end = getattr(mnode, "end_lineno", m_start + 10)
                        m_prefix = "async def" if isinstance(mnode, ast.AsyncFunctionDef) else "def"
                        m_args = ast.unparse(mnode.args)
                        m_ret = f" -> {ast.unparse(mnode.returns)}" if mnode.returns else ""
                        m_sig = f"{m_prefix} {m_qualname}({m_args}){m_ret}"
                        m_kind = "async_method" if isinstance(mnode, ast.AsyncFunctionDef) else "method"
                        m_doc = ast.get_docstring(mnode) or ""
                        m_calls = []
                        m_idents: Set[str] = set()
                        for msub in ast.walk(mnode):
                            if isinstance(msub, ast.Call):
                                try:
                                    m_calls.append(ast.unparse(msub.func))
                                except Exception:
                                    pass
                            elif isinstance(msub, ast.Name):
                                m_idents.add(msub.id)
                        m_unique_calls = sorted(set(m_calls))
                        m_sorted_idents = sorted(m_idents - {"self", "cls", "True", "False", "None", mnode.name})
                        mechanics[m_sym_id] = {
                            "signature": m_sig,
                            "kind": m_kind,
                            "is_public": not mnode.name.startswith("_"),
                            "lines": [m_start, m_end],
                            "line_count": m_end - m_start + 1,
                            "docstring": m_doc.splitlines()[0] if m_doc else None,
                            "trait_bounds": [],
                            "outgoing_calls": m_unique_calls[:200],
                            "referenced_identifiers": m_sorted_idents[:200],
                            "omitted_in_l2": {
                                "omitted_categories": ["statement bodies", "local variable assignments"],
                                "omitted_call_count": max(0, len(m_unique_calls) - 200)
                            },
                            "gate_policy": {
                                "requires_l1_mounted": True,
                                "expand_to_l3_when": f"Editing method `{m_qualname}`"
                            }
                        }

    all_idents = (
        {sub.id for sub in ast.walk(tree) if isinstance(sub, ast.Name)}
        | {sub.attr for sub in ast.walk(tree) if isinstance(sub, ast.Attribute)}
        | set(IDENT_RE.findall(text))
    ) - set(defined_symbols)

    return {
        "language": "python",
        "parser_backend": "tree_sitter" if ts_enabled else "python_ast",
        "sha256": sha256_bytes(raw_bytes),
        "line_count": len(lines),
        "embedded_source_b64_zlib": encode_source_blob(text),
        "imports": imports,
        "defined_symbols": sorted(defined_symbols),
        "module_constants": module_constants,
        "referenced_identifiers": sorted(all_idents),
        "contracts": contracts,
        "epistemics": {
            "extracted_facts": extracted_facts,
            "declared_rules": list({r["statement"]: r for r in declared_rules}.values()),
            "heuristic_hazards": heuristic_hazards
        },
        "loss": {
            "omitted_at_l1": [
                f"{len(mechanics)} function/class bodies ({len(lines)} lines of Python source)"
            ],
            "truncated_contracts_count": 0,
            "mandatory_expand_triggers": [
                "Before modifying public function/class signatures or return types",
                "Before modifying symbols with DECLARED_RULE or HEURISTIC_HAZARD entries"
            ]
        },
        "l2_mechanics": mechanics
    }


def load_working_context(root: Path) -> Dict[str, Any]:
    ctx_file = root / "lod_context.json"
    if ctx_file.exists():
        try:
            loaded = json.loads(ctx_file.read_text(encoding="utf-8"))
            loaded["epistemic_type"] = "DECLARED_WORKING_CONTEXT"
            return loaded
        except Exception:
            pass

    agents_md = root / "AGENTS.md"
    if agents_md.exists():
        lines = [ln.strip() for ln in agents_md.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return {
            "epistemic_type": "DECLARED_WORKING_CONTEXT",
            "source": "AGENTS.md",
            "mission": lines[0].lstrip("# ").strip() if lines else f"Maintain {root.name}",
            "notes": lines[1:15],
            "decisions": [],
            "open_questions": []
        }

    return {
        "epistemic_type": "DEFAULT_WORKING_CONTEXT",
        "mission": f"Understand and safely modify `{root.name}` while respecting declared rules and coupling.",
        "active_objective": "Explore L0 World Map, zoom into relevant L1 Blueprints, and check blast radius before L3 edits.",
        "accepted_decisions": [],
        "hard_constraints": [
            "Do not modify L3 source without first hydrating the module's L1 Blueprint.",
            "If a module has incoming dependents, inspect coupled L1 contracts before altering public signatures."
        ],
        "open_questions": []
    }


def _init_sqlite_store(db_path: Path) -> Any:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lod_module_cache (
            mod_id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL,
            parser_backend TEXT,
            raw_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    return conn


def build_lod_container(
    repo_dir: str,
    output_file: str = ".lod.json",
    default_l0_token_budget: int = 500,
    use_tree_sitter: Optional[bool] = None,
    incremental: bool = False,
    sqlite_file: Optional[str] = None
) -> Dict[str, Any]:
    import time
    t0 = time.perf_counter()
    root = Path(repo_dir).resolve()
    out_path = Path(output_file).resolve() if Path(output_file).is_absolute() else (root / output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if sqlite_file and Path(sqlite_file).is_absolute():
        db_path = Path(sqlite_file).resolve()
    elif sqlite_file:
        db_path = root / sqlite_file
    else:
        db_path = Path(f"{out_path}.sqlite")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    raw_modules: Dict[str, Any] = {}
    cached_by_mod: Dict[str, Tuple[str, Dict[str, Any]]] = {}

    conn = _init_sqlite_store(db_path)
    if incremental:
        for mod_id, sha, raw_json in conn.execute("SELECT mod_id, sha256, raw_json FROM lod_module_cache"):
            try:
                cached_by_mod[mod_id] = (sha, json.loads(raw_json))
            except Exception:
                pass
        if not cached_by_mod and out_path.exists():
            try:
                prev_container = json.loads(out_path.read_text(encoding="utf-8"))
                for mod_id, mdata in prev_container.get("l1_blueprints", {}).items():
                    sha = mdata.get("sha256", "")
                    if sha:
                        # Reconstruct referenced_identifiers from embedded source if loading from JSON only
                        raw_copy = dict(mdata)
                        if "referenced_identifiers" not in raw_copy and "embedded_source_b64_zlib" in raw_copy:
                            src_txt = zlib.decompress(base64.b64decode(raw_copy["embedded_source_b64_zlib"])).decode("utf-8", errors="replace")
                            raw_copy["referenced_identifiers"] = sorted(
                                set(IDENT_RE.findall(src_txt)) - set(raw_copy.get("defined_symbols", []))
                            )
                        cached_by_mod[mod_id] = (sha, raw_copy)
            except Exception:
                pass

    skip_files = {
        "lod_build.py", "lod_mcp_server.py", "lod_benchmark.py",
        "lod_build_v0_2.py", "lod_mcp_server_v0_2.py"
    }

    reused_modules: List[str] = []
    reparsed_modules: List[str] = []
    cache_upserts: List[Tuple[str, str, str, str, float]] = []
    now_ts = time.time()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fname in sorted(filenames):
            if fname in skip_files or not fname.endswith((".rs", ".py", ".ts", ".tsx", ".js", ".jsx")):
                continue
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(root).as_posix()
            mod_id = f"mod://{rel}"

            if incremental and mod_id in cached_by_mod:
                live_sha = sha256_bytes(fpath.read_bytes())
                cached_sha, cached_data = cached_by_mod[mod_id]
                if live_sha == cached_sha and "referenced_identifiers" in cached_data:
                    raw_modules[mod_id] = json.loads(json.dumps(cached_data))
                    reused_modules.append(mod_id)
                    continue

            if fname.endswith(".rs"):
                parsed = parse_rust_file(fpath, rel, use_tree_sitter=use_tree_sitter)
            elif fname.endswith(".py"):
                parsed = parse_python_file(fpath, rel, use_tree_sitter=use_tree_sitter)
            else:
                parsed = parse_ts_js_file(fpath, rel, use_tree_sitter=use_tree_sitter)

            raw_modules[mod_id] = parsed
            reparsed_modules.append(mod_id)
            cache_upserts.append((
                mod_id,
                parsed["sha256"],
                parsed.get("parser_backend", "hybrid"),
                json.dumps(parsed),
                now_ts
            ))

    deleted_modules = sorted(set(cached_by_mod.keys()) - set(raw_modules.keys()))
    if cache_upserts or deleted_modules:
        with conn:
            if cache_upserts:
                conn.executemany(
                    "INSERT OR REPLACE INTO lod_module_cache (mod_id, sha256, parser_backend, raw_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                    cache_upserts
                )
            for del_mod in deleted_modules:
                conn.execute("DELETE FROM lod_module_cache WHERE mod_id = ?", (del_mod,))
    conn.close()

    # Build Cross-Language Symbol-Reference Graph (Rust <-> Python <-> TypeScript/JS)
    symbol_to_defining_mods: Dict[str, Set[str]] = {}
    module_stems: Dict[str, str] = {}
    for mod_id, data in raw_modules.items():
        stem = Path(mod_id.replace("mod://", "")).stem
        module_stems[mod_id] = stem
        for sym in data.get("defined_symbols", []):
            if len(sym) > 2:
                symbol_to_defining_mods.setdefault(sym, set()).add(mod_id)

    for mod_id, data in raw_modules.items():
        outgoing_edges: Dict[str, List[str]] = {}
        imports_blob = " ".join(data.get("imports", []))
        ref_idents = set(data.get("referenced_identifiers", []))

        for target_id, target_stem in module_stems.items():
            if target_id != mod_id and target_stem not in ("mod", "lib", "main", "index", "__init__"):
                if re.search(rf"\b{re.escape(target_stem)}\b", imports_blob):
                    outgoing_edges.setdefault(target_id, []).append(f"imports module `{target_stem}`")

        for ident in ref_idents:
            for target_id in symbol_to_defining_mods.get(ident, set()):
                if target_id != mod_id:
                    outgoing_edges.setdefault(target_id, []).append(f"references symbol `{ident}`")

        data["coupled_with"] = [
            {"target": tid, "reasons": sorted(set(reasons))[:5], "epistemic_type": "EXTRACTED_FACT"}
            for tid, reasons in sorted(outgoing_edges.items())
        ]
        data.pop("referenced_identifiers", None)

    incoming_map: Dict[str, List[str]] = {m: [] for m in raw_modules}
    for mod_id, data in raw_modules.items():
        for edge in data["coupled_with"]:
            incoming_map[edge["target"]].append(mod_id)

    ranked_modules: List[Tuple[float, str]] = []
    all_declared_rules: List[Dict[str, Any]] = []
    all_heuristic_hazards: List[Dict[str, Any]] = []

    for mod_id, data in raw_modules.items():
        in_deg = len(incoming_map[mod_id])
        out_deg = len(data["coupled_with"])
        rules = data["epistemics"]["declared_rules"]
        hazards = data["epistemics"]["heuristic_hazards"]
        data["depended_on_by"] = sorted(incoming_map[mod_id])

        score = (in_deg * 3.0) + (out_deg * 1.5) + (len(rules) * 2.0) + (len(hazards) * 1.0)
        data["centrality_score"] = round(score, 2)
        ranked_modules.append((score, mod_id))

        all_declared_rules.extend(rules)
        all_heuristic_hazards.extend(hazards)

    ranked_modules.sort(key=lambda item: (-item[0], item[1]))

    working_context = load_working_context(root)
    budgeted_l0_modules: Dict[str, Any] = {}
    omitted_l0_modules: List[str] = []

    header_rules = [r["statement"] for r in all_declared_rules[:10]]
    omitted_rules_count = max(0, len(all_declared_rules) - len(header_rules))
    running_tokens = estimate_tokens(working_context) + estimate_tokens(header_rules) + 80

    for score, mod_id in ranked_modules:
        mdata = raw_modules[mod_id]
        entry = {
            "language": mdata["language"],
            "centrality_score": score,
            "contracts_count": len(mdata["contracts"]),
            "depends_on_count": len(mdata["coupled_with"]),
            "depended_on_by_count": len(mdata["depended_on_by"]),
            "declared_rules_count": len(mdata["epistemics"]["declared_rules"]),
            "heuristic_hazards_count": len(mdata["epistemics"]["heuristic_hazards"])
        }
        entry_cost = estimate_tokens({mod_id: entry})
        if running_tokens + entry_cost <= default_l0_token_budget or len(budgeted_l0_modules) == 0:
            budgeted_l0_modules[mod_id] = entry
            running_tokens += entry_cost
        else:
            omitted_l0_modules.append(mod_id)

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    container = {
        "lod_version": "0.4",
        "project": root.name,
        "repo_root": "." if out_path.parent == root else str(root),
        "git_commit": get_git_commit(root),
        "portability": {
            "self_contained": True,
            "source_encoding": "zlib+base64",
            "hash_algorithm": "sha256",
            "ast_backend": "tree_sitter" if ((use_tree_sitter if use_tree_sitter is not None else HAS_TREE_SITTER) and HAS_TREE_SITTER) else "stateful_lexer",
            "sqlite_backing_store": db_path.name,
            "supported_languages": ["rust", "python", "typescript", "javascript"]
        },
        "incremental_stats": {
            "incremental_mode": incremental,
            "elapsed_ms": elapsed_ms,
            "reused_modules_count": len(reused_modules),
            "reparsed_modules_count": len(reparsed_modules),
            "reparsed_modules": reparsed_modules,
            "deleted_modules": deleted_modules
        },
        "working_context": working_context,
        "l0_world_map": {
            "token_budget_target": default_l0_token_budget,
            "estimated_l0_tokens": running_tokens,
            "total_modules_in_repo": len(raw_modules),
            "declared_project_rules": header_rules,
            "top_modules_by_centrality": budgeted_l0_modules,
            "l0_loss": {
                "lossy": True,
                "omitted_modules_count": len(omitted_l0_modules),
                "omitted_modules": omitted_l0_modules,
                "omitted_declared_rules_count": omitted_rules_count,
                "omitted_heuristic_hazards_count": len(all_heuristic_hazards),
                "expand_instruction": (
                    "Call lod_map(token_budget=2500) to reveal omitted modules, "
                    "or call lod_zoom(mod_id, 'L1') to inspect module contracts and epistemic rules."
                )
            }
        },
        "l1_blueprints": raw_modules
    }

    out_path.write_text(json.dumps(container, indent=2), encoding="utf-8")

    # Automatically invalidate any in-process MCP session hydration receipts whose SHA-256 digest changed
    try:
        from semantic_lod import mcp_server as _mcp_srv
        if hasattr(_mcp_srv, "invalidate_stale_session_receipts"):
            _mcp_srv.invalidate_stale_session_receipts(container)
    except Exception:
        pass

    mode_str = f"incremental ({len(reparsed_modules)} reparsed, {len(reused_modules)} cached in {elapsed_ms}ms)" if incremental else f"full ({elapsed_ms}ms)"
    print(f"[LOD v0.4] Compiled {len(raw_modules)} polyglot modules [{mode_str}] into {out_path} ({out_path.stat().st_size} bytes)")
    return container


def main_cli() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    target_repo = args[0] if len(args) > 0 else "."
    budget = int(args[1]) if len(args) > 1 else 500
    incremental = "--incremental" in flags
    use_ts = False if "--no-tree-sitter" in flags else None
    build_lod_container(
        target_repo,
        default_l0_token_budget=budget,
        use_tree_sitter=use_ts,
        incremental=incremental
    )


if __name__ == "__main__":
    main_cli()
