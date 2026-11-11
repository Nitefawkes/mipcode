"""Contract changes at calls resolved to a specific Python definition."""
import pytest

from mipdelta.delta import analyze_delta
from mipdelta.session import DeltaSession


@pytest.mark.parametrize(
    ("before_def", "after_def", "expression"),
    [
        ("def run(x): return x", "def run(x, y): return x + y", "invoke(1)"),
        ("def run(x, y=0): return x + y", "def run(x): return x", "invoke(1, 2)"),
        ("def run(*, flag=False): return flag", "def run(*, strict=False): return strict", "invoke(flag=True)"),
        ("def run(x, y): return x-y", "def run(y, x): return x-y", "invoke(1, 2)"),
        ("def run(x): return x", "async def run(x): return x", "invoke(1)"),
        ("def run(x): return [x]", "async def run(x): return [x]", "len(invoke(1))"),
        ("def run(x, /, **kw): return x", "def run(x, y, /, **kw): return x + y", "invoke(1, x=2)"),
    ],
    ids=["required-positional", "removed-parameter", "renamed-keyword", "reordered-positional",
         "became-async", "async-result-used-synchronously", "positional-only-with-kwargs"],
)
def test_broken_calls_report_the_import_alias_location(before_def, after_def, expression):
    caller = f"from lib import run as invoke\n\ndef entry():\n    return {expression}\n"
    before = {"lib.py": before_def + "\n", "client.py": caller}
    after = dict(before, **{"lib.py": after_def + "\n"})
    findings = analyze_delta(before, after)
    assert [(f.detector, f.symbol, f.path, f.start_line, f.resolution_source) for f in findings] == [
        ("call_signature_review", "run", "client.py", 4, "static_ast"),
    ]


def test_required_keyword_still_uses_the_existing_detector_without_a_duplicate():
    before = {
        "lib.py": "def run(x, *, flag=True): return x if flag else 0\n",
        "client.py": "from lib import run as invoke\ninvoke(1)\n",
    }
    after = {**before, "lib.py": "def run(x, *, flag): return x if flag else 0\n"}
    assert [(f.detector, f.path, f.start_line) for f in analyze_delta(before, after)] == [
        ("required_keyword_missing", "client.py", 2)
    ]


@pytest.mark.parametrize(
    ("before_def", "after_def", "before_call", "after_call"),
    [
        ("def run(x): return x", "def run(x, y): return x+y", "invoke(1)", "invoke(1, 2)"),
        ("def run(x, y): return x-y", "def run(y, x): return x-y", "invoke(x=1, y=2)", "invoke(x=1, y=2)"),
        ("def run(x, y): return x-y", "def run(y, x): return x-y", "invoke(1, 2)", "invoke(2, 1)"),
        ("def run(x, y=0): return x+y", "def run(x): return x", "invoke(1)", "invoke(1)"),
        ("def run(x): return x", "def run(x, y): return x+y", "invoke(*args)", "invoke(*args)"),
        ("def run(x): return x", "def run(x, y): return x+y", "invoke(**kw)", "invoke(**kw)"),
        ("def run(x=0, /, **kw): return x", "def run(x=0, /, flag=False, **kw): return x if flag else -x",
         "invoke(x=2)", "invoke(x=2)"),
    ],
    ids=["migrated", "named-arguments", "migrated-order", "unused-removal", "varargs", "kwargs", "positional-only-keyword"],
)
def test_compatible_or_unresolved_calls_do_not_get_signature_findings(before_def, after_def, before_call, after_call):
    before = {"lib.py": before_def + "\n", "client.py": f"from lib import run as invoke\n{before_call}\n"}
    after = {"lib.py": after_def + "\n", "client.py": f"from lib import run as invoke\n{after_call}\n"}
    assert [f for f in analyze_delta(before, after) if f.detector == "call_signature_review"] == []


@pytest.mark.parametrize("expression", ["await invoke(1)", "await asyncio.gather(invoke(1))"])
def test_async_migration_handles_calls_awaited_or_passed_to_async_runner(expression):
    before = {"lib.py": "def run(x): return x\n", "client.py": "from lib import run as invoke\ninvoke(1)\n"}
    after = {"lib.py": "async def run(x): return x\n",
             "client.py": f"import asyncio\nfrom lib import run as invoke\nasync def main():\n    return {expression}\n"}
    assert analyze_delta(before, after) == []


def test_required_positional_in_a_bound_method_accounts_for_receiver():
    before = {
        "api.py": "class Service:\n    def encode(self, value):\n        return value\n",
        "app.py": "from api import Service\n\nService().encode(1)\n",
    }
    session = DeltaSession.from_workspace(before)
    result = session.apply_write(
        "api.py", "class Service:\n    def encode(self, value, policy):\n        return policy(value)\n"
    )
    assert result.cross_file_triggered
    assert [(f.detector, f.path, f.start_line) for f in result.findings] == [
        ("call_signature_review", "app.py", 3)
    ]


def test_reorder_still_reports_an_unchanged_call_in_the_edited_file():
    before = {"lib.py": "def run(x, y): return x-y\n\nrun(1, 2)\n"}
    after = {"lib.py": "def run(y, x): return x-y\n\nrun(1, 2)\n"}
    assert [(f.detector, f.start_line) for f in analyze_delta(before, after)] == [
        ("call_signature_review", 3)
    ]


def test_partial_caller_migration_still_reports_a_missing_argument():
    before = {
        "lib.py": "def run(x): return x\n",
        "client.py": "from lib import run\n\nrun(1)\n",
    }
    after = {
        "lib.py": "def run(x, y, z): return x + y + z\n",
        "client.py": "from lib import run\n\nrun(1, 2)\n",
    }
    assert [(f.detector, f.path, f.start_line) for f in analyze_delta(before, after)] == [
        ("call_signature_review", "client.py", 3)
    ]


@pytest.mark.parametrize("replacement", ["def run(x, y): return x + y\n", ""], ids=["changed", "removed"])
def test_parameter_shadowing_an_import_is_not_a_caller(replacement):
    before = {
        "lib.py": "def run(x): return x\n",
        "client.py": "from lib import run\n\ndef wrapper(run):\n    return run(1)\n",
    }
    after = {**before, "lib.py": replacement}
    assert analyze_delta(before, after) == []


def test_sync_replacement_reports_a_call_that_still_awaits_it():
    before = {
        "lib.py": "async def run(x): return x\n",
        "client.py": "from lib import run\n\nasync def main():\n    return await run(1)\n",
    }
    after = dict(before, **{"lib.py": "def run(x): return x\n"})
    assert [(f.detector, f.start_line) for f in analyze_delta(before, after)] == [
        ("call_signature_review", 4)
    ]


@pytest.mark.parametrize("replacement", ["def renamed(x): return x\n", None], ids=["renamed", "file-deleted"])
def test_removed_definition_reports_only_callers_bound_to_it(replacement):
    before = {
        "lib.py": "def run(x): return x\n",
        "other.py": "def run(x): return x\n",
        "client.py": "from lib import run as invoke\nfrom other import run\n\ninvoke(1)\nrun(2)\n",
    }
    after = dict(before)
    if replacement is None:
        del after["lib.py"]
    else:
        after["lib.py"] = replacement
    findings = analyze_delta(before, after)
    assert [(f.detector, f.symbol, f.path, f.start_line) for f in findings] == [
        ("removed_callable_review", "run", "client.py", 4),
    ]


def test_migrated_import_does_not_report_removed_callable():
    before = {"lib.py": "def run(x): return x\n", "client.py": "from lib import run as invoke\ninvoke(1)\n"}
    after = {"lib.py": "def renamed(x): return x\n", "client.py": "from lib import renamed as invoke\ninvoke(1)\n"}
    assert analyze_delta(before, after) == []


def test_module_move_does_not_satisfy_a_stale_import_with_the_same_leaf_name():
    before = {"lib.py": "def run(x): return x\n", "client.py": "from lib import run\nrun(1)\n"}
    after = {"moved/lib.py": before["lib.py"], "client.py": before["client.py"]}
    assert [(f.detector, f.path, f.start_line) for f in analyze_delta(before, after)] == [
        ("removed_callable_review", "client.py", 2)
    ]


def test_reexport_at_the_original_import_location_preserves_callers():
    before = {"lib.py": "def run(x): return x\n", "client.py": "from lib import run\nrun(1)\n"}
    after = {**before, "lib.py": "from moved import implementation as run\n",
             "moved.py": "def implementation(x): return x\n"}
    assert analyze_delta(before, after) == []
