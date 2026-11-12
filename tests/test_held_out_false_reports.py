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
