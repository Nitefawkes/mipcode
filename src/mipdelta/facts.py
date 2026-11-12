from __future__ import annotations

import ast
import operator
from dataclasses import replace
from pathlib import PurePosixPath

from .model import CallFact, ClassFact, FileFacts, FunctionFact, ParameterFact


def module_name(path: str) -> str:
    clean = path.replace("\\", "/").lstrip("/")
    p = PurePosixPath(clean)
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def dotted(node: ast.AST | None) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return (prefix + (node.attr,)) if prefix else ()
    return ()


def _annotation_head(node: ast.expr | None) -> str | None:
    """Extract primary named type from simple annotations (e.g. Reporter, Optional[Reporter], Reporter | None)."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return None if node.id in {"None", "Any", "object"} else node.id
    if isinstance(node, ast.Attribute):
        chain = dotted(node)
        return ".".join(chain) if chain else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value, mode="eval")
            return _annotation_head(parsed.body)
        except SyntaxError:
            return None
    if isinstance(node, ast.Subscript):
        head = dotted(node.value)
        if head and head[-1] in {"Optional", "ClassVar", "Final"}:
            return _annotation_head(node.slice)
        if head and head[-1] == "Union" and isinstance(node.slice, ast.Tuple):
            candidates = [t for elt in node.slice.elts if (t := _annotation_head(elt))]
            return candidates[0] if len(candidates) == 1 else None
        return ".".join(head) if head else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _annotation_head(node.left)
        right = _annotation_head(node.right)
        if left and not right:
            return left
        if right and not left:
            return right
        return None
    return None


def _return_allows_none(node: ast.expr | None) -> bool:
    """Return True if a return annotation explicitly permits None (Optional[T], T | None, None, Any)."""
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        if node.value is None:
            return True
        if isinstance(node.value, str):
            try:
                parsed = ast.parse(node.value, mode="eval")
                return _return_allows_none(parsed.body)
            except SyntaxError:
                return "None" in node.value or "Optional" in node.value
    if isinstance(node, ast.Name):
        return node.id in {"None", "Any", "object"}
    if isinstance(node, ast.Subscript):
        head = dotted(node.value)
        if head and head[-1] == "Optional":
            return True
        if head and head[-1] == "Union":
            if isinstance(node.slice, ast.Tuple):
                return any(_return_allows_none(e) for e in node.slice.elts)
            return _return_allows_none(node.slice)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _return_allows_none(node.left) or _return_allows_none(node.right)
    return False


def _extract_narrow_caught_exceptions(handlers: list[ast.ExceptHandler]) -> tuple[str, ...] | None:
    """Return sorted caught exception leaf names if all handlers are narrow, or None if broad (Exception/BaseException/bare)."""
    if not handlers:
        return ()
    caught: set[str] = set()
    for handler in handlers:
        if handler.type is None:
            return None
        elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for elt in elts:
            chain = dotted(elt)
            if not chain:
                return None
            leaf = chain[-1]
            if leaf in {"Exception", "BaseException"}:
                return None
            caught.add(leaf)
    return tuple(sorted(caught))


_FOLDABLE = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.LShift: operator.lshift,
    ast.Pow: operator.pow,
}


def _fold_number(node: ast.expr) -> int | float | None:
    """Value of a small arithmetic expression over number literals, such as `2**16`."""
    if isinstance(node, ast.Constant):
        return node.value if type(node.value) in (int, float) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _fold_number(node.operand)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _FOLDABLE:
        left, right = _fold_number(node.left), _fold_number(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, (ast.Pow, ast.LShift)) and not (abs(left) <= 2**32 and abs(right) <= 64):
            # Keep the result small enough to compute while parsing.
            return None
        try:
            return _FOLDABLE[type(node.op)](left, right)
        except (ArithmeticError, TypeError, ValueError):
            return None
    return None


def _default_text(node: ast.expr | None) -> str | None:
    """Text that is equal for defaults of equal value, where that is cheap to know."""
    if node is None:
        return None
    if not isinstance(node, ast.Constant):
        value = _fold_number(node)
        if value is not None:
            node = ast.Constant(value=value, kind=None)
    return ast.dump(node, include_attributes=False)


_OWN_SCOPE = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)


def _module_bindings(tree: ast.Module) -> tuple[frozenset[str], dict[str, str]]:
    """Names assigned at module scope, and the values of those assigned exactly once."""
    stores: dict[str, int] = {}
    rebound_elsewhere: set[str] = set()
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, _OWN_SCOPE):
            rebound_elsewhere.update(
                name for item in ast.walk(node) if isinstance(item, ast.Global) for name in item.names
            )
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            stores[node.id] = stores.get(node.id, 0) + 1
        pending.extend(ast.iter_child_nodes(node))

    constants: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if isinstance(target, ast.Name) and stores.get(target.id) == 1 and target.id not in rebound_elsewhere:
            reference = _default_text(ast.Name(id=target.id, ctx=ast.Load()))
            constants[reference] = _default_text(value)
    return frozenset(stores), constants


def _parameters(args: ast.arguments) -> tuple[tuple[ParameterFact, ...], tuple[tuple[str, str], ...]]:
    rows: list[ParameterFact] = []
    annotations: list[tuple[str, str]] = []
    pos = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(pos) - len(args.defaults)) + list(args.defaults)
    posonly_count = len(args.posonlyargs)
    for index, (arg, default) in enumerate(zip(pos, defaults)):
        kind = "posonly" if index < posonly_count else "positional"
        rows.append(ParameterFact(arg.arg, kind, _default_text(default)))
        ann = _annotation_head(arg.annotation)
        if ann:
            annotations.append((arg.arg, ann))
    if args.vararg:
        rows.append(ParameterFact(args.vararg.arg, "vararg", None))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        rows.append(ParameterFact(arg.arg, "kwonly", _default_text(default)))
        ann = _annotation_head(arg.annotation)
        if ann:
            annotations.append((arg.arg, ann))
    if args.kwarg:
        rows.append(ParameterFact(args.kwarg.arg, "kwarg", None))
    return tuple(rows), tuple(annotations)


def _value_names(node: ast.AST, *, in_lambda: bool = False) -> tuple[str, ...]:
    found: list[str] = []

    def visit(cur: ast.AST, deferred: bool) -> None:
        if isinstance(cur, ast.Lambda):
            visit(cur.body, True)
            return
        if isinstance(cur, ast.Call):
            fn_chain = dotted(cur.func)
            is_partial = bool(fn_chain and fn_chain[-1] == "partial")
            if deferred or is_partial:
                visit(cur.func, deferred)
            for idx, arg in enumerate(cur.args):
                visit(arg, True if (is_partial and idx == 0) else deferred)
            for kw in cur.keywords:
                visit(kw.value, deferred)
            return
        if isinstance(cur, ast.Name):
            if cur.id not in found:
                found.append(cur.id)
            return
        if isinstance(cur, ast.Attribute):
            chain = dotted(cur)
            if chain:
                value = ".".join(chain)
                if value not in found:
                    found.append(value)
                return
        for child in ast.iter_child_nodes(cur):
            visit(child, deferred)

    visit(node, in_lambda)
    return tuple(found)


def _adapter_keywords(value: ast.expr) -> list[tuple[str, tuple[str, ...]]]:
    """Keywords a lambda or functools.partial supplies to the callable it wraps."""
    rows: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(value, ast.Lambda):
        for nested in ast.walk(value.body):
            if isinstance(nested, ast.Call):
                nested_chain = dotted(nested.func)
                if nested_chain:
                    rows.append((nested_chain[-1], tuple(arg.arg for arg in nested.keywords if arg.arg)))
    elif isinstance(value, ast.Call):
        fn_chain = dotted(value.func)
        if fn_chain and fn_chain[-1] == "partial" and value.args:
            target_chain = dotted(value.args[0])
            if target_chain:
                rows.append((target_chain[-1], tuple(arg.arg for arg in value.keywords if arg.arg)))
    return rows


def _call_fact(
    node: ast.Call,
    path: str,
    owner: str | None,
    *,
    unpack_target_arity: int | None = None,
    result_dereferenced: bool = False,
    enclosing_caught_exceptions: tuple[str, ...] = (),
    result_consumer: ast.AST | None = None,
    under_broad_except: bool = False,
) -> CallFact | None:
    chain = dotted(node.func)
    constructed = None
    receiver_name = None
    receiver_chain: tuple[str, ...] = ()
    callee_name = ""

    if isinstance(node.func, ast.Attribute):
        callee_name = node.func.attr
        receiver = node.func.value
        if isinstance(receiver, ast.Name):
            receiver_name = receiver.id
            receiver_chain = (receiver.id,)
        elif isinstance(receiver, ast.Attribute):
            receiver_chain = dotted(receiver)
            if receiver_chain:
                receiver_name = receiver_chain[0]
        elif isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name):
            constructed = receiver.func.id
            if not chain:
                chain = (constructed, callee_name)
    elif chain:
        callee_name = chain[-1]

    if not chain and not (constructed and callee_name):
        return None
    if not callee_name:
        callee_name = chain[-1]

    vals: list[str] = []
    has_varargs = False
    has_kwargs = False
    callback_wrapped_keywords: list[tuple[str, tuple[str, ...]]] = []
    for arg in node.args:
        if isinstance(arg, ast.Starred):
            has_varargs = True
            vals.extend(_value_names(arg.value))
        else:
            vals.extend(_value_names(arg))
            callback_wrapped_keywords.extend(_adapter_keywords(arg))
    for kw in node.keywords:
        if kw.arg is None:
            has_kwargs = True
        vals.extend(_value_names(kw.value))
        callback_wrapped_keywords.extend(_adapter_keywords(kw.value))
    pos_count = sum(1 for arg in node.args if not isinstance(arg, ast.Starred))
    return CallFact(
        path,
        node.lineno,
        owner,
        callee_name,
        chain,
        receiver_name,
        constructed,
        pos_count,
        tuple(kw.arg for kw in node.keywords if kw.arg),
        tuple(dict.fromkeys(vals)),
        tuple(callback_wrapped_keywords),
        has_varargs,
        has_kwargs,
        receiver_chain,
        unpack_target_arity,
        result_dereferenced,
        enclosing_caught_exceptions,
        node,
        result_consumer,
        under_broad_except,
    )


class _CallContextScanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self._result_consumers: dict[int, ast.AST] = {}

    def visit_Await(self, node: ast.Await) -> None:
        if isinstance(node.value, ast.Call):
            self._result_consumers[id(node.value)] = node
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        if isinstance(node.iter, ast.Call):
            self._result_consumers[id(node.iter)] = node
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        if node.is_async and isinstance(node.iter, ast.Call):
            self._result_consumers[id(node.iter)] = node
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for arg in node.args:
            if isinstance(arg, ast.Call):
                self._result_consumers[id(arg)] = node
        for keyword in node.keywords:
            if isinstance(keyword.value, ast.Call):
                self._result_consumers[id(keyword.value)] = node
        self.generic_visit(node)


class _FunctionScanner(_CallContextScanner):
    """Single-pass intra-function fact collector (eliminating secondary ast.parse passes)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self.calls: list[ast.Call] = []
        self.local_name_reads: set[str] = set()
        self.local_name_stores: set[str] = set()
        self.bare_truthy_checks: list[tuple[str, int]] = []
        self.self_attr_annotations: list[tuple[str, str]] = []
        self.nested_declarations: list[ast.AST] = []
        self.return_tuple_arities: set[int] = set()
        self.has_non_tuple_return: bool = False
        self.has_non_none_return: bool = False
        self.has_explicit_none_return: bool = False
        self.raised_exceptions: set[str] = set()
        self.guarded_raises: list[tuple[str, tuple[str, ...]]] = []
        self._guard_names: list[frozenset[str]] = []
        self.call_unpack_arity: dict[int, int] = {}
        self.assigned_call_vars: dict[str, int] = {}
        self.dereferenced_calls: set[int] = set()
        self.dereferenced_vars: set[str] = set()
        self.guarded_vars: set[str] = set()
        self.call_caught_exceptions: dict[int, tuple[str, ...]] = {}
        self.calls_under_broad_except: set[int] = set()
        self._try_caught_stack: list[tuple[str, ...]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.nested_declarations.append(node)
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                self.local_name_reads.add(child.id)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.nested_declarations.append(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.local_name_reads.add(node.id)
        else:
            self.local_name_stores.add(node.id)

    def visit_If(self, node: ast.If) -> None:
        if isinstance(node.test, ast.Name):
            self.bare_truthy_checks.append((node.test.id, node.test.lineno))
        tested = frozenset(child.id for child in ast.walk(node.test) if isinstance(child, ast.Name))
        self.guarded_vars.update(tested)
        self._guard_names.append(tested)
        self.generic_visit(node)
        self._guard_names.pop()

    def visit_IfExp(self, node: ast.IfExp) -> None:
        for child in ast.walk(node.test):
            if isinstance(child, ast.Name):
                self.guarded_vars.add(child.id)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, (ast.Tuple, ast.List)) and not any(isinstance(e, ast.Starred) for e in target.elts):
                self.call_unpack_arity[id(node.value)] = len(target.elts)
            elif isinstance(target, ast.Name):
                self.assigned_call_vars[target.id] = id(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if (
            isinstance(node.target, ast.Attribute)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == "self"
        ):
            ann = _annotation_head(node.annotation)
            if ann:
                self.self_attr_annotations.append((node.target.attr, ann))
        elif isinstance(node.target, ast.Name) and isinstance(node.value, ast.Call):
            self.assigned_call_vars[node.target.id] = id(node.value)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.writes.add(node.attr)
            else:
                self.reads.add(node.attr)
        elif isinstance(node.value, ast.Call):
            self.dereferenced_calls.add(id(node.value))
        elif isinstance(node.value, ast.Name) and isinstance(node.ctx, ast.Load):
            self.dereferenced_vars.add(node.value.id)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        target = node.value
        if (
            isinstance(node.ctx, (ast.Store, ast.Del))
            and isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            self.writes.add(target.attr)
        elif isinstance(target, ast.Call) and isinstance(node.ctx, ast.Load):
            self.dereferenced_calls.add(id(target))
        elif isinstance(target, ast.Name) and isinstance(node.ctx, ast.Load):
            self.dereferenced_vars.add(target.id)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is None or (isinstance(node.value, ast.Constant) and node.value.value is None):
            self.has_explicit_none_return = True
        else:
            self.has_non_none_return = True
            if isinstance(node.value, ast.Tuple) and not any(isinstance(e, ast.Starred) for e in node.value.elts):
                self.return_tuple_arities.add(len(node.value.elts))
            else:
                self.has_non_tuple_return = True
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        name = None
        if node.exc is not None:
            if isinstance(node.exc, ast.Call):
                chain = dotted(node.exc.func)
                # `raise self.make_error(...)` raises whatever the factory returns.
                if chain and chain[-1][:1].isupper():
                    name = chain[-1]
            elif isinstance(node.exc, (ast.Name, ast.Attribute)):
                chain = dotted(node.exc)
                if chain and chain[-1][:1].isupper():
                    name = chain[-1]
        if name is not None:
            self.raised_exceptions.add(name)
            # Names tested by the enclosing if statements that lead to this raise.
            guards = sorted(frozenset().union(*self._guard_names)) if self._guard_names else []
            self.guarded_raises.append((name, tuple(guards)))
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        caught = _extract_narrow_caught_exceptions(node.handlers)
        push_frame = caught is None or bool(caught)
        if push_frame:
            self._try_caught_stack.append(caught)
        for stmt in node.body:
            self.visit(stmt)
        if push_frame:
            self._try_caught_stack.pop()
        for handler in node.handlers:
            self.visit(handler)
        for stmt in node.orelse:
            self.visit(stmt)
        for stmt in node.finalbody:
            self.visit(stmt)

    visit_TryStar = visit_Try

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
            and node.func.attr in {"append", "extend", "add", "update", "insert", "remove", "discard", "clear"}
        ):
            self.writes.add(node.func.value.attr)
        if None in self._try_caught_stack:
            self.calls_under_broad_except.add(id(node))
        elif self._try_caught_stack:
            merged: set[str] = set()
            for c in self._try_caught_stack:
                if c:
                    merged.update(c)
            if merged:
                self.call_caught_exceptions[id(node)] = tuple(sorted(merged))
        self.calls.append(node)
        super().visit_Call(node)


def _shift_call(call: CallFact, line_delta: int) -> CallFact:
    return replace(call, line=call.line + line_delta)


class _ModuleScanner(_CallContextScanner):
    def __init__(
        self,
        path: str,
        source_lines: list[str] | None = None,
        prior_facts: FileFacts | None = None,
    ) -> None:
        super().__init__()
        self.path = path.replace("\\", "/")
        self.module = module_name(path)
        self.source_lines = source_lines or []
        self.prior_facts = (
            prior_facts
            if (prior_facts is not None and prior_facts.parse_error is None and prior_facts.module == self.module)
            else None
        )
        self.functions: dict[str, FunctionFact] = {}
        self.classes: dict[str, ClassFact] = {}
        self.calls: list[CallFact] = []
        self.imports: dict[str, tuple[str, str | None]] = {}
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            local = item.asname or item.name.split(".")[0]
            self.imports[local] = (item.name, None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = "." * node.level
        if node.module:
            base += node.module
        for item in node.names:
            self.imports[item.asname or item.name] = (base, item.name)

    def visit_Call(self, node: ast.Call) -> None:
        row = _call_fact(
            node,
            self.path,
            ".".join(self.function_stack) or None,
            result_consumer=self._result_consumers.get(id(node)),
        )
        if row:
            self.calls.append(row)
        super().visit_Call(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = ".".join([*self.class_stack, node.name])
        bases = tuple(".".join(dotted(base)) for base in node.bases if dotted(base))
        attr_annotations: dict[str, str] = {}
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                ann = _annotation_head(stmt.annotation)
                if ann:
                    attr_annotations[stmt.target.id] = ann
        assigned_names = frozenset(
            target.id
            for stmt in node.body
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None
            for target in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
            if isinstance(target, ast.Name)
        )
        row = ClassFact(
            self.path, self.module, qual, node.name, bases, node.lineno, {}, attr_annotations, assigned_names
        )
        self.classes[qual] = row
        self.class_stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self.class_stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        prefix = [*self.class_stack, *self.function_stack]
        qual = ".".join([*prefix, node.name])
        class_qual = ".".join(self.class_stack) if self.class_stack else None
        end_line = getattr(node, "end_lineno", node.lineno)
        start_idx = max(0, (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1)
        src_slice = "\n".join(self.source_lines[start_idx:end_line]) if self.source_lines else ""

        prior_fn = self.prior_facts.functions.get(qual) if (self.prior_facts is not None and src_slice) else None
        if (
            prior_fn is not None
            and not prior_fn.has_nested_defs
            and prior_fn.class_qualname == class_qual
            and prior_fn.source_slice == src_slice
        ):
            line_delta = node.lineno - prior_fn.line
            if line_delta == 0:
                shifted_truthy = prior_fn.bare_truthy_checks
                shifted_calls = prior_fn.own_calls
            else:
                shifted_truthy = tuple((nm, ln + line_delta) for nm, ln in prior_fn.bare_truthy_checks)
                shifted_calls = tuple(_shift_call(c, line_delta) for c in prior_fn.own_calls)
            fact = FunctionFact(
                self.path,
                self.module,
                qual,
                node.name,
                class_qual,
                node.lineno,
                end_line,
                prior_fn.parameters,
                prior_fn._body_dump_cache,
                prior_fn.field_reads,
                prior_fn.field_writes,
                prior_fn.local_name_reads,
                shifted_truthy,
                prior_fn.param_annotations,
                prior_fn.self_attr_annotations,
                prior_fn.return_annotation,
                src_slice,
                shifted_calls,
                False,
                prior_fn.is_property,
                prior_fn.return_tuple_arities,
                prior_fn.has_non_none_return,
                prior_fn.has_explicit_none_return,
                prior_fn.return_allows_none,
                prior_fn.raised_exceptions,
                node,
                guarded_raises=prior_fn.guarded_raises,
                local_name_stores=prior_fn.local_name_stores,
            )
            self.functions[qual] = fact
            if class_qual and class_qual in self.classes:
                cls = self.classes[class_qual]
                cls.methods[node.name] = fact
                for attr, ann in fact.self_attr_annotations:
                    cls.attr_annotations.setdefault(attr, ann)
                if fact.is_property and fact.return_annotation:
                    cls.attr_annotations.setdefault(node.name, fact.return_annotation)
            self.calls.extend(shifted_calls)
            return

        local = _FunctionScanner()
        for child in node.body:
            local.visit(child)
        for var_name, call_id in local.assigned_call_vars.items():
            if var_name in local.dereferenced_vars and var_name not in local.guarded_vars:
                local.dereferenced_calls.add(call_id)
        params, param_anns = _parameters(node.args)
        ret_ann = _annotation_head(node.returns)
        ret_allows_none = _return_allows_none(node.returns)
        is_prop = "property" in {dotted(d)[-1] for d in node.decorator_list if dotted(d)}
        own_calls_list: list[CallFact] = []
        for call_node in local.calls:
            cid = id(call_node)
            row = _call_fact(
                call_node,
                self.path,
                qual,
                unpack_target_arity=local.call_unpack_arity.get(cid),
                result_dereferenced=(cid in local.dereferenced_calls),
                enclosing_caught_exceptions=local.call_caught_exceptions.get(cid, ()),
                result_consumer=local._result_consumers.get(cid),
                under_broad_except=(cid in local.calls_under_broad_except),
            )
            if row:
                own_calls_list.append(row)
        own_calls = tuple(own_calls_list)
        has_nested = bool(local.nested_declarations)
        tuple_arities = frozenset() if local.has_non_tuple_return else frozenset(local.return_tuple_arities)
        fact = FunctionFact(
            self.path,
            self.module,
            qual,
            node.name,
            class_qual,
            node.lineno,
            end_line,
            params,
            None,
            frozenset(local.reads),
            frozenset(local.writes),
            frozenset(local.local_name_reads),
            tuple(local.bare_truthy_checks),
            param_anns,
            tuple(local.self_attr_annotations),
            ret_ann,
            src_slice,
            own_calls,
            has_nested,
            is_prop,
            tuple_arities,
            local.has_non_none_return,
            local.has_explicit_none_return,
            ret_allows_none,
            frozenset(local.raised_exceptions),
            node,
            guarded_raises=tuple(local.guarded_raises),
            local_name_stores=frozenset(local.local_name_stores),
        )
        self.functions[qual] = fact
        if class_qual and class_qual in self.classes:
            cls = self.classes[class_qual]
            cls.methods[node.name] = fact
            for attr, ann in local.self_attr_annotations:
                cls.attr_annotations.setdefault(attr, ann)
            if is_prop and ret_ann:
                cls.attr_annotations.setdefault(node.name, ret_ann)
        self.function_stack.append(node.name)
        for nested in local.nested_declarations:
            self.visit(nested)
        self.function_stack.pop()

        self.calls.extend(own_calls)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def parse_file(path: str, text: str, *, prior_facts: FileFacts | None = None) -> FileFacts:
    normalized = path.replace("\\", "/")
    try:
        tree = ast.parse(text, filename=normalized)
    except SyntaxError as exc:
        return FileFacts(normalized, module_name(normalized), {}, {}, [], {}, f"{exc.msg} at line {exc.lineno}")
    scanner = _ModuleScanner(normalized, text.splitlines(), prior_facts=prior_facts)
    scanner.visit(tree)
    module_name_stores, constant_defaults = _module_bindings(tree)
    return FileFacts(
        normalized,
        scanner.module,
        scanner.functions,
        scanner.classes,
        scanner.calls,
        scanner.imports,
        module_name_stores=module_name_stores,
        constant_defaults=constant_defaults,
    )
