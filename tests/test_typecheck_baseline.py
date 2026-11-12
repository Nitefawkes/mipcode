"""mypy baseline for the stale-file mutation corpus, on throwaway repositories."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mypy")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "recall_mutation"))

build_and_score = importlib.import_module("build_and_score")
typecheck_baseline = importlib.import_module("typecheck_baseline")


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


def _baseline(tmp_path: Path, parent: dict[str, str], child: dict[str, str | None]) -> tuple[dict, dict]:
    """Commit parent and child, build the corpus with the harness, then run the mypy baseline."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    _commit(repo, {"pkg/__init__.py": "", **parent}, "parent")
    _commit(repo, child, "child")
    corpus_dir = tmp_path / "corpus"
    assert build_and_score.main(["--repo", str(repo), "--out", str(corpus_dir)]) == 0
    out = tmp_path / "typecheck"
    work = tmp_path / "work"
    argv = [
        "--corpus", str(corpus_dir / "repo" / "corpus.json"),
        "--repo", f"repo={repo}",
        "--work", str(work),
        "--out", str(out),
        "--python", sys.executable,
        "--timeout", "300",
    ]
    assert typecheck_baseline.main(argv) == 0
    assert not (work / "repo").exists(), "scratch trees are removed"
    report = json.loads((out / "repo" / "typecheck.json").read_text(encoding="utf-8"))
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    return report, summary


TYPED_CORE_P = "def render_page(data: int) -> int:\n    return data\n"
TYPED_CORE_C = "def render_page(data: int, width: int) -> int:\n    return data * width\n"


def test_breaking_stale_call_in_typed_code_is_reported_at_the_call(tmp_path):
    report, summary = _baseline(
        tmp_path,
        {
            "pkg/core.py": TYPED_CORE_P,
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show() -> int:\n    return render_page(1)\n",
        },
        {
            "pkg/core.py": TYPED_CORE_C,
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show() -> int:\n    return render_page(1, 80)\n",
        },
    )
    [mutation] = report["mutations"]
    assert mutation["tc_status"] == "ok"
    assert mutation["target"] == "pkg"
    assert mutation["errors_commit"] == 0
    [site] = mutation["sites"]
    assert site["index"] == 0
    assert site["binding"] == "breaking" and site["name_uniqueness"] == "unique"
    assert site["site_hit"] and site["file_hit"]  # carried over from the corpus
    assert site["tc_site_hit"] and site["tc_file_hit"]
    assert site["tc_site_hit_codes"] == ["call-arg"]
    assert site["tc_site_hit_names_definition"]
    [error] = site["tc_site_hit_errors"]
    assert error["line"] == 5 and 'Missing positional argument "width"' in error["message"]
    assert mutation["new_errors_elsewhere"] == []

    assert "mypy" in summary["mypy_version"]
    assert "--check-untyped-defs" in summary["command"] and "--follow-imports=silent" not in summary["command"]
    cell = summary["pooled"]["overlap"]["site"]["breaking x unique"]
    assert cell["sites"]["both"] == 1 and cell["sites"]["scored"] == 1
    assert cell["mutations"]["both"] == 1


def test_untyped_callee_is_still_checked_for_arity_but_an_any_receiver_hides_the_call(tmp_path):
    """With --check-untyped-defs mypy checks the body of an unannotated function and still knows an
    unannotated callee's arity, so the plain call is reported. A call through an unannotated
    parameter has an Any receiver: mypy cannot see which function it reaches and stays silent."""
    report, _ = _baseline(
        tmp_path,
        {
            "pkg/core.py": "def render_page(data):\n    return data\n",
            "pkg/use.py": (
                "from pkg.core import render_page\n\n\n"
                "def show():\n    return render_page(1)\n\n\n"
                "def show_page(page):\n    return page.render_page(1)\n"
            ),
        },
        {
            "pkg/core.py": "def render_page(data, width):\n    return data * width\n",
            "pkg/use.py": (
                "from pkg.core import render_page\n\n\n"
                "def show():\n    return render_page(1, 80)\n\n\n"
                "def show_page(page):\n    return page.render_page(1, 80)\n"
            ),
        },
    )
    [mutation] = report["mutations"]
    assert mutation["tc_status"] == "ok"
    direct, through_any = mutation["sites"]
    assert direct["line"] == 5 and through_any["line"] == 9
    assert direct["binding"] == through_any["binding"] == "breaking"
    assert direct["tc_site_hit"] and direct["tc_site_hit_codes"] == ["call-arg"]
    assert not through_any["tc_site_hit"]
    assert through_any["tc_file_hit"]  # the direct call's error is in the same file


def test_errors_already_in_the_commit_tree_are_not_new(tmp_path):
    use_p = (
        "from pkg.core import render_page\n\n"
        'bad: int = "not an int"\n\n\n'
        "def show() -> int:\n    return render_page(1)\n"
    )
    use_c = (
        '"""Shifted down by two lines."""\n\n'
        "from pkg.core import render_page\n\n"
        'bad: int = "not an int"\n\n\n'
        "def show() -> int:\n    return render_page(1, 80)\n"
    )
    other = 'count: int = "zero"\n'
    report, summary = _baseline(
        tmp_path,
        {"pkg/core.py": TYPED_CORE_P, "pkg/use.py": use_p, "pkg/other.py": other},
        {"pkg/core.py": TYPED_CORE_C, "pkg/use.py": use_c},
    )
    [mutation] = report["mutations"]
    assert mutation["tc_status"] == "ok"
    assert mutation["errors_commit"] == 2  # other.py and the assignment in use.py
    assert mutation["errors_mutated"] == 3
    [new] = mutation["new_errors_in_stale_file"]
    assert new["code"] == "call-arg" and new["line"] == 7
    assert mutation["new_errors_elsewhere"] == []
    [site] = mutation["sites"]
    assert site["tc_site_hit"]
    commit_row = report["commits"][0]
    assert commit_row["targets"]["pkg"]["errors_by_code"] == {"assignment": 2}


def test_removed_function_is_reported_at_its_import_not_at_the_call(tmp_path):
    report, summary = _baseline(
        tmp_path,
        {
            "pkg/core.py": TYPED_CORE_P,
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show() -> int:\n    return render_page(1)\n",
        },
        {
            "pkg/core.py": "def render(data: int) -> int:\n    return data\n",
            "pkg/use.py": "from pkg.core import render\n\n\ndef show() -> int:\n    return render(1)\n",
        },
    )
    [mutation] = report["mutations"]
    [site] = mutation["sites"]
    assert site["binding"] == "breaking_removed"
    assert not site["tc_site_hit"]
    assert site["tc_import_hit"] and site["tc_site_or_import_hit"] and site["tc_file_hit"]
    [error] = site["tc_import_hit_errors"]
    assert error["line"] == 1 and error["code"] == "attr-defined"
    cell = summary["pooled"]["overlap"]["site_or_import"]["breaking_removed x unique"]
    assert cell["sites"]["scored"] == 1 and cell["sites"]["mip_delta_only"] + cell["sites"]["neither"] == 0


def test_blocking_error_in_the_commit_tree_excludes_its_mutations(tmp_path):
    broken = "def broken(:\n    pass\n"
    report, summary = _baseline(
        tmp_path,
        {
            "pkg/core.py": TYPED_CORE_P,
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show() -> int:\n    return render_page(1)\n",
            "pkg/broken.py": broken,
        },
        {
            "pkg/core.py": TYPED_CORE_C,
            "pkg/use.py": "from pkg.core import render_page\n\n\ndef show() -> int:\n    return render_page(1, 80)\n",
        },
    )
    [mutation] = report["mutations"]
    assert mutation["tc_status"] == "blocking_error"
    assert mutation["sites"][0]["tc_status"] == "blocking_error"
    assert not mutation["sites"][0]["tc_site_hit"]
    cell = summary["pooled"]["overlap"]["site"]["all"]
    assert cell["sites"]["scored"] == 0
    assert cell["sites"]["excluded"] == {"blocking_error": 1}
    assert cell["mutations"]["excluded"] == {"blocking_error": 1}
    assert summary["pooled"]["mutation_status"] == {"blocking_error": 1}


def test_new_errors_is_a_multiset_difference_that_ignores_line_numbers():
    def error(path, line, message, code="misc"):
        return {"path": path, "line": line, "column": 1, "code": code, "message": message}

    complete = [error("a.py", 10, "x"), error("b.py", 3, "y"), error("a.py", 20, "Name defined on line 4")]
    mutated = [
        error("a.py", 12, "x"),  # shifted, same key: not new
        error("a.py", 30, "x"),  # second instance of the key: new
        error("b.py", 3, "y"),
        error("a.py", 22, "Name defined on line 6"),  # line reference inside the message is ignored
        error("c.py", 1, "z"),
    ]
    c_to_m = {10: 12, 20: 22}
    fresh = typecheck_baseline.new_errors(mutated, complete, "a.py", c_to_m)
    assert [(e["path"], e["line"]) for e in fresh] == [("a.py", 30), ("c.py", 1)]


def test_package_target_walks_up_while_init_exists(tmp_path):
    (tmp_path / "src" / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "pkg" / "sub" / "__init__.py").write_text("")
    (tmp_path / "scripts").mkdir()
    assert typecheck_baseline.package_target(tmp_path, "src/pkg/sub/mod.py") == "src/pkg"
    assert typecheck_baseline.package_target(tmp_path, "scripts/tool.py") == "scripts/tool.py"
