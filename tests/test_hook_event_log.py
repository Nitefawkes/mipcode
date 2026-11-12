"""Append-only event log kept next to each obligation ledger, and its summarizer."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
SUMMARIZER = str(ROOT / "experiments" / "dogfood" / "summarize_events.py")
LIB = "def load(p):\n    return p, 1\n"
LIB_THREE = "def load(p):\n    return p, 1, 2\n"
APP = "from lib import load\n\ndef use():\n    a, b = load('x')\n    return a\n"
APP_FIXED = "from lib import load\n\ndef use():\n    a, b, c = load('x')\n    return a\n"
APP_LOCAL = "from lib import load\n\ndef use():\n    a, b = load('x')\n    print(a)\n    return a\n"
SITE_KEYS = {"id", "detector", "symbol", "path", "line", "provenance"}


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
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _run(repo: Path, *args: str, stdin: str = "", env: dict[str, str] | None = None):
    full_env = {**os.environ, "PYTHONPATH": SRC}
    full_env.pop("CLAUDE_PROJECT_DIR", None)
    full_env.pop("MIPDELTA_HOOK_DEBUG", None)
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "mipdelta.cli", *args],
        cwd=repo, input=stdin, capture_output=True, text=True, encoding="utf-8", env=full_env,
    )


def _post_edit(repo: Path, file_path: Path, session: str = "s1", env: dict[str, str] | None = None):
    event = {
        "session_id": session,
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file_path)},
        "cwd": str(repo),
    }
    return _run(
        repo, "--hook", "claude-post-edit", stdin=json.dumps(event),
        env={"CLAUDE_PROJECT_DIR": str(repo), **(env or {})},
    )


def _stop(repo: Path, session: str = "s1"):
    event = {"session_id": session, "hook_event_name": "Stop", "stop_hook_active": False, "cwd": str(repo)}
    return _run(repo, "--hook", "claude-stop", stdin=json.dumps(event), env={"CLAUDE_PROJECT_DIR": str(repo)})


def _events(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        assert row["ts"].endswith("Z") and "evidence" not in row
    return rows


def _hook_log(repo: Path, session: str = "s1") -> Path:
    return repo / ".mipdelta" / f"ledger-{session}.events.jsonl"


def _summarize(*targets: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, SUMMARIZER, *map(str, targets), "--json"], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_site_edit_opens_then_discharges_with_the_edited_path(repo):
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    _post_edit(repo, repo / "lib.py")
    (repo / "app.py").write_text(APP_FIXED, encoding="utf-8")
    _post_edit(repo, repo / "app.py")

    rows = _events(_hook_log(repo))
    assert [row["event"] for row in rows] == ["opened", "run", "discharged", "run"]
    opened, first_run, discharged, second_run = rows
    assert SITE_KEYS <= set(opened) and SITE_KEYS <= set(discharged)
    assert (opened["path"], opened["line"], opened["symbol"]) == ("app.py", 4, "load")
    assert opened["detector"] == "return_contract_review" and opened["provenance"] == "static_ast"
    assert opened["edited_path"] == "lib.py" and discharged["edited_path"] == "app.py"
    assert discharged["id"] == opened["id"]
    assert all(row["session"] == "s1" and len(row["base_commit"]) == 40 for row in rows)
    assert opened["invocation"] == first_run["invocation"] != second_run["invocation"]
    assert first_run["source"] == "claude-post-edit" and first_run["workspace_parsed"] is True
    assert (first_run["opened"], first_run["discharged"], first_run["open"]) == (1, 0, 1)
    assert (second_run["opened"], second_run["discharged"], second_run["open"]) == (0, 1, 0)
    assert first_run["changed_paths"] == 1 and second_run["changed_paths"] == 2
    assert isinstance(first_run["wall_ms"], float)

    summary = _summarize(repo)["pooled"]
    assert summary["discharged"] == {"total": 1, "site_file_edited": 1, "other_file_edited": 0, "unknown": 0}
    assert summary["edits_open_to_discharge"] == {"count": 1, "median": 1, "max": 1}
    assert summary["still_open_at_last_event"] == 0


def test_reverting_the_contract_change_discharges_from_another_file(repo):
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    _post_edit(repo, repo / "lib.py")
    (repo / "lib.py").write_text(LIB, encoding="utf-8")
    _post_edit(repo, repo / "lib.py")
    (repo / "app.py").write_text(APP_LOCAL, encoding="utf-8")
    assert _post_edit(repo, repo / "app.py").stdout == ""

    rows = _events(_hook_log(repo))
    assert [row["event"] for row in rows] == ["opened", "run", "discharged", "run", "run"]
    discharged = rows[2]
    assert discharged["path"] == "app.py" and discharged["edited_path"] == "lib.py"
    revert_run, local_run = rows[3], rows[4]
    # Nothing changed after the revert; then only app.py changed, without a cross-file contract.
    assert (revert_run["changed_paths"], revert_run["workspace_parsed"]) == (0, False)
    assert (local_run["changed_paths"], local_run["workspace_parsed"]) == (1, False)
    assert rows[1]["workspace_parsed"] is True

    summary = _summarize(_hook_log(repo))["pooled"]
    assert summary["discharged"]["other_file_edited"] == 1
    assert summary["runs_by_source"] == {"claude-post-edit": 3}
    assert summary["latency_ms"]["workspace_parsed"]["runs"] == 1
    assert summary["latency_ms"]["changed_files_only"]["runs"] == 2


def test_check_git_and_waive_write_events_next_to_the_ledger(repo):
    ledger = repo / "state" / "ledger.json"
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    checked = _run(repo, "--check-git", "--root", str(repo), "--ledger", str(ledger), "--json")
    (site,) = json.loads(checked.stdout)["open"]
    waived = _run(repo, "--waive", site["id"], "--reason", "caller goes away", "--ledger", str(ledger))
    assert waived.returncode == 0

    rows = _events(repo / "state" / "ledger.events.jsonl")
    assert [row["event"] for row in rows] == ["opened", "run", "waived"]
    assert "edited_path" not in rows[0] and rows[0]["session"] is None
    assert rows[1]["source"] == "check-git" and rows[1]["workspace_parsed"] is True
    assert rows[2]["id"] == site["id"] and rows[2]["reason"] == "caller goes away"
    assert rows[2]["base_commit"] == rows[0]["base_commit"]

    summary = _summarize(repo)["pooled"]
    assert (summary["opened"], summary["waived"], summary["still_open_at_last_event"]) == (1, 1, 0)
    assert summary["by_detector"]["return_contract_review"] == {"opened": 1, "waived": 1}


def test_stop_block_lists_the_ids_and_a_stop_discharge_is_unknown(repo):
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    _post_edit(repo, repo / "lib.py")
    assert json.loads(_stop(repo).stdout)["decision"] == "block"
    (repo / "app.py").write_text(APP_FIXED, encoding="utf-8")
    assert _stop(repo).stdout == ""

    rows = _events(_hook_log(repo))
    assert [row["event"] for row in rows] == ["opened", "run", "stop_blocked", "run", "discharged", "run"]
    opened, blocked = rows[0], rows[2]
    assert blocked["ids"] == [opened["id"]] and blocked["new_ids"] == [opened["id"]]
    assert "edited_path" not in blocked and rows[3]["source"] == "claude-stop"

    summary = _summarize(repo)["pooled"]
    assert summary["stop_blocks"] == 1
    assert summary["discharged"]["unknown"] == 1
    assert summary["runs_by_source"] == {"claude-post-edit": 1, "claude-stop": 2}


def test_quiet_session_without_a_ledger_leaves_no_state(repo):
    (repo / "app.py").write_text(APP_LOCAL, encoding="utf-8")
    assert _post_edit(repo, repo / "app.py").stdout == ""
    assert _stop(repo).stdout == ""
    assert not (repo / ".mipdelta").exists()


def test_quiet_runs_are_recorded_when_asked(repo):
    (repo / "app.py").write_text(APP_LOCAL, encoding="utf-8")
    result = _post_edit(repo, repo / "app.py", env={"MIPDELTA_LOG_ALL_RUNS": "1"})
    assert result.stdout == ""

    rows = _events(repo / ".mipdelta" / "ledger-s1.events.jsonl")
    assert [row["event"] for row in rows] == ["run"]
    assert rows[0]["opened"] == 0 and rows[0]["workspace_parsed"] is False
    assert rows[0]["edited_path"] == "app.py"
    assert not (repo / ".mipdelta" / "ledger-s1.json").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert ".mipdelta" not in status


def test_unwritable_log_never_changes_the_hook_or_check_outcome(repo):
    (repo / ".mipdelta").mkdir()
    _hook_log(repo).mkdir()  # a directory where the log file should be
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    hooked = _post_edit(repo, repo / "lib.py")
    assert hooked.returncode == 0 and hooked.stderr == ""
    assert "app.py:4" in json.loads(hooked.stdout)["hookSpecificOutput"]["additionalContext"]

    (repo / "app.py").write_text(APP_FIXED, encoding="utf-8")
    debug = _post_edit(repo, repo / "app.py", env={"MIPDELTA_HOOK_DEBUG": "1"})
    assert debug.returncode == 0 and debug.stdout == "" and "event log" in debug.stderr

    (repo / "app.py").write_text(APP, encoding="utf-8")
    ledger = repo / "ledger.json"
    (repo / "ledger.events.jsonl").mkdir()
    failing = _run(repo, "--check-git", "--root", str(repo), "--ledger", str(ledger), "--fail-on-findings")
    assert failing.returncode == 1 and "app.py:4" in failing.stdout and failing.stderr == ""


def _line(event: str, invocation: str, **fields) -> str:
    return json.dumps({"event": event, "ts": "2026-11-12T00:00:00.000Z", "invocation": invocation, **fields})


def test_summarizer_classifies_a_recorded_log(tmp_path):
    site_a = {"detector": "return_contract_review", "symbol": "load", "path": "a.py", "line": 3, "provenance": "static_ast"}
    site_b = {**site_a, "path": "b.py", "detector": "required_param_review"}
    site_c = {**site_a, "path": "c.py"}
    edit = lambda path: {"session": "s", "base_commit": "c0", "edited_path": path}  # noqa: E731
    stop = {"session": "s", "base_commit": "c0"}
    run = lambda source, ms, parsed: {"source": source, "wall_ms": ms, "workspace_parsed": parsed}  # noqa: E731
    lines = [
        _line("opened", "i1", id="a", **site_a, **edit("lib.py")),
        _line("opened", "i1", id="b", **site_b, **edit("lib.py")),
        _line("opened", "i1", id="c", **site_c, **edit("lib.py")),
        _line("run", "i1", **run("claude-post-edit", 100.0, True), **edit("lib.py")),
        _line("run", "i2", **run("claude-post-edit", 10.0, False), **edit("x.py")),
        _line("discharged", "i3", id="a", **site_a, **edit("a.py")),
        _line("run", "i3", **run("claude-post-edit", 120.0, True), **edit("a.py")),
        _line("stop_blocked", "i4", ids=["b", "c"], new_ids=["b", "c"], **stop),
        _line("run", "i4", **run("claude-stop", 90.0, True), **stop),
        _line("discharged", "i5", id="b", **site_b, **edit("lib.py")),
        _line("run", "i5", **run("claude-post-edit", 20.0, False), **edit("lib.py")),
        "{not json",
    ]
    log = tmp_path / "ledger-s.events.jsonl"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    data = _summarize(log)
    assert list(data["repositories"]) == [str(log)]
    summary = data["pooled"]
    assert summary["sessions"] == 1 and summary["distinct_session_ids"] == 1
    assert summary["runs_by_source"] == {"claude-post-edit": 4, "claude-stop": 1}
    assert summary["opened"] == 3
    assert summary["discharged"] == {"total": 2, "site_file_edited": 1, "other_file_edited": 1, "unknown": 0}
    assert summary["still_open_at_last_event"] == 1
    assert summary["stop_blocks"] == 1
    assert summary["edits_open_to_discharge"] == {"count": 2, "median": 2.5, "max": 3}
    assert summary["by_detector"]["return_contract_review"] == {"discharged": 1, "opened": 2, "still_open": 1}
    assert summary["latency_ms"]["workspace_parsed"] == {"runs": 3, "p50": 100.0, "p95": 120.0, "max": 120.0}
    assert summary["latency_ms"]["changed_files_only"] == {"runs": 2, "p50": 10.0, "p95": 20.0, "max": 20.0}
    assert summary["unreadable_lines"] == 1

    text = subprocess.run(
        [sys.executable, SUMMARIZER, str(log)], capture_output=True, text=True, encoding="utf-8"
    ).stdout
    assert "discharged: 2 (site file edited 1, other file edited 1, unknown 0)" in text
