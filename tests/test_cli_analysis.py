"""Exercise the command with real repositories and subprocess exit status."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _check(root, *args):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-B", "-m", "mipdelta.cli", "--root", str(root), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "provider.py").write_text("def encode(value):\n    return value\n", encoding="utf-8")
    (root / "pkg" / "caller.py").write_text(
        "from pkg.provider import encode as convert\n\nconvert(1)\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "baseline")
    return root


def test_git_check_in_package_keeps_repository_paths_and_imports(repo):
    (repo / "pkg" / "provider.py").write_text(
        "def encode(value, *, required):\n    return value + required\n", encoding="utf-8"
    )
    # An untracked source outside the requested scope must not enter the analysis.
    (repo / "outside.py").write_text("def broken(:\n", encoding="utf-8")
    result = _check(repo / "pkg", "--check-git", "--json", "--fail-on-findings")
    assert result.returncode == 1, result.stderr
    data = json.loads(result.stdout)
    assert data["changed_paths"] == ["pkg/provider.py"]
    assert [(f["detector"], f["path"], f["start_line"]) for f in data["findings"]] == [
        ("required_keyword_missing", "pkg/caller.py", 3),
    ]


def test_git_check_rejects_invalid_baseline_even_when_clean(repo):
    result = _check(repo, "--check-git", "--base-ref", "not-a-revision", "--fail-on-findings", "--json")
    assert result.returncode == 2
    data = json.loads(result.stdout)
    assert "not-a-revision" in data["error"]
    assert "findings" not in data


def test_git_check_rejects_non_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent.resolve()))
    result = _check(tmp_path, "--check-git", "--fail-on-findings")
    assert result.returncode == 2
    assert "Git" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("path", ["pkg/provider.py", "pkg/provider.pyi", "pkg/caller.py"])
def test_git_check_reports_invalid_source_instead_of_an_empty_result(repo, path):
    (repo / path).write_text("def broken(:\n", encoding="utf-8")
    result = _check(repo, "--check-git")
    assert result.returncode == 2
    assert path in result.stderr and "line 1" in result.stderr
    assert result.stdout == ""


def test_git_check_rejects_invalid_baseline_source(repo):
    path = repo / "pkg" / "provider.py"
    path.write_text("def broken(:\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "unparseable baseline")
    path.write_text("def encode(value):\n    return value\n", encoding="utf-8")
    result = _check(repo, "--check-git")
    assert result.returncode == 2
    assert "pkg/provider.py" in result.stderr and "line 1" in result.stderr


@pytest.mark.parametrize("source", ["def encode(:\n", "def encode(x, x): return x\n"])
def test_candidate_syntax_error_reports_the_logical_path(repo, tmp_path, source):
    candidate = tmp_path / "candidate.txt"
    candidate.write_text(source, encoding="utf-8")
    result = _check(repo, "--write-path", "pkg/provider.py", "--candidate-file", str(candidate), "--json")
    assert result.returncode == 2
    assert "pkg/provider.py" in json.loads(result.stdout)["error"]
    assert (repo / "pkg" / "provider.py").read_text(encoding="utf-8").startswith("def encode(value)")


def test_clean_repository_is_still_silent(repo):
    result = _check(repo, "--check-git", "--fail-on-findings")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_git_check_excludes_untracked_ignored_python_sources(repo):
    (repo / ".gitignore").write_text("scratch.py\n", encoding="utf-8")
    (repo / "scratch.py").write_text("def broken(:\n", encoding="utf-8")
    (repo / "pkg" / "provider.py").write_text(
        "def encode(value, policy): return policy(value)\n", encoding="utf-8"
    )
    result = _check(repo, "--check-git", "--fail-on-findings")
    assert result.returncode == 1, result.stderr
    assert "pkg/caller.py:3 call_signature_review[static] encode" in result.stdout


@pytest.mark.parametrize("rename", [False, True], ids=["deleted", "renamed"])
def test_git_check_reports_call_to_removed_file(repo, rename):
    provider = repo / "pkg" / "provider.py"
    if rename:
        provider.rename(provider.with_name("replacement.py"))
    else:
        provider.unlink()
    result = _check(repo, "--check-git", "--json", "--fail-on-findings")
    assert result.returncode == 1, result.stderr
    data = json.loads(result.stdout)
    expected_paths = ["pkg/provider.py", "pkg/replacement.py"] if rename else ["pkg/provider.py"]
    assert data["changed_paths"] == expected_paths
    assert [(f["detector"], f["path"], f["start_line"]) for f in data["findings"]] == [
        ("removed_callable_review", "pkg/caller.py", 3),
    ]


def test_candidate_check_reports_missing_required_positional(repo, tmp_path):
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("def encode(value, policy):\n    return policy(value)\n", encoding="utf-8")
    result = _check(repo, "--write-path", "pkg/provider.py", "--candidate-file", str(candidate),
                    "--fail-on-findings")
    assert result.returncode == 1, result.stderr
    assert "pkg/caller.py:3 call_signature_review[static] encode" in result.stdout
    assert "missing a required argument: 'policy'" in result.stdout


_UNPARSABLE = {
    "legacy/py2_script.py": 'print "legacy"\n',
    "templates/skeleton.py": "def {{ name }}():\n    pass\n",
    "templates/skeleton.pyi": "def {{ name }}() -> None: ...\n",
}


@pytest.fixture
def repo_with_unparsable_files(repo):
    for name, text in _UNPARSABLE.items():
        (repo / name).parent.mkdir(exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "unparsable bystanders")
    return repo


def test_git_check_skips_unchanged_unparsable_files(repo_with_unparsable_files):
    repo = repo_with_unparsable_files
    (repo / "pkg" / "provider.py").write_text(
        "def encode(value, policy): return policy(value)\n", encoding="utf-8"
    )
    result = _check(repo, "--check-git", "--fail-on-findings")
    assert result.returncode == 1, result.stderr
    assert "pkg/caller.py:3 call_signature_review[static] encode" in result.stdout
    assert "skipped 3 unparsable unchanged file(s)" in result.stderr
    assert "legacy/py2_script.py" in result.stderr

    data = json.loads(_check(repo, "--check-git", "--json").stdout)
    assert data["skipped_unparsed_paths"] == sorted(_UNPARSABLE)
    assert [f["path"] for f in data["findings"]] == ["pkg/caller.py"]


def test_candidate_check_skips_unparsable_bystanders(repo_with_unparsable_files, tmp_path):
    repo = repo_with_unparsable_files
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("def encode(value, policy):\n    return policy(value)\n", encoding="utf-8")
    result = _check(repo, "--write-path", "pkg/provider.py", "--candidate-file", str(candidate),
                    "--fail-on-findings")
    assert result.returncode == 1, result.stderr
    assert "pkg/caller.py:3 call_signature_review[static] encode" in result.stdout
    assert "skipped 3 unparsable unchanged file(s)" in result.stderr


def test_git_check_still_rejects_an_edit_to_an_unparsable_file(repo_with_unparsable_files):
    repo = repo_with_unparsable_files
    (repo / "legacy" / "py2_script.py").write_text('print "edited"\n', encoding="utf-8")
    result = _check(repo, "--check-git")
    assert result.returncode == 2
    assert "legacy/py2_script.py" in result.stderr


def test_clean_repository_with_unparsable_files_is_silent(repo_with_unparsable_files):
    result = _check(repo_with_unparsable_files, "--check-git", "--fail-on-findings")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_git_check_excludes_vendored_paths_but_keeps_top_level_packages(tmp_path):
    root = tmp_path / "repo"
    for rel, body in {
        "mylib/__init__.py": "",
        "mylib/packages/chardet/prober.py": "class Base:\n    def reset(self, x):\n        return x\n\nclass Sub(Base):\n    def reset(self, x):\n        return x\n",
        "vendor/lib.py": "def fn(a):\n    return a\n",
        "packages/app/service.py": "def handle(req):\n    return req\n",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base")

    # Mutate vendored code AND top-level monorepo `packages/app/service.py`
    (root / "mylib/packages/chardet/prober.py").write_text(
        "class Base:\n    def reset(self, x, filter_mode=None):\n        return (x, filter_mode)\n\n"
        "class Sub(Base):\n    def reset(self, x):\n        return x\n",
        encoding="utf-8",
    )
    (root / "vendor/lib.py").write_text("def fn(a, unused_flag=1):\n    return a\n", encoding="utf-8")
    (root / "packages/app/service.py").write_text("def handle(req, dry_run=False):\n    return req\n", encoding="utf-8")

    result = _check(root, "--check-git", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["changed_paths"] == ["packages/app/service.py"]
    assert [(f["detector"], f["path"]) for f in data["findings"]] == [
        ("parameter_sink_review", "packages/app/service.py"),
    ]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring requires Python 3.12 or later")
def test_run_pytest_uses_cap1_and_prioritizes_target_src_over_tool_src(tmp_path):
    root = tmp_path / "src_repo"
    (root / "src/targetpkg").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src/targetpkg/__init__.py").write_text("", encoding="utf-8")
    (root / "src/targetpkg/engine.py").write_text(
        "class Backend:\n    def execute(self, cmd):\n        return cmd\n\n"
        "def dispatch(backend):\n    return backend.execute('ping')\n",
        encoding="utf-8",
    )
    (root / "tests/test_engine.py").write_text(
        "from targetpkg.engine import Backend, dispatch\n\n"
        "def test_dispatch():\n    assert dispatch(Backend()) == 'ping'\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base")

    # Add optional parameter with default so pytest passes while Shard inspects cap1
    (root / "src/targetpkg/engine.py").write_text(
        "class Backend:\n    def execute(self, cmd):\n        return cmd\n\n"
        "def make_wrapper(backend, *, retry=False):\n    return backend.execute('ping')\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "wrapper")
    (root / "src/targetpkg/engine.py").write_text(
        "class Backend:\n    def execute(self, cmd, *, retry=False):\n        return (cmd, retry)\n\n"
        "def make_wrapper(backend, *, retry=False):\n    return backend.execute('ping')\n",
        encoding="utf-8",
    )
    (root / "tests/test_engine.py").write_text(
        "from targetpkg.engine import Backend, make_wrapper\n\n"
        "def test_wrapper():\n    assert make_wrapper(Backend()) == ('ping', False)\n",
        encoding="utf-8",
    )
    result = _check(root, "--run-pytest", "--pytest-args=-q", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout[result.stdout.index("{"):])
    assert any(
        f["detector"] == "delegating_wrapper_review" and f["resolution_source"] == "observed_runtime"
        for f in data["findings"]
    )
    shards = list((root / ".mipdelta" / "runtime").glob("edges-*.json"))
    assert shards
    shard_payload = json.loads(shards[0].read_text(encoding="utf-8"))
    assert shard_payload["max_events_per_site"] == 1
