"""Tests for optional --review Stage-2 micro-triage integration."""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from mipdelta import cli
from mipdelta.model import Finding
from mipdelta.review import (
    REVIEW_SYSTEM_PROMPT,
    ReviewError,
    ReviewResult,
    format_review_prompt,
    review_findings,
)


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "mod.py").write_text("def callee(a):\n    return a\n", encoding="utf-8")
    (root / "app.py").write_text("from mod import callee\n\ndef run():\n    return callee(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True, capture_output=True)
    return root


def test_review_prompt_includes_eager_to_deferred_async_guidance():
    assert "execution-timing-shifted" in REVIEW_SYSTEM_PROMPT
    assert "eager-to-deferred" in REVIEW_SYSTEM_PROMPT


def test_review_not_invoked_when_stage1_is_silent(tmp_path, monkeypatch, capsys):
    root = _git_repo(tmp_path)
    (root / "mod.py").write_text("def callee(a):\n    return a + 1\n", encoding="utf-8")
    called = False

    def boom(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("review_findings must not run when Stage 1 is silent")

    monkeypatch.setattr(cli, "review_findings", boom)
    rc = cli.main_cli(["--check-git", "--root", str(root), "--review", "--fail-on-findings"])
    assert rc == 0
    assert called is False
    assert capsys.readouterr().out == ""


def test_review_suppresses_false_alarm_in_check_git(tmp_path, monkeypatch, capsys):
    root = _git_repo(tmp_path)
    (root / "mod.py").write_text("def callee(a, b):\n    return a + b\n", encoding="utf-8")

    captured = {}

    def fake_review(**kwargs):
        captured.update(kwargs)
        return ReviewResult(
            verdict="suppress_false_alarm",
            reason="Test false-alarm suppression.",
            backend="claude_cli",
            model="haiku",
            wall_ms=12.3,
            cost_usd=0.002,
        )

    monkeypatch.setattr(cli, "review_findings", fake_review)
    rc = cli.main_cli(["--check-git", "--root", str(root), "--review", "--fail-on-findings", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["findings"]) >= 1
    assert payload["advisory_text"] == ""
    assert payload["review"]["verdict"] == "suppress_false_alarm"
    assert payload["review"]["suppressed"] is True
    assert captured["findings"]


def test_review_confirms_omission_and_fails_on_findings(tmp_path, monkeypatch, capsys):
    root = _git_repo(tmp_path)
    (root / "mod.py").write_text("def callee(a, b):\n    return a + b\n", encoding="utf-8")

    def fake_review(**_kwargs):
        return ReviewResult(
            verdict="confirm_omission",
            reason="app.py:4 omits new required arg b.",
            backend="anthropic_api",
            model="claude-haiku-4-5",
            wall_ms=42.0,
            cost_usd=0.0018,
        )

    monkeypatch.setattr(cli, "review_findings", fake_review)
    rc = cli.main_cli(["--check-git", "--root", str(root), "--review", "--fail-on-findings"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[mip-delta]" in out
    assert "[mip-delta:review confirm_omission] app.py:4 omits new required arg b." in out


def test_review_works_in_candidate_mode(tmp_path, monkeypatch, capsys):
    root = _git_repo(tmp_path)
    candidate = tmp_path / "cand.py"
    candidate.write_text("def callee(a, b=None):\n    return a\n", encoding="utf-8")

    def fake_review(**_kwargs):
        return ReviewResult(
            verdict="confirm_omission",
            reason="Parameter b is unused in callee.",
            backend="claude_cli",
            model="haiku",
            wall_ms=15.0,
        )

    monkeypatch.setattr(cli, "review_findings", fake_review)
    rc = cli.main_cli(
        [
            "--root",
            str(root),
            "--write-path",
            "mod.py",
            "--candidate-file",
            str(candidate),
            "--review",
            "--fail-on-findings",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "[mip-delta:review confirm_omission] Parameter b is unused in callee." in out


def test_review_missing_credentials_returns_error(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    (root / "mod.py").write_text("def callee(a, b):\n    return a + b\n", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("mipdelta.review.shutil.which", lambda _name: None)
    rc = cli.main_cli(["--check-git", "--root", str(root), "--review", "--json"])
    assert rc == 2


def test_anthropic_api_backend_parses_tool_use_response(monkeypatch):
    finding = Finding("parameter_sink_review", "f", "pkg/a.py", 2, 4, "candidate", ("unused x",), "static_ast")
    after_sources = {"pkg/a.py": "import os\n\ndef f(x=None):\n    return 1\n"}

    fake_response = {
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 200, "output_tokens": 40, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "content": [
            {
                "type": "tool_use",
                "name": "submit_review",
                "input": {"verdict": "review_worthy", "reason": "x is unused in body."},
            }
        ],
    }

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with mock.patch("urllib.request.urlopen", return_value=FakeResp(json.dumps(fake_response).encode("utf-8"))):
        res = review_findings(
            findings=[finding],
            anchor_for=lambda _f: "f",
            after_sources=after_sources,
            diff_text="--- a/pkg/a.py\n+++ b/pkg/a.py\n+def f(x=None):",
        )
    assert res.backend == "anthropic_api"
    assert res.verdict == "review_worthy"
    assert res.suppressed is False
    assert res.cost_usd == pytest.approx(0.0004)


def test_format_review_prompt_bounds_and_includes_snippet():
    finding = Finding("parameter_sink_review", "f", "pkg/a.py", 3, 4, "candidate", ("unused x",), "static_ast")
    prompt = format_review_prompt(
        findings=[finding],
        anchor_for=lambda _f: "f",
        after_sources={"pkg/a.py": "import sys\n\ndef f(x=None):\n    return 1\n"},
        diff_text="+def f(x=None):",
    )
    assert "parameter_sink_review" in prompt
    assert "import sys" in prompt
    assert "Lines 3-4:" in prompt
    assert "+def f(x=None):" in prompt


def test_review_empty_findings_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        review_findings(findings=[], anchor_for=lambda _f: "", after_sources={}, diff_text="")
