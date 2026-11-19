"""Analyzer runner: clone at a pinned SHA and run the frozen analyzer."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "sherlock_recall"))

run_recall = importlib.import_module("run_recall")

VAULT = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Vault {
    uint256 private constant _VIRTUAL_SHARES = 1000;
    uint256 private constant _ACTIVE_SCALE = 1e18;
    uint256[49] private __gap;

    function preview(uint256 assets) external pure returns (uint256) {
        return assets * _ACTIVE_SCALE;
    }
}
"""


def _write_tree(base: Path) -> None:
    (base / "pkg" / "contracts").mkdir(parents=True)
    (base / "pkg" / "contracts" / "Vault.sol").write_text(VAULT, encoding="utf-8")
    (base / "test").mkdir()
    (base / "test" / "Vault.t.sol").write_text(VAULT, encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_runner_end_to_end_on_local_dir(tmp_path):
    repo = tmp_path / "repo"
    _write_tree(repo)
    out = run_recall.run_audit(repo)
    assert "wall_ms" in out
    dead = [f for f in out["findings"] if f["detector"] == "state_sink_review"]
    assert dead
    assert any(f["path"] == "pkg/contracts/Vault.sol" for f in dead)
    assert all(not f["path"].startswith(("test/", "/")) and ":" not in f["path"]
               for f in out["findings"] if run_recall.is_production_sol(f["path"]))


def test_audit_contest_filters_production_and_counts(tmp_path):
    repo = tmp_path / "repo"
    _write_tree(repo)
    entry = run_recall.audit_contest(
        {"name": "2024-08-x", "companion_repo": "o/x", "resolved_sha": "abc"}, repo)
    assert entry["files_scanned"] == 2
    assert entry["production_files"] == 1
    assert entry["production_lines"] == len(VAULT.splitlines())
    assert entry["all_findings_count"] >= len(entry["findings"]) > 0
    assert all(f["path"] == "pkg/contracts/Vault.sol" for f in entry["findings"])


def test_run_audit_failure_names_contest(tmp_path):
    with pytest.raises(RuntimeError, match="2024-08-x"):
        run_recall.run_audit(tmp_path / "does-not-exist", label="2024-08-x")


def test_clone_at_local_file_repo_and_reuse(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _write_tree(src)
    _git(src, "init", "-q")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    sha = _git(src, "rev-parse", "HEAD")
    monkeypatch.setattr(run_recall, "remote_url", lambda repo: "file://" + src.as_posix())
    cache = tmp_path / "cache"
    d = run_recall.clone_at("own/name", sha, cache)
    assert d == cache / f"own__name@{sha}"
    assert (d / "pkg" / "contracts" / "Vault.sol").exists()
    monkeypatch.setattr(run_recall, "remote_url", lambda repo: "file://" + (tmp_path / "gone").as_posix())
    assert run_recall.clone_at("own/name", sha, cache) == d  # reused, no fetch


def test_main_records_per_contest_error_and_writes_json(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_tree(repo)
    out = tmp_path / "out"
    out.mkdir()
    (out / "recall_set.json").write_text(json.dumps({"contests": [
        {"name": "good", "set": "dev", "companion_repo": "o/good", "resolved_sha": "a1", "findings": []},
        {"name": "bad", "set": "dev", "companion_repo": "o/bad", "resolved_sha": "b2", "findings": []},
        {"name": "other", "set": "measurement", "companion_repo": "o/other", "resolved_sha": "c3",
         "findings": []},
    ]}), encoding="utf-8")

    def fake_clone(repo_name, sha, cache):
        if repo_name == "o/bad":
            raise RuntimeError("clone failed")
        return repo

    monkeypatch.setattr(run_recall, "clone_at", fake_clone)
    rc = run_recall.main(["--set", "dev", "--out", str(out), "--engine-commit", "3ebdabd"])
    assert rc == 0
    data = json.loads((out / "hits_dev.json").read_text(encoding="utf-8"))
    assert data["engine_commit"] == "3ebdabd"
    names = [c["name"] for c in data["contests"]]
    assert names == ["good", "bad"]
    good, bad = data["contests"]
    assert good["findings"] and good["resolved_sha"] == "a1"
    assert "clone failed" in bad["error"]
    assert "candidates" not in good


def test_no_local_paths_guard():
    with pytest.raises(ValueError):
        run_recall._assert_no_local_paths({"p": "C:/Users/x"})
    with pytest.raises(ValueError):
        run_recall._assert_no_local_paths({"p": "file:///tmp/x"})
    run_recall._assert_no_local_paths({"p": "pkg/contracts/Vault.sol"})
