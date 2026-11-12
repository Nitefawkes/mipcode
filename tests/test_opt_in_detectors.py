"""Detectors that report only when a caller asks for them."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mipdelta import DeltaSession, analyze_delta
from mipdelta.delta import OPT_IN_DETECTORS, analyze_changed_only
from mipdelta.gateway import DeltaImpactSideband

SRC = str(Path(__file__).resolve().parents[1] / "src")
TRUTHINESS = "parameter_truthiness_review"
BEFORE = "def serialize(data):\n    return str(data)\n"
AFTER = (
    "def serialize(data, base=None):\n"
    "    if base:\n"
    "        return base + str(data)\n"
    "    return str(data)\n"
)


def _detectors(findings):
    return sorted(f.detector for f in findings)


def test_truthiness_review_is_listed_as_opt_in():
    assert TRUTHINESS in OPT_IN_DETECTORS


def test_truthiness_review_is_off_unless_enabled():
    files = {"serialize.py": BEFORE}, {"serialize.py": AFTER}
    assert analyze_delta(*files) == []
    assert _detectors(analyze_delta(*files, enable=[TRUTHINESS])) == [TRUTHINESS]


def test_changed_only_analysis_honors_the_same_switch():
    files = {"serialize.py": BEFORE}, {"serialize.py": AFTER}
    assert analyze_changed_only(*files)[0] == []
    assert _detectors(analyze_changed_only(*files, enable=[TRUTHINESS])[0]) == [TRUTHINESS]


REMOVED_BEFORE = {"lib.py": "def old(x):\n    return x\n", "app.py": "from lib import old\n\ndef use():\n    return old(1)\n"}
REMOVED_AFTER = {"lib.py": "def new(x):\n    return x\n", "app.py": REMOVED_BEFORE["app.py"]}
BOUND_BEFORE = {"lib.py": "def dump(v):\n    return v\n", "table.py": "from lib import dump\n\nA = prop(dump)\n"}
BOUND_AFTER = {"lib.py": "def dump(v, *, strict):\n    return v if strict else None\n", "table.py": BOUND_BEFORE["table.py"]}


@pytest.mark.parametrize(
    ("detector", "before", "after"),
    [
        ("removed_callable_review", REMOVED_BEFORE, REMOVED_AFTER),
        ("callback_binding_review", BOUND_BEFORE, BOUND_AFTER),
    ],
)
def test_noisy_cross_file_detectors_are_off_unless_enabled(detector, before, after):
    assert detector in OPT_IN_DETECTORS
    assert detector not in _detectors(analyze_delta(before, after))
    assert detector in _detectors(analyze_delta(before, after, enable=[detector]))


def test_unknown_detector_name_is_rejected():
    with pytest.raises(ValueError, match="call_signature_review"):
        analyze_delta({"serialize.py": BEFORE}, {"serialize.py": AFTER}, enable=["call_signature_review"])


def test_session_reports_the_detector_only_when_enabled():
    quiet = DeltaSession.from_workspace({"serialize.py": BEFORE})
    assert quiet.apply_write("serialize.py", AFTER).findings == ()

    enabled = DeltaSession.from_workspace({"serialize.py": BEFORE}, enable=[TRUTHINESS])
    assert _detectors(enabled.apply_write("serialize.py", AFTER).findings) == [TRUTHINESS]
    assert len(enabled.open_obligations()) == 1


def test_gateway_sideband_passes_the_switch_to_its_session(tmp_path):
    (tmp_path / "serialize.py").write_text(BEFORE, encoding="utf-8")
    quiet = DeltaImpactSideband(tmp_path)
    enabled = DeltaImpactSideband(tmp_path, enable=[TRUTHINESS])
    assert quiet.delta_session.apply_write("serialize.py", AFTER).findings == ()
    assert _detectors(enabled.delta_session.apply_write("serialize.py", AFTER).findings) == [TRUTHINESS]


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "serialize.py").write_text(BEFORE, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True, capture_output=True)
    (root / "serialize.py").write_text(AFTER, encoding="utf-8")
    return root


def _run(repo: Path, *args: str, stdin: str = ""):
    env = {**os.environ, "PYTHONPATH": SRC, "CLAUDE_PROJECT_DIR": str(repo)}
    return subprocess.run(
        [sys.executable, "-m", "mipdelta.cli", *args],
        cwd=repo, input=stdin, capture_output=True, text=True, encoding="utf-8", env=env,
    )


def test_cli_git_check_needs_the_enable_flag(repo):
    assert _run(repo, "--check-git", "--root", str(repo)).stdout == ""
    enabled = _run(repo, "--check-git", "--root", str(repo), "--enable", TRUTHINESS)
    assert enabled.returncode == 0
    assert f"serialize.py:2 {TRUTHINESS}" in enabled.stdout


def test_cli_candidate_check_needs_the_enable_flag(repo, tmp_path):
    (repo / "serialize.py").write_text(BEFORE, encoding="utf-8")
    candidate = tmp_path / "candidate.py"
    candidate.write_text(AFTER, encoding="utf-8")
    args = ("--root", str(repo), "--write-path", "serialize.py", "--candidate-file", str(candidate))
    assert _run(repo, *args).stdout == ""
    assert TRUTHINESS in _run(repo, *args, "--enable", TRUTHINESS).stdout


def test_cli_rejects_a_detector_that_is_not_opt_in(repo):
    result = _run(repo, "--check-git", "--root", str(repo), "--enable", "call_signature_review")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_post_edit_hook_needs_the_enable_flag(repo):
    event = json.dumps(
        {"session_id": "s1", "cwd": str(repo), "tool_input": {"file_path": str(repo / "serialize.py")}}
    )
    assert _run(repo, "--hook", "claude-post-edit", stdin=event).stdout == ""
    enabled = _run(repo, "--hook", "claude-post-edit", "--enable", TRUTHINESS, stdin=event)
    context = json.loads(enabled.stdout)["hookSpecificOutput"]["additionalContext"]
    assert TRUTHINESS in context
