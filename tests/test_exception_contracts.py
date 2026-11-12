"""Exception hierarchy and bounded propagation for exception_contract_review."""
import pytest

from mipdelta.delta import analyze_delta


def _exception_findings(before, after):
    return [
        (f.path, f.start_line, f.evidence[0])
        for f in analyze_delta(before, after)
        if f.detector == "exception_contract_review"
    ]


def _caller(handler: str) -> str:
    return (
        "from lib import load\n\n"
        "def use():\n"
        "    try:\n"
        "        return load('a')\n"
        f"    except {handler}:\n"
        "        return None\n"
    )


@pytest.mark.parametrize(
    ("raised", "handler"),
    [
        ("KeyError", "LookupError"),
        ("FileNotFoundError", "OSError"),
        ("FileNotFoundError", "IOError"),
        ("UnicodeDecodeError", "ValueError"),
        ("KeyError", "(TypeError, LookupError)"),
    ],
)
def test_builtin_base_class_handler_catches_the_new_exception(raised, handler):
    before = {"lib.py": "def load(p):\n    return p\n", "app.py": _caller(handler)}
    after = {**before, "lib.py": f"def load(p):\n    if not p:\n        raise {raised}(p)\n    return p\n"}
    assert _exception_findings(before, after) == []


def test_unrelated_builtin_handler_is_still_reported():
    before = {"lib.py": "def load(p):\n    return p\n", "app.py": _caller("LookupError")}
    after = {**before, "lib.py": "def load(p):\n    if not p:\n        raise ValueError(p)\n    return p\n"}
    assert [(path, line) for path, line, _ in _exception_findings(before, after)] == [("app.py", 5)]


def test_repository_hierarchy_is_followed_through_several_bases():
    errors = (
        "class AppError(Exception):\n    pass\n\n"
        "class ParseError(AppError):\n    pass\n\n"
        "class EmptyInput(ParseError):\n    pass\n"
    )
    before = {
        "errors.py": errors,
        "lib.py": "from errors import EmptyInput\n\ndef load(p):\n    return p\n",
        "app.py": _caller("AppError").replace("from lib import load", "from lib import load\nfrom errors import AppError"),
    }
    after = {
        **before,
        "lib.py": "from errors import EmptyInput\n\ndef load(p):\n    if not p:\n        raise EmptyInput(p)\n    return p\n",
    }
    assert _exception_findings(before, after) == []


def test_repository_exception_rooted_in_a_builtin_is_caught_by_that_builtin():
    before = {
        "lib.py": "class Missing(KeyError):\n    pass\n\ndef load(p):\n    return p\n",
        "app.py": _caller("LookupError"),
    }
    after = {
        **before,
        "lib.py": "class Missing(KeyError):\n    pass\n\ndef load(p):\n    if not p:\n        raise Missing(p)\n    return p\n",
    }
    assert _exception_findings(before, after) == []


def test_unknown_hierarchies_on_both_sides_abstain():
    # Neither class is defined in the workspace, so their relationship is unknown.
    before = {
        "lib.py": "import vendor\n\ndef load(p):\n    return p\n",
        "app.py": (
            "import vendor\nfrom lib import load\n\n"
            "def use():\n    try:\n        return load('a')\n    except vendor.TransportError:\n        return None\n"
        ),
    }
    after = {
        **before,
        "lib.py": "import vendor\n\ndef load(p):\n    if not p:\n        raise vendor.Timeout(p)\n    return p\n",
    }
    assert _exception_findings(before, after) == []


def test_new_exception_propagates_through_an_unguarded_intermediate_caller():
    before = {
        "lib.py": "def _read(p):\n    return p\n\ndef load(p):\n    return _read(p)\n",
        "app.py": _caller("ValueError"),
    }
    after = {
        **before,
        "lib.py": (
            "class Bad(Exception):\n    pass\n\n"
            "def _read(p):\n    if not p:\n        raise Bad(p)\n    return p\n\n"
            "def load(p):\n    return _read(p)\n"
        ),
    }
    found = _exception_findings(before, after)
    assert [(path, line) for path, line, _ in found] == [("app.py", 5)]
    assert "Bad" in found[0][2] and "load" in found[0][2]


@pytest.mark.parametrize("guard", ["Exception", "Bad"])
def test_propagation_stops_where_an_intermediate_caller_handles_it(guard):
    lib_after = (
        "class Bad(Exception):\n    pass\n\n"
        "def _read(p):\n    if not p:\n        raise Bad(p)\n    return p\n\n"
        "def load(p):\n"
        "    try:\n        return _read(p)\n"
        f"    except {guard}:\n        return None\n"
    )
    lib_before = lib_after.replace("    if not p:\n        raise Bad(p)\n", "")
    before = {"lib.py": lib_before, "app.py": _caller("ValueError")}
    after = {**before, "lib.py": lib_after}
    assert _exception_findings(before, after) == []


def test_propagation_is_bounded_to_two_intermediate_callers():
    def chain(raising: bool) -> str:
        body = "    if not p:\n        raise Bad(p)\n    return p\n" if raising else "    return p\n"
        return (
            "class Bad(Exception):\n    pass\n\n"
            f"def f0(p):\n{body}\n"
            "def f1(p):\n    return f0(p)\n\n"
            "def f2(p):\n    return f1(p)\n\n"
            "def f3(p):\n    return f2(p)\n"
        )

    def caller(name: str) -> str:
        return _caller("ValueError").replace("load", name)

    for name, expected in (("f2", 1), ("f3", 0)):
        before = {"lib.py": chain(False), "app.py": caller(name)}
        after = {**before, "lib.py": chain(True)}
        assert len(_exception_findings(before, after)) == expected, name
