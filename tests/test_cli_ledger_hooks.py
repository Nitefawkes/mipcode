"""Persisted obligations and agent hook adapters for the mip-delta command."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
LIB = "def load(p):\n    return p, 1\n"
LIB_THREE = "def load(p):\n    return p, 1, 2\n"
APP = "from lib import load\n\ndef use():\n    a, b = load('x')\n    return a\n"
APP_FIXED = "from lib import load\n\ndef use():\n    a, b, c = load('x')\n    return a\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "lib.py").write_text(LIB, encoding="utf-8")
    (root / "app.py").write_text(APP, encoding="utf-8")
    (root / "notes.md").write_text("notes\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _run(repo: Path, *args: str, stdin: str = "", env: dict[str, str] | None = None):
    full_env = {**os.environ, "PYTHONPATH": SRC, **(env or {})}
    full_env.pop("CLAUDE_PROJECT_DIR", None)
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "mipdelta.cli", *args],
        cwd=repo, input=stdin, capture_output=True, text=True, encoding="utf-8", env=full_env,
    )


def _check(repo: Path, *args: str):
    return _run(repo, "--check-git", "--root", str(repo), "--ledger", str(repo / "ledger.json"), *args)


def test_ledger_reports_an_obligation_once_then_its_discharge(repo):
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    first = _check(repo)
    assert first.returncode == 0
    assert "app.py:4 return_contract_review" in first.stdout

    again = _check(repo)
    assert again.stdout == ""

    listed = _check(repo, "--open")
    assert listed.stdout.startswith("[mip-delta] 1 open: app.py:4 return_contract_review[static] load #")

    (repo / "app.py").write_text(APP_FIXED, encoding="utf-8")
    data = json.loads(_check(repo, "--json").stdout)
    assert [row["path"] for row in data["discharged"]] == ["app.py"]
    assert data["opened"] == [] and data["open"] == []
    assert _check(repo, "--open").stdout == ""


def test_waived_obligation_no_longer_fails_the_check(repo):
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    failing = _check(repo, "--fail-on-findings", "--json")
    assert failing.returncode == 1
    (row,) = json.loads(failing.stdout)["open"]

    ledger = str(repo / "ledger.json")
    unknown = _run(repo, "--waive", "ffffffff", "--ledger", ledger)
    assert unknown.returncode == 2 and "ffffffff" in unknown.stderr
    waived = _run(repo, "--waive", row["id"], "--reason", "caller is removed next", "--ledger", ledger)
    assert waived.returncode == 0

    passing = _check(repo, "--fail-on-findings", "--json")
    assert passing.returncode == 0
    data = json.loads(passing.stdout)
    assert data["open"] == [] and [item["id"] for item in data["waived"]] == [row["id"]]


def _post_edit(repo: Path, file_path: Path, session: str = "s1"):
    event = {
        "session_id": session,
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file_path)},
        "cwd": str(repo),
    }
    return _run(repo, "--hook", "claude-post-edit", stdin=json.dumps(event), env={"CLAUDE_PROJECT_DIR": str(repo)})


def _stop(repo: Path, active: bool = False, session: str = "s1"):
    event = {"session_id": session, "hook_event_name": "Stop", "stop_hook_active": active, "cwd": str(repo)}
    return _run(repo, "--hook", "claude-stop", stdin=json.dumps(event), env={"CLAUDE_PROJECT_DIR": str(repo)})


def test_post_edit_hook_is_silent_for_local_and_non_python_edits(repo):
    (repo / "notes.md").write_text("changed\n", encoding="utf-8")
    assert _post_edit(repo, repo / "notes.md").stdout == ""

    (repo / "lib.py").write_text("def load(p):\n    value = p\n    return value, 1\n", encoding="utf-8")
    local = _post_edit(repo, repo / "lib.py")
    assert local.returncode == 0 and local.stdout == "" and local.stderr == ""
    assert not (repo / ".mipdelta").exists()
    assert _stop(repo).stdout == ""


def test_post_edit_hook_adds_context_once_and_stop_hook_blocks_once(repo):
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    first = json.loads(_post_edit(repo, repo / "lib.py").stdout)
    assert first["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "app.py:4 return_contract_review" in first["hookSpecificOutput"]["additionalContext"]
    assert (repo / ".mipdelta" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    status = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout
    assert ".mipdelta" not in status

    assert _post_edit(repo, repo / "lib.py").stdout == ""

    blocked = json.loads(_stop(repo).stdout)
    assert blocked["decision"] == "block"
    assert "1 open: app.py:4 return_contract_review" in blocked["reason"]

    assert _stop(repo, active=True).stdout == ""
    assert _stop(repo).stdout == ""  # already listed once; a second stop is not blocked


def test_stop_hook_allows_completion_after_the_site_is_fixed(repo):
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    _post_edit(repo, repo / "lib.py")
    (repo / "app.py").write_text(APP_FIXED, encoding="utf-8")
    assert _post_edit(repo, repo / "app.py").stdout == ""
    assert _stop(repo).stdout == ""


def test_hook_keeps_the_session_baseline_after_a_commit(repo):
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    _post_edit(repo, repo / "lib.py")
    _git(repo, "commit", "-q", "-am", "change return shape")

    blocked = json.loads(_stop(repo).stdout)
    assert "app.py:4 return_contract_review" in blocked["reason"]
    # A different session starts from the new commit and has nothing to report.
    assert _stop(repo, session="s2").stdout == ""


def test_hook_stays_quiet_on_unusable_input(repo, tmp_path):
    assert _run(repo, "--hook", "claude-post-edit", stdin="not json").returncode == 0
    (repo / "lib.py").write_text("def load(p:\n", encoding="utf-8")
    broken = _post_edit(repo, repo / "lib.py")
    assert broken.returncode == 0 and broken.stdout == ""
    outside = tmp_path / "plain"
    outside.mkdir()
    event = json.dumps({"session_id": "s", "tool_input": {"file_path": str(outside / "a.py")}, "cwd": str(outside)})
    not_a_repo = _run(outside, "--hook", "claude-post-edit", stdin=event, env={"CLAUDE_PROJECT_DIR": str(outside)})
    assert not_a_repo.returncode == 0 and not_a_repo.stdout == ""


def test_merge_base_check_reports_a_caller_added_on_the_other_branch(repo):
    """Two branches merge without a textual conflict, yet one adds a caller of the old contract."""
    _git(repo, "checkout", "-q", "-b", "change-contract")
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    (repo / "app.py").write_text(APP_FIXED, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "return a third value and update the caller")
    assert _run(repo, "--check-git", "--root", str(repo), "--base-ref", "HEAD~1").stdout == ""

    _git(repo, "checkout", "-q", "-")
    (repo / "report.py").write_text(
        "from lib import load\n\ndef summarize():\n    name, count = load('x')\n    return name\n", encoding="utf-8"
    )
    _git(repo, "add", "report.py")
    _git(repo, "commit", "-q", "-m", "add a report that unpacks two values")
    base = subprocess.run(
        ["git", "merge-base", "HEAD", "change-contract"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _git(repo, "merge", "-q", "--no-edit", "change-contract")

    merged = _run(repo, "--check-git", "--root", str(repo), "--base-ref", base)
    assert merged.returncode == 0
    assert "report.py:4 return_contract_review" in merged.stdout
    assert "app.py" not in merged.stdout
