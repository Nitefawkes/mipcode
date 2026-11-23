"""Tests for LayerNorm-trap ranking/clipping fixes, ObligationLedger oscillation detection,
and canonical actual_delta ground-truth summaries.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mipdelta import cli
from mipdelta.delta import format_advisory, rank_findings, summarize_actual_delta
from mipdelta.facts import parse_file
from mipdelta.ledger import ObligationLedger, format_open_obligations, format_thrash_advisory
from mipdelta.model import Finding
from mipdelta.review import MAX_DIFF_CHARS, MAX_SNIPPETS, ReviewResult, format_review_prompt
from mipdelta.session import DeltaSession

SRC = str(Path(__file__).resolve().parents[1] / "src")
LIB = "def load(p):\n    return p, 1\n"
LIB_THREE = "def load(p):\n    return p, 1, 2\n"
APP = "from lib import load\n\ndef use():\n    a, b = load('x')\n    return a\n"
APP_FIXED = "from lib import load\n\ndef use():\n    a, b, c = load('x')\n    return a\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _run_cli(repo: Path, *args: str, stdin: str = "", env: dict[str, str] | None = None):
    full_env = {**os.environ, "PYTHONPATH": SRC}
    full_env.pop("CLAUDE_PROJECT_DIR", None)
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "mipdelta.cli", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
    )


# ---------------------------------------------------------------------------
# 1. LayerNorm-trap ranking, advisory clipping, and Stage-2 review truncation
# ---------------------------------------------------------------------------


def test_rank_findings_prioritizes_production_over_support_and_test_paths():
    findings = [
        Finding("delegating_wrapper_review", "bench", "benchmarks/a_bench.py", 10, 10, "candidate", ("e",), "static_ast"),
        Finding("delegating_wrapper_review", "demo", "examples/a_demo.py", 10, 10, "candidate", ("e",), "static_ast"),
        Finding("delegating_wrapper_review", "helper", "test/a_helper.py", 10, 10, "candidate", ("e",), "static_ast"),
        Finding("delegating_wrapper_review", "fix", "conftest.py", 10, 10, "candidate", ("e",), "static_ast"),
        Finding("delegating_wrapper_review", "Client.send", "pkg/z_client.py", 42, 42, "candidate", ("e",), "static_ast"),
    ]
    ranked = rank_findings(findings)
    assert ranked[0].path == "pkg/z_client.py"
    assert [f.path for f in ranked[1:3]] == ["benchmarks/a_bench.py", "examples/a_demo.py"]
    assert {f.path for f in ranked[3:]} == {"conftest.py", "test/a_helper.py"}

    advisory = format_advisory(findings, max_bytes=250, max_items=3)
    assert "pkg/z_client.py:42" in advisory


def test_rank_findings_prevents_underscore_private_paths_and_symbols_from_evicting_public_apis():
    findings = [
        Finding("parameter_sink_review", "_helper", "pkg/_compat.py", 5, 5, "candidate", ("e",), "static_ast"),
        Finding("parameter_sink_review", "Internal._run", "pkg/_internal/a.py", 8, 8, "candidate", ("e",), "static_ast"),
        Finding("parameter_sink_review", "Client._private", "pkg/client.py", 12, 12, "candidate", ("e",), "static_ast"),
        Finding("parameter_sink_review", "Client.__init__", "pkg/client.py", 15, 15, "candidate", ("e",), "static_ast"),
        Finding("parameter_sink_review", "Client.send", "pkg/z_transport.py", 20, 20, "candidate", ("e",), "static_ast"),
    ]
    ranked = rank_findings(findings)
    # Public modules + public/dunder symbols come before single-underscore private helpers/modules.
    assert [(f.path, f.symbol) for f in ranked[:2]] == [
        ("pkg/client.py", "Client.__init__"),
        ("pkg/z_transport.py", "Client.send"),
    ]
    assert ranked[2] == findings[2]  # public module, private method
    assert {f.path for f in ranked[3:]} == {"pkg/_compat.py", "pkg/_internal/a.py"}


def test_rank_findings_maps_solidity_detector_priorities():
    findings = [
        Finding("state_sink_review", "Vault.DEAD", "src/a_vault.sol", 10, 10, "candidate", ("e",), "static_ast"),
        Finding("parameter_sink_review", "Vault.deposit", "src/b_vault.sol", 20, 20, "candidate", ("e",), "static_ast"),
        Finding("override_modifier_review", "Vault.withdraw", "src/c_vault.sol", 30, 30, "candidate", ("e",), "static_ast"),
        Finding("try_catch_sink_review", "Bridge.send", "src/z_bridge.sol", 40, 40, "candidate", ("e",), "static_ast"),
    ]
    ranked = rank_findings(findings)
    assert [f.detector for f in ranked] == [
        "try_catch_sink_review",
        "override_modifier_review",
        "parameter_sink_review",
        "state_sink_review",
    ]


def test_ledger_open_ranks_production_seams_ahead_of_tests():
    ledger = ObligationLedger()
    findings = [
        Finding("parameter_sink_review", "helper", "tests/a_test.py", 5, 5, "candidate", ("e",), "static_ast"),
        Finding("parameter_sink_review", "demo", "examples/a_demo.py", 6, 6, "candidate", ("e",), "static_ast"),
        Finding("required_keyword_missing", "Client.send", "pkg/z_core.py", 30, 30, "candidate", ("e",), "static_ast"),
    ]
    update = ledger.update(findings, lambda f: f.symbol)
    assert [item.finding.path for item in update.open] == [
        "pkg/z_core.py",
        "examples/a_demo.py",
        "tests/a_test.py",
    ]
    summary = format_open_obligations(update.open, max_bytes=120, show_ids=False)
    assert "pkg/z_core.py:30" in summary


def test_review_prompt_ranks_before_unique_site_cap_and_reserves_callee_contracts():
    # 13 findings (exceeds MAX_UNIQUE_SITES=12 and MAX_SNIPPETS=8):
    # Put 12 test-file findings first, then 1 production delegating_wrapper_review at the end.
    findings = [
        Finding("parameter_sink_review", f"t{i}", f"tests/a_{i:02d}_test.py", 3, 3, "candidate", ("unused x",), "static_ast")
        for i in range(12)
    ]
    findings.append(
        Finding(
            "delegating_wrapper_review",
            "Wrapper.send",
            "pkg/z_wrapper.py",
            3,
            3,
            "candidate",
            ("calls Core.send without forwarding timeout",),
            "static_ast",
        )
    )
    after_sources = {
        f"tests/a_{i:02d}_test.py": f"def t{i}(x=None):\n    return 1\n" for i in range(12)
    }
    after_sources["pkg/z_wrapper.py"] = (
        "class Wrapper:\n"
        "    def send(self, url, timeout=None):\n"
        "        return Core().send(url)\n"
    )
    after_sources["pkg/z_core.py"] = (
        "class Core:\n"
        "    def send(self, url, timeout=None):\n"
        "        return (url, timeout)\n"
    )
    prompt = format_review_prompt(
        findings=findings,
        anchor_for=lambda f: f.symbol,
        after_sources=after_sources,
        diff_text="",
    )
    # Production wrapper finding must be ranked #1 (not dropped by the 12-site cap)
    assert "1. [delegating_wrapper_review] pkg/z_wrapper.py:3" in prompt
    # And callee contract snippet must NOT be starved out even though >8 sites exist!
    assert "pkg/z_core.py :: Core.send [callee contract]" in prompt
    # Total snippet headers stay within MAX_SNIPPETS
    assert prompt.count("--- ") <= MAX_SNIPPETS


def test_review_snippet_truncation_centers_around_focus_line():
    # Build a 60-line function whose lines are ~100 chars each (~6000 chars > MAX_SNIPPET_CHARS=3200),
    # with focus_line near the bottom (line 52).
    body = ["def big(a, b=None):"]
    for idx in range(2, 56):
        marker = f"FOCUS_MARKER_LINE_{idx}" if idx == 52 else f"filler_var_{idx}"
        body.append(f"    {marker} = '" + ("x" * 80) + "'")
    body.append("    return a")
    src = "\n".join(body) + "\n"
    finding = Finding("parameter_sink_review", "big", "pkg/big.py", 52, 52, "candidate", ("unused b",), "static_ast")
    prompt = format_review_prompt(
        findings=[finding],
        anchor_for=lambda _f: "big",
        after_sources={"pkg/big.py": src},
        diff_text="",
    )
    assert "FOCUS_MARKER_LINE_52" in prompt


def test_review_cap_diff_prioritizes_flagged_file_over_huge_alphabetical_diff():
    huge_a = "diff --git a/a_generated.py b/a_generated.py\n--- a/a_generated.py\n+++ b/a_generated.py\n" + (
        "+x = 'noise'\n" * ((MAX_DIFF_CHARS // 12) + 500)
    )
    flagged_z = (
        "diff --git a/z_transport.py b/z_transport.py\n"
        "--- a/z_transport.py\n"
        "+++ b/z_transport.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def send(url):\n"
        "+def send(url, http1=True):\n"
    )
    finding = Finding("parameter_sink_review", "send", "z_transport.py", 1, 1, "candidate", ("unused http1",), "static_ast")
    prompt = format_review_prompt(
        findings=[finding],
        anchor_for=lambda _f: "send",
        after_sources={"z_transport.py": "def send(url, http1=True):\n    return url\n"},
        diff_text=huge_a + flagged_z,
    )
    assert "def send(url, http1=True):" in prompt
    diff_section = prompt.split("=== PRODUCTION .PY DIFF ===", 1)[1]
    assert diff_section.lstrip().startswith("diff --git a/z_transport.py b/z_transport.py")


# ---------------------------------------------------------------------------
# 2. Discrete Oscillation Detection in ObligationLedger & Hook Flow
# ---------------------------------------------------------------------------


def test_ledger_reopen_oscillation_triggers_thrashing_and_round_trips():
    session = DeltaSession.from_workspace({"lib.py": LIB, "app.py": APP})

    # Clean opening: not thrashing, standard <=250B advisory
    opened = session.apply_write("lib.py", LIB_THREE)
    assert opened.thrashing is False
    assert opened.advisory_text.startswith("[mip-delta] ")
    assert opened.advisory_utf8_bytes <= 250

    # Clean discharge: 0 bytes, not thrashing
    fixed = session.apply_write("app.py", APP_FIXED)
    assert fixed.thrashing is False
    assert fixed.advisory_text == ""
    assert fixed.advisory_utf8_bytes == 0

    # Re-breaking the same obligation (opened -> discharged -> opened): triggers thrashing!
    reopened = session.apply_write("app.py", APP)
    assert reopened.thrashing is True
    assert reopened.advisory_text.startswith("[mip-delta:thrash] oscillating (reopened 1x in use)")
    assert "app.py:4 return_contract_review[static] load in use" in reopened.advisory_text
    assert reopened.advisory_utf8_bytes <= 600

    # Persists cleanly across JSON round-trip
    data = session.ledger.to_dict()
    assert data["reopen_counts"]
    restored = ObligationLedger.from_dict(data)
    assert restored.open()[0].reopen_count == 1
    assert format_thrash_advisory(restored.update([], lambda _f: "")) == ""


def test_ledger_detects_three_consecutive_stalled_writes_on_same_file():
    session = DeltaSession.from_workspace({"lib.py": LIB, "app.py": APP})
    first = session.apply_write("lib.py", LIB_THREE)
    assert first.thrashing is False

    # Stalled writes 1 and 2 on app.py stay silent (0 bytes)
    s1 = session.apply_write("app.py", APP + "# edit 1\n")
    assert s1.thrashing is False and s1.advisory_text == ""
    s2 = session.apply_write("app.py", APP + "# edit 2\n")
    assert s2.thrashing is False and s2.advisory_text == ""

    # 3rd consecutive stalled write on app.py triggers widened thrash advisory
    s3 = session.apply_write("app.py", APP + "# edit 3\n")
    assert s3.thrashing is True
    assert s3.advisory_text.startswith("[mip-delta:thrash] stalled (3 writes to app.py without reducing 1 open):")
    assert "app.py:4 return_contract_review[static] load in use" in s3.advisory_text
    assert s3.advisory_utf8_bytes <= 600

    # Fixing the site discharges the obligation and resets stalled counter
    repaired = session.apply_write("app.py", APP_FIXED)
    assert repaired.thrashing is False and repaired.advisory_text == "" and repaired.open_obligations == 0


def test_claude_post_edit_hook_emits_thrash_advisory_on_oscillation_and_stall(tmp_path):
    repo = _make_repo(tmp_path, {"lib.py": LIB, "app.py": APP})

    def post_edit(file_path: Path):
        event = {
            "session_id": "osc",
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(file_path)},
            "cwd": str(repo),
        }
        return _run_cli(
            repo,
            "--hook",
            "claude-post-edit",
            stdin=json.dumps(event),
            env={"CLAUDE_PROJECT_DIR": str(repo)},
        )

    # Open obligation
    (repo / "lib.py").write_text(LIB_THREE, encoding="utf-8")
    r1 = json.loads(post_edit(repo / "lib.py").stdout)
    assert r1["hookSpecificOutput"]["additionalContext"].startswith("[mip-delta] ")

    # 1st and 2nd stalled edits on app.py -> silent
    (repo / "app.py").write_text(APP + "# try 1\n", encoding="utf-8")
    assert post_edit(repo / "app.py").stdout == ""
    (repo / "app.py").write_text(APP + "# try 2\n", encoding="utf-8")
    assert post_edit(repo / "app.py").stdout == ""

    # 3rd stalled edit on app.py -> widened [mip-delta:thrash] advisory
    (repo / "app.py").write_text(APP + "# try 3\n", encoding="utf-8")
    r_stall = json.loads(post_edit(repo / "app.py").stdout)
    ctx_stall = r_stall["hookSpecificOutput"]["additionalContext"]
    assert ctx_stall.startswith("[mip-delta:thrash] stalled (3 writes to app.py")
    assert len(ctx_stall.encode("utf-8")) <= 600

    # Fix (discharge), then re-break -> oscillating thrash advisory immediately
    (repo / "app.py").write_text(APP_FIXED, encoding="utf-8")
    assert post_edit(repo / "app.py").stdout == ""
    (repo / "app.py").write_text(APP, encoding="utf-8")
    r_osc = json.loads(post_edit(repo / "app.py").stdout)
    ctx_osc = r_osc["hookSpecificOutput"]["additionalContext"]
    assert ctx_osc.startswith("[mip-delta:thrash] oscillating (reopened 1x in use)")

    # Event log recorded thrashing events without breaking schema
    events = [
        json.loads(line)
        for line in (repo / ".mipdelta" / "ledger-osc.events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    thrash_events = [row for row in events if row["event"] == "thrashing"]
    assert len(thrash_events) == 2
    assert thrash_events[0]["stalled_writes"] == 3
    assert len(thrash_events[1]["reopened_ids"]) == 1


# ---------------------------------------------------------------------------
# 3. Canonical actual_delta Ground-Truth Tuple (--json, claude-stop, --review)
# ---------------------------------------------------------------------------


def test_summarize_actual_delta_captures_mutated_signatures_and_new_raises():
    before_src = (
        "class ApiError(Exception):\n    pass\n\n"
        "def fetch(url):\n    return url\n\n"
        "def legacy():\n    return 1\n"
    )
    after_src = (
        "class ApiError(Exception):\n    pass\n\n"
        "async def fetch(url, *, strict):\n    if not strict:\n        raise ApiError('bad')\n    return url, 1\n"
    )
    before_facts = {"pkg/api.py": parse_file("pkg/api.py", before_src)}
    after_facts = {"pkg/api.py": parse_file("pkg/api.py", after_src)}
    summary = summarize_actual_delta(before_facts, after_facts, findings=[])
    assert "pkg/api.py::fetch (params_changed,async_changed,return_changed)" in summary["mutated_signatures"]
    assert "pkg/api.py::legacy (removed)" in summary["mutated_signatures"]
    assert summary["new_raises"] == ["pkg/api.py::fetch +ApiError"]
    assert summary["open_count"] == 0
    assert set(summary) == {
        "mutated_signatures",
        "new_raises",
        "opened_obligations",
        "discharged_obligations",
        "open_count",
    }


def test_cli_json_and_claude_stop_emit_canonical_actual_delta(tmp_path):
    before_lib = (
        "class BadLoad(Exception):\n    pass\n\n"
        "def load(p):\n    return p, 1\n"
    )
    after_lib = (
        "class BadLoad(Exception):\n    pass\n\n"
        "def load(p, *, strict=False):\n    if strict:\n        raise BadLoad()\n    return p, 1, 2\n"
    )
    repo = _make_repo(tmp_path, {"lib.py": before_lib, "app.py": APP})
    (repo / "lib.py").write_text(after_lib, encoding="utf-8")

    # --check-git --json includes canonical actual_delta
    checked = _run_cli(repo, "--check-git", "--root", str(repo), "--json")
    assert checked.returncode == 0
    payload = json.loads(checked.stdout)
    delta = payload["actual_delta"]
    assert "lib.py::load (params_changed,return_changed)" in delta["mutated_signatures"]
    assert delta["new_raises"] == ["lib.py::load +BadLoad"]
    assert delta["open_count"] >= 1

    # claude-stop hook includes actual_delta alongside decision=block for mechanical gating
    stop_event = {"session_id": "gate", "hook_event_name": "Stop", "stop_hook_active": False, "cwd": str(repo)}
    stopped = _run_cli(
        repo,
        "--hook",
        "claude-stop",
        stdin=json.dumps(stop_event),
        env={"CLAUDE_PROJECT_DIR": str(repo)},
    )
    assert stopped.returncode == 0
    stop_payload = json.loads(stopped.stdout)
    assert stop_payload["decision"] == "block"
    stop_delta = stop_payload["actual_delta"]
    assert "lib.py::load (params_changed,return_changed)" in stop_delta["mutated_signatures"]
    assert stop_delta["new_raises"] == ["lib.py::load +BadLoad"]
    assert stop_delta["open_count"] >= 1
    assert len(stop_delta["opened_obligations"]) == stop_delta["open_count"]


def test_review_receives_canonical_actual_delta_in_prompt(tmp_path, monkeypatch, capsys):
    repo = _make_repo(
        tmp_path,
        {
            "mod.py": "def callee(a):\n    return a\n",
            "app.py": "from mod import callee\n\ndef run():\n    return callee(1)\n",
        },
    )
    (repo / "mod.py").write_text("def callee(a, b):\n    raise ValueError('x')\n", encoding="utf-8")

    captured: dict = {}

    def fake_review(**kwargs):
        captured.update(kwargs)
        prompt = format_review_prompt(
            findings=kwargs["findings"],
            anchor_for=kwargs["anchor_for"],
            after_sources=kwargs["after_sources"],
            diff_text=kwargs["diff_text"],
            changed_test_files=kwargs.get("changed_test_files", ()),
            subject=kwargs.get("subject", ""),
            cache=kwargs.get("cache"),
            actual_delta=kwargs.get("actual_delta"),
        )
        captured["rendered_prompt"] = prompt
        return ReviewResult(
            verdict="confirm_omission",
            reason="Missing required arg b.",
            backend="anthropic_api",
            model="claude-haiku-4-5",
            wall_ms=10.0,
        )

    monkeypatch.setattr(cli, "review_findings", fake_review)
    rc = cli.main_cli(["--check-git", "--root", str(repo), "--review", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["actual_delta"]["mutated_signatures"] == ["mod.py::callee (params_changed)"]
    assert out["actual_delta"]["new_raises"] == ["mod.py::callee +ValueError"]
    assert captured["actual_delta"] == out["actual_delta"]
    rendered = captured["rendered_prompt"]
    assert "=== CANONICAL STRUCTURAL DELTA (GROUND TRUTH) ===" in rendered
    assert "mod.py::callee (params_changed)" in rendered
    assert "mod.py::callee +ValueError" in rendered
