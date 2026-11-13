from __future__ import annotations

import ast
from dataclasses import dataclass, field
from hashlib import sha256


@dataclass(frozen=True)
class ParameterFact:
    name: str
    kind: str
    default: str | None


@dataclass(frozen=True)
class SourceFile:
    """A source value bound to its exact UTF-8 content digest."""

    path: str
    sha256: str
    text: str
    byte_count: int

    @classmethod
    def from_text(cls, path: str, text: str) -> "SourceFile":
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        data = text.encode("utf-8")
        return cls(normalized, sha256(data).hexdigest(), text, len(data))


@dataclass(frozen=True)
class Finding:
    detector: str
    symbol: str
    path: str
    start_line: int
    end_line: int
    confidence: str
    evidence: tuple[str, ...]
    resolution_source: str = "static_ast"


@dataclass(frozen=True)
class ObservedCallEdge:
    caller_path: str
    caller_qualname: str
    instruction_offset: int
    callee_qualname: str
    callee_name: str
    receiver_type: str | None
    pid: int
    edge_label: str = "observed_runtime"
    caller_line: int | None = None


@dataclass(frozen=True)
class CallFact:
    path: str
    line: int
    owner_function: str | None
    callee_name: str
    callee_chain: tuple[str, ...]
    receiver_name: str | None
    constructed_receiver: str | None
    positional_count: int
    keyword_names: tuple[str, ...]
    value_names: tuple[str, ...]
    callback_wrapped_keywords: tuple[tuple[str, tuple[str, ...]], ...] = ()
    has_varargs: bool = False
    has_kwargs: bool = False
    receiver_chain: tuple[str, ...] = ()
    unpack_target_arity: int | None = None
    result_dereferenced: bool = False
    enclosing_caught_exceptions: tuple[str, ...] = ()
    _ast_node: ast.Call | None = field(default=None, repr=False, compare=False)
    _result_consumer: ast.AST | None = field(default=None, repr=False, compare=False)
    under_broad_except: bool = False


@dataclass
class FunctionFact:
    path: str
    module: str
    qualname: str
    name: str
    class_qualname: str | None
    line: int
    end_line: int
    parameters: tuple[ParameterFact, ...]
    _body_dump_cache: str | None
    field_reads: frozenset[str]
    field_writes: frozenset[str]
    local_name_reads: frozenset[str] = frozenset()
    bare_truthy_checks: tuple[tuple[str, int], ...] = ()
    param_annotations: tuple[tuple[str, str], ...] = ()
    self_attr_annotations: tuple[tuple[str, str], ...] = ()
    return_annotation: str | None = None
    source_slice: str = ""
    own_calls: tuple[CallFact, ...] = ()
    has_nested_defs: bool = False
    is_property: bool = False
    return_tuple_arities: frozenset[int] = frozenset()
    has_non_none_return: bool = False
    has_explicit_none_return: bool = False
    return_allows_none: bool = False
    raised_exceptions: frozenset[str] = frozenset()
    _ast_node: ast.AST | None = field(default=None, repr=False, compare=False)
    guarded_raises: tuple[tuple[str, tuple[str, ...]], ...] = ()
    local_name_stores: frozenset[str] = frozenset()
    non_self_store_reads: frozenset[str] = frozenset()
    attr_loads: frozenset[str] = frozenset()

    @property
    def decorator_names(self) -> tuple[str, ...]:
        """Leaf names of decorators applied to this function."""
        node = self._ast_node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ()
        names: list[str] = []
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name:
                names.append(name)
        return tuple(names)

    @property
    def is_stub(self) -> bool:
        """An abstract method, overload, or declaration whose body does no work."""
        node = self._ast_node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        if any(name in {"abstractmethod", "overload"} for name in self.decorator_names):
            return True
        for statement in node.body:
            if isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                continue
            if (
                isinstance(statement, ast.Return)
                and isinstance(statement.value, ast.Name)
                and statement.value.id == "NotImplemented"
            ):
                continue
            if isinstance(statement, ast.Raise) and statement.exc is not None:
                raised = statement.exc.func if isinstance(statement.exc, ast.Call) else statement.exc
                if getattr(raised, "id", "") == "NotImplementedError":
                    continue
            return False
        return True

    @property
    def is_async(self) -> bool:
        return isinstance(self._ast_node, ast.AsyncFunctionDef)

    @property
    def is_generator(self) -> bool:
        """The function's own body yields. A yield in a nested function does not count."""
        node = self._ast_node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        pending = list(node.body)
        while pending:
            item = pending.pop()
            if isinstance(item, (ast.Yield, ast.YieldFrom)):
                return True
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                pending.extend(ast.iter_child_nodes(item))
        return False

    @property
    def body_dump(self) -> str:
        if self._body_dump_cache is None:
            if self._ast_node is not None:
                self._body_dump_cache = ast.dump(self._ast_node, include_attributes=False)
            else:
                self._body_dump_cache = self.source_slice
        return self._body_dump_cache


@dataclass
class ClassFact:
    path: str
    module: str
    qualname: str
    name: str
    bases: tuple[str, ...]
    line: int
    methods: dict[str, FunctionFact]
    attr_annotations: dict[str, str] = field(default_factory=dict)
    # Names the class body or a method's `self` assigns from an expression that could be a
    # callable, such as `finish = _finish` or `self.finish = self._safe`.
    assigned_names: frozenset[str] = frozenset()


@dataclass
class FileFacts:
    path: str
    module: str
    functions: dict[str, FunctionFact]
    classes: dict[str, ClassFact]
    calls: list[CallFact]
    imports: dict[str, tuple[str, str | None]]
    parse_error: str | None = None
    # Names assigned at module scope from an expression that could be a callable.
    module_name_stores: frozenset[str] = frozenset()
    # For a module name bound exactly once to a small literal: the default text of a
    # reference to it, mapped to the default text of its value.
    constant_defaults: dict[str, str] = field(default_factory=dict)
    attr_loads: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CustodyReport:
    clean: bool
    checked_files: int
    modified_externally: tuple[str, ...]
    missing_files: tuple[str, ...]
    unexpected_files: tuple[str, ...]
    cpu_seconds: float
    wall_seconds: float


@dataclass(frozen=True)
class WriteDeltaResult:
    path: str
    before_sha256: str | None
    after_sha256: str
    content_changed: bool
    cross_file_triggered: bool
    parses_executed: int
    candidate_calls_inspected: int
    findings: tuple[Finding, ...]
    ranked_findings: tuple[Finding, ...]
    advisory_text: str
    advisory_utf8_bytes: int
    cpu_seconds: float
    wall_seconds: float
    discharged_findings: tuple[Finding, ...] = ()
    open_obligations: int = 0
