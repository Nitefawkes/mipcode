from __future__ import annotations

import ast
from inspect import Parameter, Signature

from .facts import dotted
from .model import CallFact, FileFacts, FunctionFact

_KINDS = {
    "posonly": Parameter.POSITIONAL_ONLY,
    "positional": Parameter.POSITIONAL_OR_KEYWORD,
    "vararg": Parameter.VAR_POSITIONAL,
    "kwonly": Parameter.KEYWORD_ONLY,
    "kwarg": Parameter.VAR_KEYWORD,
}
_ASYNCIO_CONSUMERS = {
    "asyncio.create_task",
    "asyncio.ensure_future",
    "asyncio.gather",
    "asyncio.run",
    "asyncio.run_coroutine_threadsafe",
    "asyncio.shield",
    "asyncio.wait_for",
}


def call_head_is_parameter(call: CallFact, caller: FileFacts) -> bool:
    owner = caller.functions.get(call.owner_function or "")
    if owner is None or not call.callee_chain:
        return False
    head = call.callee_chain[0]
    if call.receiver_chain == ("self",) and head not in caller.imports:
        return False
    return any(parameter.name == head for parameter in owner.parameters)


def _receiver_count(
    target: FunctionFact, call: CallFact, caller: FileFacts, provenance: str
) -> int | None:
    """Count a known implicit receiver. Custom decorators can replace the signature."""
    node = target._ast_node
    decorators = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        decorators = {dotted(item) for item in node.decorator_list}
    if not decorators <= {("staticmethod",), ("classmethod",)}:
        return None
    if target.class_qualname is None:
        return 0

    if call.constructed_receiver or call.receiver_chain == ("self",) or provenance in {
        "static_type_candidate", "observed_runtime"
    }:
        return int(("staticmethod",) not in decorators)

    # Class.method(obj, ...) has an explicit receiver, except for @classmethod.
    if len(call.callee_chain) >= 2:
        class_name = target.class_qualname.rsplit(".", 1)[-1]
        local_receiver = call.callee_chain[-2]
        alias = caller.imports.get(local_receiver)
        if local_receiver == class_name or (len(call.callee_chain) == 2 and alias and alias[1] == class_name):
            return int(("classmethod",) in decorators)
    return None


def _signature(target: FunctionFact) -> Signature:
    return Signature(
        Parameter(p.name, _KINDS[p.kind], default=Parameter.empty if p.default is None else None)
        for p in target.parameters
    )


def _binding_error(signature: Signature, call: CallFact, receiver_count: int) -> str | None:
    keywords = dict.fromkeys(call.keyword_names)
    if any(p.kind == Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        # Python routes a positional-only name in **kwargs into the keyword dict.
        for parameter in signature.parameters.values():
            if parameter.kind == Parameter.POSITIONAL_ONLY:
                keywords.pop(parameter.name, None)
    try:
        signature.bind(*([None] * (call.positional_count + receiver_count)), **keywords)
    except TypeError as exc:
        return str(exc)
    return None


def _prior_calls(call: CallFact, caller: FileFacts, previous_caller: FileFacts | None) -> list[CallFact]:
    if previous_caller is None:
        return []
    head = call.callee_chain[0]
    if caller.imports.get(head) != previous_caller.imports.get(head):
        return []
    return [
        prior for prior in previous_caller.calls
        if prior.owner_function == call.owner_function and prior.callee_chain == call.callee_chain
    ]


def _same_positional_arguments(call: CallFact, caller: FileFacts, previous_caller: FileFacts | None) -> bool:
    if previous_caller is caller:
        return True
    if call._ast_node is None:
        return False
    arguments = [ast.dump(arg) for arg in call._ast_node.args]
    return any(
        prior._ast_node is not None and [ast.dump(arg) for arg in prior._ast_node.args] == arguments
        for prior in _prior_calls(call, caller, previous_caller)
    )


def _prior_call_bound(
    old: FunctionFact,
    signature: Signature,
    call: CallFact,
    caller: FileFacts,
    previous_caller: FileFacts | None,
    provenance: str,
) -> bool:
    if previous_caller is None or previous_caller is caller:
        return False
    candidates = _prior_calls(call, caller, previous_caller)
    if len(candidates) != 1:
        candidates = [prior for prior in candidates if prior.line == call.line]
    if len(candidates) != 1:
        return False
    prior = candidates[0]
    receiver = _receiver_count(old, prior, previous_caller, provenance)
    return (
        receiver is not None and not (prior.has_varargs or prior.has_kwargs)
        and _binding_error(signature, prior, receiver) is None
    )


_SYNC_ITERATING_BUILTINS = {
    "all", "any", "dict", "enumerate", "frozenset", "iter", "len", "list", "max", "min",
    "next", "set", "sorted", "sum", "tuple", "zip",
}


def _iterated_asynchronously(consumer: ast.AST | None) -> bool:
    return isinstance(consumer, ast.AsyncFor) or (
        isinstance(consumer, ast.comprehension) and bool(consumer.is_async)
    )


def _iterated_synchronously(consumer: ast.AST | None) -> bool:
    if isinstance(consumer, ast.For):
        return True
    if isinstance(consumer, ast.comprehension):
        return not consumer.is_async
    return (
        isinstance(consumer, ast.Call)
        and isinstance(consumer.func, ast.Name)
        and consumer.func.id in _SYNC_ITERATING_BUILTINS
    )


def _consumes_async_result(call: CallFact, caller: FileFacts) -> bool:
    consumer = call._result_consumer
    if isinstance(consumer, ast.Await) or _iterated_asynchronously(consumer):
        return True
    if isinstance(consumer, ast.Call):
        chain = dotted(consumer.func)
        alias = caller.imports.get(chain[0]) if chain else None
        if alias is not None:
            module, symbol = alias
            qualified = ".".join([module, *([symbol] if symbol else []), *chain[1:]])
            return qualified in _ASYNCIO_CONSUMERS
    return False


def signature_review_reason(
    old: FunctionFact,
    new: FunctionFact,
    call: CallFact,
    caller: FileFacts,
    previous_caller: FileFacts | None,
    provenance: str,
) -> str | None:
    """Describe an explicit call binding or coroutine change at a resolved target."""
    if provenance == "static_ast" and call_head_is_parameter(call, caller):
        return None
    old_receiver = _receiver_count(old, call, caller, provenance)
    new_receiver = _receiver_count(new, call, caller, provenance)
    if old_receiver is None or new_receiver is None:
        return None

    if not (call.has_varargs or call.has_kwargs):
        try:
            old_signature, new_signature = _signature(old), _signature(new)
        except ValueError:
            # The in-memory editor API can see partially written declarations.
            return None
        old_error = _binding_error(old_signature, call, old_receiver)
        new_error = _binding_error(new_signature, call, new_receiver)
        if new_error is not None and (
            old_error is None or _prior_call_bound(old, old_signature, call, caller, previous_caller, provenance)
        ):
            return f"call does not bind to the changed signature ({new_error})"
        if old_error is None and new_error is None and call.positional_count:
            old_slots = [p.name for p in old.parameters if p.kind in {"posonly", "positional"}]
            new_slots = [p.name for p in new.parameters if p.kind in {"posonly", "positional"}]
            before = old_slots[old_receiver:old_receiver + call.positional_count]
            after = new_slots[new_receiver:new_receiver + call.positional_count]
            for index, (previous, current) in enumerate(zip(before, after), 1):
                if previous != current and (previous in new_slots or current in old_slots):
                    if _same_positional_arguments(call, caller, previous_caller):
                        return f"positional argument {index} now binds to {current!r} instead of {previous!r}"
                    break

    if old.is_async != new.is_async:
        if new.is_async and new.is_generator:
            # Calling an async generator returns an async iterator at once: nothing is awaited.
            # A caller breaks if it still iterates the result with a plain `for`. That is known
            # when the old function was a generator, or when this call visibly iterates.
            consumer = call._result_consumer
            if not _iterated_asynchronously(consumer) and (old.is_generator or _iterated_synchronously(consumer)):
                return "target became an async generator; review how the call result is iterated"
            return None
        if new.is_async and not _consumes_async_result(call, caller):
            return "target became async; review how the call result is consumed"
        if not new.is_async and isinstance(call._result_consumer, ast.Await):
            return "call is still awaited after the target became synchronous"
    return None
