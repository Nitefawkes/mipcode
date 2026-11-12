"""False reports found by the held-out replays, reduced to their cause.

Each case is a small version of a site that two readers labeled a false report in
results/held_out. They were written after those results were read, so the held-out
repositories no longer count as unseen for these detectors.
"""
import pytest

from mipdelta.delta import analyze_delta


def _reports(findings):
    return [(f.detector, f.path, f.start_line) for f in findings]


def _evidence(findings):
    return [text for f in findings for text in f.evidence]


# call_signature_review: an async generator is not a coroutine (scrapy a8a8f20d9c49).

def test_function_that_becomes_an_async_generator_is_not_reported_as_a_coroutine():
    before = {
        "lib.py": "def rows(source):\n    return source.rows()\n",
        "client.py": "from lib import rows\n\ndef entry(source):\n    return wrap(rows(source))\n",
    }
    after = {
        **before,
        "lib.py": "async def rows(source):\n    async for row in source.rows():\n        yield row\n",
    }
    assert analyze_delta(before, after) == []


def test_generator_that_becomes_async_reports_a_call_iterated_synchronously():
    before = {
        "lib.py": "def rows(x):\n    yield x\n",
        "client.py": "from lib import rows\n\ndef entry():\n    return [row for row in rows(1)]\n",
    }
    after = {**before, "lib.py": "async def rows(x):\n    yield x\n"}
    findings = analyze_delta(before, after)
    assert _reports(findings) == [("call_signature_review", "client.py", 4)]
    assert _evidence(findings) == ["target became an async generator; review how the call result is iterated"]


@pytest.mark.parametrize(
    "body",
    ["    async for row in rows(1):\n        print(row)\n", "    return [row async for row in rows(1)]\n"],
    ids=["async-for", "async-comprehension"],
)
def test_generator_that_becomes_async_is_quiet_when_iterated_with_async_for(body):
    before = {
        "lib.py": "def rows(x):\n    yield x\n",
        "client.py": "from lib import rows\n\ndef entry():\n    return list(rows(1))\n",
    }
    after = {
        "lib.py": "async def rows(x):\n    yield x\n",
        "client.py": "from lib import rows\n\nasync def entry():\n" + body,
    }
    assert analyze_delta(before, after) == []


def test_yield_in_a_nested_function_does_not_make_the_outer_function_a_generator():
    before = {
        "lib.py": "def run(x):\n    def inner():\n        yield x\n    return inner()\n",
        "client.py": "from lib import run\n\ndef entry():\n    return run(1)\n",
    }
    after = {
        **before,
        "lib.py": "async def run(x):\n    def inner():\n        yield x\n    return inner()\n",
    }
    findings = analyze_delta(before, after)
    assert _reports(findings) == [("call_signature_review", "client.py", 4)]
    assert _evidence(findings) == ["target became async; review how the call result is consumed"]


# override_signature_review: a default moved into a named constant (urllib3 ae1f7da020e9).

OVERRIDE = "from base import Reader\n\nclass Wrapper(Reader):\n    def stream(self, amt=2**16):\n        return amt\n"
BASE = "class Reader:\n    def stream(self, amt=2**16):\n        return amt\n"
BASE_WITH_CONSTANT = "_CHUNK = {value}\n\nclass Reader:\n    def stream(self, amt=_CHUNK):\n        return amt\n"


@pytest.mark.parametrize("value", ["2**16", "65536", "64 * 1024"], ids=["same-expression", "literal", "product"])
def test_base_default_moved_into_a_constant_of_equal_value_is_not_drift(value):
    before = {"base.py": BASE, "child.py": OVERRIDE}
    after = {**before, "base.py": BASE_WITH_CONSTANT.format(value=value)}
    assert analyze_delta(before, after) == []


def test_base_default_moved_into_a_constant_of_another_value_is_still_drift():
    before = {"base.py": BASE, "child.py": OVERRIDE}
    after = {**before, "base.py": BASE_WITH_CONSTANT.format(value="2**20")}
    assert _reports(analyze_delta(before, after)) == [("override_signature_review", "child.py", 4)]


def test_a_reassigned_module_name_is_not_treated_as_a_constant():
    before = {"base.py": BASE, "child.py": OVERRIDE}
    after = {**before, "base.py": "_CHUNK = 1\n" + BASE_WITH_CONSTANT.format(value="2**16")}
    assert _reports(analyze_delta(before, after)) == [("override_signature_review", "child.py", 4)]


def test_rewriting_a_default_as_an_equal_literal_is_not_a_signature_change():
    before = {"base.py": BASE, "child.py": OVERRIDE}
    after = {**before, "base.py": BASE.replace("2**16", "65536")}
    assert analyze_delta(before, after) == []


# removed_callable_review: the name is still bound in the same scope.

def test_module_function_replaced_by_an_alias_is_still_bound():
    # attrs `new_class = types.new_class`, pytest `isdatacls = dataclasses.is_dataclass`.
    before = {
        "compat.py": "def is_record(value):\n    return hasattr(value, 'fields')\n",
        "client.py": "from compat import is_record\n\ndef entry(value):\n    return is_record(value)\n",
    }
    after = {**before, "compat.py": "import dataclasses\n\nis_record = dataclasses.is_dataclass\n"}
    assert analyze_delta(before, after) == []


def test_nested_function_replaced_by_an_unpacked_local_is_still_bound():
    # attrs `extra_lines, fmt_setter, fmt_setter_with_converter = _determine_setters(...)`.
    before = {
        "make.py": (
            "def script(names):\n"
            "    def fmt(name):\n"
            "        return name + ' = 1'\n"
            "    return [fmt(name) for name in names]\n"
        ),
    }
    after = {
        "make.py": (
            "def setters():\n"
            "    def fmt(name):\n"
            "        return name + ' = 1'\n"
            "    return [], fmt\n"
            "\n"
            "def script(names):\n"
            "    lines, fmt = setters()\n"
            "    return lines + [fmt(name) for name in names]\n"
        ),
    }
    assert analyze_delta(before, after) == []


def test_method_replaced_by_an_instance_attribute_is_still_bound():
    # attrs `_ClassBuilder._add_method_dunders` became an attribute set in `__init__`.
    before = {
        "builder.py": (
            "class Builder:\n"
            "    def finish(self, method):\n"
            "        return method\n"
            "    def build(self, method):\n"
            "        return self.finish(method)\n"
        ),
    }
    after = {
        "builder.py": (
            "class Builder:\n"
            "    def __init__(self, safe):\n"
            "        self.finish = self._safe if safe else self._fast\n"
            "    def _safe(self, method):\n"
            "        return method\n"
            "    def _fast(self, method):\n"
            "        return method\n"
            "    def build(self, method):\n"
            "        return self.finish(method)\n"
        ),
    }
    assert analyze_delta(before, after) == []


def test_method_replaced_by_a_class_level_alias_is_still_bound():
    before = {
        "builder.py": (
            "class Builder:\n"
            "    def finish(self, method):\n"
            "        return method\n"
            "    def build(self, method):\n"
            "        return self.finish(method)\n"
        ),
    }
    after = {
        "builder.py": (
            "def _finish(self, method):\n"
            "    return method\n"
            "\n"
            "class Builder:\n"
            "    finish = _finish\n"
            "    def build(self, method):\n"
            "        return self.finish(method)\n"
        ),
    }
    assert analyze_delta(before, after) == []


STATUS = "class {name}:\n    @classmethod\n    def phrase(cls, value):\n        return str(value)\n"
STATUS_CALLER = "from status import codes\n\ndef reason(value):\n    return codes.phrase(value)\n"


def test_class_renamed_to_its_alias_keeps_its_methods():
    # httpx: `codes = StatusCode` became `class codes`, and callers already used `codes`.
    before = {"status.py": STATUS.format(name="StatusCode") + "\ncodes = StatusCode\n", "models.py": STATUS_CALLER}
    after = {**before, "status.py": STATUS.format(name="codes")}
    assert analyze_delta(before, after) == []


def test_class_renamed_without_an_alias_still_reports_callers_of_the_old_name():
    caller = "from status import StatusCode\n\ndef reason(value):\n    return StatusCode.phrase(value)\n"
    before = {"status.py": STATUS.format(name="StatusCode"), "models.py": caller}
    after = {**before, "status.py": STATUS.format(name="codes")}
    assert _reports(analyze_delta(before, after)) == [("removed_callable_review", "models.py", 4)]


def test_removed_method_with_no_other_binding_is_still_reported():
    before = {
        "builder.py": (
            "class Builder:\n"
            "    def finish(self, method):\n"
            "        return method\n"
            "    def build(self, method):\n"
            "        return self.finish(method)\n"
        ),
    }
    after = {
        "builder.py": (
            "class Builder:\n"
            "    def build(self, method):\n"
            "        return self.finish(method)\n"
        ),
    }
    assert _reports(analyze_delta(before, after)) == [("removed_callable_review", "builder.py", 3)]


# Limits on the rules above, found by an independent review of the first version.

def test_a_huge_folded_default_does_not_stop_the_analysis():
    factors = " * ".join(["(2**32)**64"] * 8)
    before = {"m.py": "def f(x=1):\n    return x\n"}
    after = {"m.py": f"LIMIT = {factors}\n\ndef f(x={factors}):\n    return x\n"}
    assert analyze_delta(before, after) == []


def test_override_that_imports_the_new_base_constant_is_not_drift():
    before = {"base.py": BASE.replace("2**16", "1024"), "child.py": OVERRIDE.replace("2**16", "1024")}
    after = {
        "base.py": BASE_WITH_CONSTANT.format(value="65536"),
        "child.py": OVERRIDE.replace("import Reader", "import Reader, _CHUNK").replace("2**16", "_CHUNK"),
    }
    assert analyze_delta(before, after) == []


@pytest.mark.parametrize(
    "constant_lines",
    [
        "_CHUNK = 65536\n\nclass Reader:\n    _CHUNK = 1024\n",
        "_CHUNK = 65536\nfrom fast import _CHUNK\n\nclass Reader:\n",
        "_CHUNK = 65536\ndef _CHUNK():\n    return 1024\n\nclass Reader:\n",
    ],
    ids=["class-attribute-shadows", "import-rebinds", "def-rebinds"],
)
def test_a_shadowed_or_rebound_name_is_not_resolved_as_the_module_constant(constant_lines):
    child = OVERRIDE.replace("2**16", "65536")
    before = {"base.py": BASE.replace("2**16", "65536"), "child.py": child}
    after = {"base.py": constant_lines + "    def stream(self, amt=_CHUNK):\n        return amt\n", "child.py": child}
    assert _reports(analyze_delta(before, after)) == [("override_signature_review", "child.py", 4)]


def test_large_module_literals_are_not_kept_as_constants():
    from mipdelta.facts import parse_file

    facts = parse_file("data.py", "TABLE = [" + ", ".join(str(n) for n in range(2000)) + "]\nSIZE = 2**16\n")
    assert list(facts.constant_defaults.values()) == ["Constant(value=65536)"]


BOX = (
    "class Box:\n"
    "    def __init__(self):\n"
    "        self.items = []\n"
    "{init}"
    "{body}"
    "    def report(self):\n"
    "        return self.size() + 1\n"
)
BOX_METHOD = "    def size(self):\n        return len(self.items)\n"


@pytest.mark.parametrize(
    ("init", "body"),
    [
        ("        self.size = 0\n", ""),
        ("        self.size = None\n", ""),
        ("", "    size = 0\n"),
        ("", "    def clear(self):\n        del self.size\n"),
        ("", "    def add(self, item):\n        self.size.append(item)\n"),
    ],
    ids=["instance-number", "instance-none", "class-number", "deleted", "mutated"],
)
def test_method_replaced_by_data_is_still_reported(init, body):
    before = {"box.py": BOX.format(init="", body=BOX_METHOD)}
    after = {"box.py": BOX.format(init=init, body=body)}
    assert ("removed_callable_review", "box.py") in [(d, p) for d, p, _ in _reports(analyze_delta(before, after))]


@pytest.mark.parametrize(
    "replacement",
    [
        "from typing import Callable\n\ncompute: Callable[[int], int]\n",
        "compute = None\n",
        "compute = 1\ndel compute\n",
        "for compute in range(3):\n    pass\n",
        "with open('x') as compute:\n    pass\n",
        "values = [1, 2]\nfirst, *compute = [1, 2]\n",
    ],
    ids=["annotation-only", "none", "deleted", "loop-target", "with-target", "star-unpacked-literal"],
)
def test_module_name_bound_to_something_not_callable_is_still_reported(replacement):
    before = {
        "lib.py": "def compute(x):\n    return x\n",
        "client.py": "from lib import compute\n\ndef entry():\n    return compute(1)\n",
    }
    after = {**before, "lib.py": replacement}
    assert _reports(analyze_delta(before, after)) == [("removed_callable_review", "client.py", 4)]


@pytest.mark.parametrize(
    "binding",
    ["    fmt = '{} = 1'\n", "    for fmt in names:\n        pass\n"],
    ids=["string", "loop-target"],
)
def test_nested_function_replaced_by_a_local_that_is_not_callable_is_still_reported(binding):
    before = {
        "make.py": "def script(names):\n    def fmt(name):\n        return name\n    return [fmt(name) for name in names]\n",
    }
    after = {"make.py": "def script(names):\n" + binding + "    return [fmt(name) for name in names]\n"}
    assert [d for d, _, _ in _reports(analyze_delta(before, after))] == ["removed_callable_review"]


@pytest.mark.parametrize(
    "body",
    ["    return [row for row in rows(1)]\n", "    for row in rows(1):\n        print(row)\n", "    return list(rows(1))\n"],
    ids=["comprehension", "for-loop", "list-call"],
)
def test_function_that_becomes_an_async_generator_reports_a_call_iterated_synchronously(body):
    before = {
        "lib.py": "def rows(x):\n    return [x]\n",
        "client.py": "from lib import rows\n\ndef entry():\n" + body,
    }
    after = {**before, "lib.py": "async def rows(x):\n    yield x\n"}
    findings = analyze_delta(before, after)
    assert [(d, p) for d, p, _ in _reports(findings)] == [("call_signature_review", "client.py")]
    assert _evidence(findings) == ["target became an async generator; review how the call result is iterated"]


def test_a_coroutine_iterated_by_a_plain_comprehension_is_still_reported():
    before = {
        "lib.py": "def rows(x):\n    return [x]\n",
        "client.py": "from lib import rows\n\ndef entry():\n    return [row for row in rows(1)]\n",
    }
    after = {**before, "lib.py": "async def rows(x):\n    return [x]\n"}
    assert _evidence(analyze_delta(before, after)) == ["target became async; review how the call result is consumed"]
