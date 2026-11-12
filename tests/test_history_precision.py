"""Quiet cases reduced from reports on complete upstream commits during history replay."""
import pytest

from mipdelta.delta import analyze_delta


def _detectors(before, after):
    return sorted({f.detector for f in analyze_delta(before, after)})


@pytest.mark.parametrize(
    "body",
    [
        '        """Filter the items."""',
        "        ...",
        "        raise NotImplementedError",
        '        """Filter."""\n        raise NotImplementedError()',
    ],
    ids=["docstring", "ellipsis", "not-implemented", "docstring-and-raise"],
)
def test_declaration_without_a_body_can_gain_a_parameter(body):
    before = {"spec.py": f"class Base:\n    def filter(self, items):\n{body}\n"}
    after = {"spec.py": f"class Base:\n    def filter(self, items, key=None):\n{body}\n"}
    assert _detectors(before, after) == []


def test_abstract_method_with_a_default_body_can_gain_a_parameter():
    template = (
        "import abc\n\nclass Base(abc.ABC):\n    @abc.abstractmethod\n"
        "    def filter(self, items{extra}):\n        return list(items)\n"
    )
    assert _detectors({"spec.py": template.format(extra="")}, {"spec.py": template.format(extra=", key=None")}) == []


def test_catch_all_parameters_are_not_expected_to_be_read():
    before = {"ranges.py": "class Range:\n    def __new__(cls):\n        return super().__new__(cls)\n"}
    after = {"ranges.py": "class Range:\n    def __new__(cls, *args, **kwargs):\n        return super().__new__(cls)\n"}
    assert _detectors(before, after) == []


def test_unused_parameter_of_a_new_function_is_not_a_contract_change():
    before = {"mod.py": "X = 1\n"}
    after = {"mod.py": "X = 1\n\ndef build(data, flag):\n    return data\n"}
    assert _detectors(before, after) == []


def test_implemented_function_gaining_an_unread_parameter_is_still_reported():
    before = {"mod.py": "def build(data):\n    return data\n"}
    after = {"mod.py": "def build(data, flag=False):\n    return data\n"}
    assert _detectors(before, after) == ["parameter_sink_review"]


def test_class_moved_to_another_file_keeps_its_internal_calls():
    cls = (
        "class Boundary:\n"
        "    def _is_family(self, other):\n        return True\n\n"
        "    def __lt__(self, other):\n        return self._is_family(other)\n"
    )
    before = {"specifiers.py": cls}
    after = {"specifiers.py": "from _ranges import Boundary\n", "_ranges.py": cls}
    assert _detectors(before, after) == []


def _tags(limit_param: str, guard: str) -> str:
    return (
        "class InvalidTag(ValueError):\n    pass\n\n"
        "class TooManyTags(ValueError):\n    pass\n\n"
        f"def parse_tag(tag{limit_param}):\n"
        "    if not tag:\n        raise InvalidTag(tag)\n"
        f"{guard}"
        "    return tag.split('.')\n"
    )


def _wheel(call: str) -> str:
    return (
        "from tags import InvalidTag, parse_tag\n\n"
        "def parse_wheel(name):\n"
        "    try:\n"
        f"        return {call}\n"
        "    except InvalidTag:\n        return None\n"
    )


GUARD = "    if limit is not None and len(tag) > limit:\n        raise TooManyTags(tag)\n"


def test_exception_behind_an_omitted_new_parameter_does_not_reach_the_caller():
    before = {"tags.py": _tags("", ""), "utils.py": _wheel("parse_tag(name)")}
    after = {**before, "tags.py": _tags(", *, limit=None", GUARD)}
    assert _detectors(before, after) == []


@pytest.mark.parametrize("call", ["parse_tag(name, limit=3)", "parse_tag(name, **options)"])
def test_exception_behind_a_supplied_new_parameter_is_reported(call):
    before = {"tags.py": _tags("", ""), "utils.py": _wheel("parse_tag(name)")}
    after = {"tags.py": _tags(", *, limit=None", GUARD), "utils.py": _wheel(call).replace("def parse_wheel(name)", "def parse_wheel(name, **options)")}
    assert "exception_contract_review" in _detectors(before, after)


def test_unconditional_new_exception_is_reported_even_with_a_new_parameter():
    before = {"tags.py": _tags("", ""), "utils.py": _wheel("parse_tag(name)")}
    after = {**before, "tags.py": _tags(", *, limit=None", "    if len(tag) > 99:\n        raise TooManyTags(tag)\n")}
    assert "exception_contract_review" in _detectors(before, after)


def test_renamed_positional_parameter_is_not_a_new_unread_parameter():
    before = {"items.py": "class Key:\n    def __eq__(self, other):\n        return NotImplemented\n\n    def get(self, key):\n        return self\n"}
    after = {"items.py": "class Key:\n    def __eq__(self, __o):\n        return NotImplemented\n\n    def get(self, name):\n        return self\n"}
    assert _detectors(before, after) == []


def test_override_keeps_matching_a_renamed_positional_base_parameter():
    template = (
        "class Key:\n    def __eq__(self, {name}):\n        return self is {name}\n\n"
        "class SingleKey(Key):\n    def __eq__(self, other):\n        return self is other\n"
    )
    assert _detectors({"items.py": template.format(name="other")}, {"items.py": template.format(name="__o")}) == []


def test_parameter_consumed_by_the_paired_constructor_is_not_unread():
    template = (
        "class Stamp(tuple):\n"
        "    def __new__(cls, value{extra}):\n        return super().__new__(cls, (value,{use}))\n\n"
        "    def __init__(self, value{extra}):\n        self.ready = True\n"
    )
    before = {"items.py": template.format(extra="", use="")}
    after = {"items.py": template.format(extra=", fold=0", use=" fold")}
    assert _detectors(before, after) == []


def test_local_name_shadowing_a_changed_function_is_not_a_callback_binding():
    api = (
        "def string(raw{extra}):\n    return str(raw){tail}\n\n"
        "def parse(string):\n    return Parser(string)\n\n"
        "def each(values):\n    for string in values:\n        emit(string)\n"
    )
    before = {"api.py": api.format(extra="", tail="")}
    after = {"api.py": api.format(extra=", *, literal", tail=" if literal else ''")}
    assert _detectors(before, after) == []


def test_raising_the_result_of_a_factory_method_is_not_a_named_exception():
    parser = (
        "class EmptyKey(Exception):\n    pass\n\n"
        "class Parser:\n"
        "    def parse_error(self, kind):\n        return kind()\n\n"
        "    def _key(self):\n{body}        return 1\n\n"
        "    def table(self):\n        try:\n            return self._key()\n        except EmptyKey:\n            return None\n"
    )
    before = {"parser.py": parser.format(body="")}
    after = {"parser.py": parser.format(body="        if self.depth > 9:\n            raise self.parse_error(ValueError)\n")}
    assert _detectors(before, after) == []


def test_method_removed_in_favor_of_an_external_base_class_is_not_reported():
    before = {
        "items.py": "class AoT:\n    def append(self, x):\n        self.body = x\n\n    def extend(self, xs):\n        for x in xs:\n            self.append(x)\n"
    }
    after = {
        "items.py": "from collections import UserList\n\nclass AoT(UserList):\n    def extend(self, xs):\n        for x in xs:\n            self.append(x)\n"
    }
    assert _detectors(before, after) == []


def test_method_removed_from_a_class_without_bases_is_still_reported():
    before = {
        "items.py": "class AoT:\n    def append(self, x):\n        self.body = x\n\n    def extend(self, xs):\n        for x in xs:\n            self.append(x)\n"
    }
    after = {"items.py": "class AoT:\n    def extend(self, xs):\n        for x in xs:\n            self.append(x)\n"}
    assert _detectors(before, after) == ["removed_callable_review"]


@pytest.mark.parametrize(
    ("before_src", "after_src"),
    [
        (
            "class File:\n    def __exit__(self):\n        self.close()\n",
            "class File:\n    def __exit__(self, exc_type, exc_value, tb):\n        self.close()\n",
        ),
        (
            "def option():\n    def show(ctx):\n        ctx.exit()\n    return show\n",
            "def option():\n    def show(ctx, param):\n        ctx.exit()\n    return show\n",
        ),
        (
            "class ParamType:\n    def metavar(self):\n        return None\n\n"
            "class Choice(ParamType):\n    def metavar(self):\n        return 'x'\n",
            "class ParamType:\n    def metavar(self, ctx):\n        return None\n\n"
            "class Choice(ParamType):\n    def metavar(self, ctx):\n        return ctx.name\n",
        ),
        (
            "class Base:\n    def render(self):\n        return ''\n\n"
            "class Child(Base):\n    def render(self):\n        return 'x'\n",
            "class Base:\n    def render(self, ctx):\n        return ''\n\n"
            "class Child(Base):\n    def render(self, ctx):\n        return 'x'\n",
        ),
    ],
    ids=["protocol-method", "nested-callback", "overridden-hook", "override-of-trivial-base"],
)
def test_parameter_required_by_a_shared_signature_is_not_reported_as_unread(before_src, after_src):
    assert _detectors({"mod.py": before_src}, {"mod.py": after_src}) == []


def test_nested_helper_is_not_confused_with_a_same_named_helper_elsewhere():
    template = (
        "class Response:\n"
        "    def www_authenticate(self):\n"
        "        def on_update(value{extra}):\n            self.header = value\n"
        "        return Tracker(on_update)\n\n"
        "    def mimetype_params(self):\n"
        "        def on_update(value):\n            self.params = value\n"
        "        return Tracker(on_update)\n"
    )
    before = {"response.py": template.format(extra="")}
    after = {"response.py": template.format(extra=", source=None")}
    found = [(f.detector, f.start_line) for f in analyze_delta(before, after) if f.detector == "callback_binding_review"]
    assert found == [("callback_binding_review", 5)]


def test_nested_function_extracted_to_module_level_keeps_its_callers():
    before = {"serving.py": "def run():\n    def log_startup(sock):\n        return sock\n    return log_startup(1)\n"}
    after = {"serving.py": "def log_startup(sock):\n    return sock\n\ndef run():\n    return log_startup(1)\n"}
    assert _detectors(before, after) == []


def test_removed_nested_function_does_not_claim_calls_to_an_imported_name():
    before = {
        "urls.py": "def _make():\n    def quote(text):\n        return text\n    return quote\n\nurl_quote = _make()\n"
    }
    after = {"urls.py": "from urllib.parse import quote\n\ndef url_quote(text):\n    return quote(text)\n"}
    assert _detectors(before, after) == []


def test_module_that_became_a_package_still_exports_the_name():
    before = {
        "pkg/data.py": "def iter_items(mapping):\n    return list(mapping)\n",
        "pkg/request.py": "from .data import iter_items\n\ndef args(m):\n    return iter_items(m)\n",
    }
    after = {
        "pkg/data/__init__.py": "from .structures import iter_items\n",
        "pkg/data/structures.py": "def iter_items(mapping):\n    return list(mapping)\n",
        "pkg/request.py": before["pkg/request.py"],
    }
    assert _detectors(before, after) == []


def test_private_backing_attribute_is_not_parallel_state():
    template = (
        "class Builder:\n"
        "    def __init__(self):\n        self.form = None\n\n"
        "    def load(self, value):\n{write}\n"
        "    def environ(self):\n        return self.form\n"
    )
    before = {"test.py": template.format(write="        self.form = value")}
    after = {"test.py": template.format(write="        self._form = value")}
    assert _detectors(before, after) == []


def _graphs(base_body: str, child_body: str, parameter: str) -> str:
    return (
        f"class Graph:\n    def parse(self, source{parameter}):\n{base_body}\n"
        f"class Conjunctive(Graph):\n    def parse(self, source{parameter}):\n{child_body}"
    )


def test_override_that_ignores_a_parameter_its_base_consumes_is_reported():
    before = _graphs("        return load(source)\n", "        return load_all(source)\n", "")
    after = _graphs("        return load(source, base_iri)\n", "        return load_all(source)\n", ", base_iri=None")
    found = [(f.detector, f.symbol) for f in analyze_delta({"graph.py": before}, {"graph.py": after})]
    assert ("parameter_sink_review", "Conjunctive.parse") in found
    assert ("parameter_sink_review", "Graph.parse") not in found


def test_parameter_unread_across_a_working_method_family_is_reported_for_each_member():
    before = _graphs("        return load(source)\n", "        return load_all(source)\n", "")
    after = _graphs("        return load(source)\n", "        return load_all(source)\n", ", base_iri=None")
    found = sorted(f.symbol for f in analyze_delta({"graph.py": before}, {"graph.py": after}) if f.detector == "parameter_sink_review")
    assert found == ["Conjunctive.parse", "Graph.parse"]

