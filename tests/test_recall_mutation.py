"""Stale-file mutation corpus: ground truth and scoring on throwaway repositories."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "recall_mutation"))

build_and_score = importlib.import_module("build_and_score")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, files: dict[str, str | None], message: str) -> None:
    for path, text in files.items():
        target = repo / path
        if text is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _history(tmp_path: Path, parent: dict[str, str], child: dict[str, str | None]) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    _commit(repo, parent, "parent")
    _commit(repo, child, "child")
    return build_and_score.build_repository(repo, None)


def _sites(report: dict) -> list[dict]:
    return [site for mutation in report["mutations"] for site in mutation["sites"]]


def test_required_parameter_with_stale_caller_is_breaking_and_detected(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1)\n",
        },
        {
            "pkg/core.py": "def render_page(data, width):\n    return data * width\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1, 80)\n",
        },
    )
    [mutation] = report["mutations"]
    assert mutation["stale_path"] == "pkg/use.py"
    assert mutation["analysis_error"] is None
    [site] = mutation["sites"]
    assert site["line"] == 5
    assert site["call_text"] == "render_page(1)"
    assert site["binding"] == "breaking"
    assert site["name_uniqueness"] == "unique"
    assert site["definition"]["old_parameters"] == "(data)"
    assert site["definition"]["new_parameters"] == "(data, width)"
    assert site["enclosing_function"] == "show"
    assert site["site_hit"] and site["file_hit"]
    assert site["site_hit_detectors"]
    assert mutation["collateral"] == []

    summary = build_and_score.summarize([report])
    cell = summary["pooled"]["strata"]["breaking x unique"]
    assert (cell["sites"], cell["site_hits"], cell["site_recall"]) == (1, 1, 1.0)


def test_optional_parameter_is_compatible(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1)\n",
        },
        {
            "pkg/core.py": "def render_page(data, width=80):\n    return data * width\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1, width=40)\n",
        },
    )
    [site] = _sites(report)
    assert site["binding"] == "compatible"
    assert site["definition"]["new_parameters"] == "(data, width=...)"


def test_removed_function_is_breaking_removed(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def old_helper(x):\n    return x\n\n\ndef keep():\n    return 0\n",
            "pkg/use.py": "from pkg.core import old_helper\n\n\ndef show():\n    return old_helper(2)\n",
        },
        {
            "pkg/core.py": "def keep():\n    return 0\n",
            "pkg/use.py": "def show():\n    return 2\n",
        },
    )
    [mutation] = report["mutations"]
    [site] = mutation["sites"]
    assert site["binding"] == "breaking_removed"
    assert site["definition"]["change"] == "removed"
    assert site["definition"]["new_parameters"] is None
    assert site["name_uniqueness"] == "unique"
    assert site["site_hit"]
    assert "removed_callable_review" in site["site_hit_detectors"]


def test_removed_function_behind_module_getattr_is_shimmed(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def old_helper(x):\n    return x\n",
            "pkg/use.py": "from pkg.core import old_helper\n\n\ndef show():\n    return old_helper(2)\n",
        },
        {
            "pkg/core.py": (
                "def _old_helper(x):\n    return x\n\n\n"
                "def __getattr__(name):\n    if name == 'old_helper':\n        return _old_helper\n"
                "    raise AttributeError(name)\n"
            ),
            "pkg/use.py": "def show():\n    return 2\n",
        },
    )
    [site] = _sites(report)
    assert site["binding"] == "removed_shimmed"
    assert site["definition"]["removed_behind_module_getattr"] is True


def test_leaf_name_defined_twice_is_ambiguous(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/other.py": "class Widget:\n    def render_page(self, data):\n        return data\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1)\n",
        },
        {
            "pkg/core.py": "def render_page(data, width):\n    return data * width\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1, 80)\n",
        },
    )
    [site] = _sites(report)
    assert site["name_uniqueness"] == "ambiguous"
    assert site["uniqueness_reason"] == "multiple_definitions"
    assert site["definitions_of_name_in_parent"] == 2
    summary = build_and_score.summarize([report])
    assert summary["pooled"]["strata"]["breaking x ambiguous"]["sites"] == 1
    assert summary["pooled"]["strata"]["breaking x unique"]["sites"] == 0


def test_builtin_method_name_is_ambiguous_even_when_defined_once(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "class Store:\n    def get(self, key):\n        return key\n",
            "pkg/use.py": "def show(store):\n    return store.get(1)\n",
        },
        {
            "pkg/core.py": "class Store:\n    def get(self, key, default):\n        return key\n",
            "pkg/use.py": "def show(store):\n    return store.get(1, None)\n",
        },
    )
    [site] = _sites(report)
    assert site["name_uniqueness"] == "ambiguous"
    assert site["uniqueness_reason"] == "builtin_collision"


def test_method_called_through_attribute_drops_self(tmp_path):
    parent_core = "class Renderer:\n    def draw_frame(self, data):\n        return data\n"
    child_core = "class Renderer:\n    def draw_frame(self, data, scale):\n        return data * scale\n"
    report = _history(
        tmp_path,
        {
            "pkg/core.py": parent_core,
            "pkg/use.py": "def show(renderer):\n    return renderer.draw_frame(1)\n",
        },
        {
            "pkg/core.py": child_core,
            "pkg/use.py": "def show(renderer):\n    return renderer.draw_frame(1, 2)\n",
        },
    )
    [site] = _sites(report)
    assert site["call_form"] == "attribute"
    assert site["definition"]["is_method"] is True
    assert site["definition"]["qualname"] == "Renderer.draw_frame"
    # With self dropped, (data, scale) does not bind one positional argument.
    assert site["binding"] == "breaking"


def test_unbound_call_through_the_class_keeps_self(tmp_path):
    parent_core = "class Renderer:\n    def draw_frame(self, data):\n        return data\n"
    child_core = "class Renderer:\n    def draw_frame(self, data, scale=1):\n        return data * scale\n"
    use = "from pkg.core import Renderer\n\nclass Fast(Renderer):\n    def show(self, other):\n        {call}\n"
    report = _history(
        tmp_path,
        {"pkg/core.py": parent_core, "pkg/use.py": use.format(call="return Renderer.draw_frame(self, 1)")},
        {"pkg/core.py": child_core, "pkg/use.py": use.format(call="return Renderer.draw_frame(self, 1, 2)")},
    )
    [site] = _sites(report)
    # The instance is passed explicitly, so (self, data, scale=1) binds two positional arguments.
    assert site["binding"] == "compatible"


def test_bound_call_that_passes_self_as_an_argument_still_drops_self(tmp_path):
    parent_core = "class Registry:\n    def register(self, item):\n        return item\n"
    child_core = "class Registry:\n    def register(self, item, name):\n        return item, name\n"
    use = "class Plugin:\n    def attach(self, registry):\n        {call}\n"
    report = _history(
        tmp_path,
        {"pkg/core.py": parent_core, "pkg/use.py": use.format(call="return registry.register(self)")},
        {"pkg/core.py": child_core, "pkg/use.py": use.format(call="return registry.register(self, 'p')")},
    )
    [site] = _sites(report)
    # registry.register(self) binds self to item; the new required name is missing.
    assert site["binding"] == "breaking"


def test_method_call_still_compatible_after_dropping_self(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "class Renderer:\n    def draw_frame(self, data):\n        return data\n",
            "pkg/use.py": "def show(renderer):\n    return renderer.draw_frame(1)\n",
        },
        {
            "pkg/core.py": "class Renderer:\n    def draw_frame(self, data, scale=1):\n        return data * scale\n",
            "pkg/use.py": "def show(renderer):\n    return renderer.draw_frame(1, scale=2)\n",
        },
    )
    [site] = _sites(report)
    # Without dropping self, one positional argument would fail to bind (self, data, scale=...).
    assert site["binding"] == "compatible"


def test_other_changed_file_without_a_call_produces_no_mutation(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1)\n",
            "pkg/misc.py": "def unrelated():\n    return 1\n",
        },
        {
            "pkg/core.py": "def render_page(data, width):\n    return data * width\n",
            "pkg/misc.py": "def unrelated():\n    return 2\n",
        },
    )
    assert report["mutations"] == []
    assert report["counts"]["changed_definitions_signature"] == 1


def test_call_on_unchanged_line_is_not_ground_truth(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/use.py": "def show(render_page):\n    a = render_page(1)\n    return a\n",
        },
        {
            "pkg/core.py": "def render_page(data, width):\n    return data * width\n",
            "pkg/use.py": "def show(render_page):\n    a = render_page(1)\n    return a + 1\n",
        },
    )
    assert report["mutations"] == []


def test_unpacked_call_is_unknown_and_test_files_are_ignored(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show(args):\n    return render_page(*args)\n",
            "tests/test_use.py": "from pkg.core import render_page\n\n\ndef test_it():\n    render_page(1)\n",
        },
        {
            "pkg/core.py": "def render_page(data, width):\n    return data * width\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show(args):\n    return render_page(*args, 80)\n",
            "tests/test_use.py": "from pkg.core import render_page\n\n\ndef test_it():\n    render_page(1, 2)\n",
        },
    )
    [mutation] = report["mutations"]
    assert mutation["stale_path"] == "pkg/use.py"
    [site] = mutation["sites"]
    assert site["binding"] == "unknown"


def test_insertion_inside_a_multiline_call_counts_as_an_edit(tmp_path):
    report = _history(
        tmp_path,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(\n        1,\n    )\n",
        },
        {
            "pkg/core.py": "def render_page(data, *, width):\n    return data * width\n",
            "pkg/use.py": (
                "from pkg.core import render_page\n\n\ndef show():\n    return render_page(\n        1,\n"
                "        width=80,\n    )\n"
            ),
        },
    )
    [site] = _sites(report)
    assert site["binding"] == "breaking"
    assert site["definition"]["new_parameters"] == "(data, *, width)"


def test_corpus_is_self_describing_and_cli_writes_outputs(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    _commit(
        repo,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1)\n",
        },
        "parent",
    )
    _commit(
        repo,
        {
            "pkg/core.py": "def render_page(data, width):\n    return data * width\n",
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show():\n    return render_page(1, 80)\n",
        },
        "child",
    )
    out = tmp_path / "out"
    assert build_and_score.main(["--repo", str(repo), "--out", str(out), "--limit", "5"]) == 0
    corpus = json.loads((out / "repo" / "corpus.json").read_text(encoding="utf-8"))
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    [mutation] = corpus["mutations"]
    assert corpus["materialization"]
    blob = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-p", mutation["stale_blob"]], capture_output=True, check=True
    ).stdout.decode()
    assert "render_page(1)\n" in blob
    assert len(mutation["commit"]) == 40 and len(mutation["parent"]) == 40
    assert summary["per_repository"]["repo"]["sites_scored"] == 1
    assert "breaking x unique" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("params", "rendered"),
    [
        ("a, b=1, /, c=2, *args, d, e=3, **kw", "(a, b=..., /, c=..., *args, d, e=..., **kw)"),
        ("*, key", "(*, key)"),
        ("", "()"),
    ],
)
def test_parameter_rendering(params, rendered):
    import ast

    node = ast.parse(f"def f({params}): pass").body[0]
    assert build_and_score.render_parameters(build_and_score.parameters(node.args)) == rendered
