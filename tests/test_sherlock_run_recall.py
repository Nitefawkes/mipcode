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


def _make_src(tmp_path):
    src = tmp_path / "src"
    _write_tree(src)
    _git(src, "init", "-q")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    return src, _git(src, "rev-parse", "HEAD")


def test_git_calls_use_longpaths_and_no_prompt_env(tmp_path, monkeypatch):
    src, sha = _make_src(tmp_path)
    monkeypatch.setattr(run_recall, "remote_url", lambda repo: "file://" + src.as_posix())
    real = run_recall._run_with_tree_kill
    calls = []

    def spy(argv, **kw):
        calls.append((list(argv), kw))
        return real(argv, **kw)

    monkeypatch.setattr(run_recall, "_run_with_tree_kill", spy)
    run_recall.clone_at("own/name", sha, tmp_path / "cache")
    git_calls = [(argv, kw) for argv, kw in calls if argv[0] == "git"]
    assert git_calls
    for argv, kw in git_calls:
        assert argv[1:3] == ["-c", "core.longpaths=true"]
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kw["env"]["GCM_INTERACTIVE"] == "never"
        assert kw["env"]["GIT_ASKPASS"] == ""
        assert kw["timeout"] == (600 if "fetch" in argv else 120)


class _HangingPopen:
    """Popen stand-in whose first communicate() times out; records the kill path."""
    instances = []

    def __init__(self, argv, **kw):
        self.args, self.kw, self.pid, self.returncode = argv, kw, 4242, None
        self.calls = []
        _HangingPopen.instances.append(self)

    def communicate(self, timeout=None):
        self.calls.append(timeout)
        if len(self.calls) == 1:
            raise subprocess.TimeoutExpired(self.args, timeout)
        raise subprocess.TimeoutExpired(self.args, timeout)  # drain also hangs: swallowed


def _hang(monkeypatch):
    _HangingPopen.instances = []
    killed = []
    monkeypatch.setattr(run_recall.subprocess, "Popen", _HangingPopen)
    monkeypatch.setattr(run_recall, "_kill_tree", lambda p: killed.append(p.pid))
    return killed


def test_git_timeout_kills_tree_and_becomes_runtime_error(tmp_path, monkeypatch):
    killed = _hang(monkeypatch)
    with pytest.raises(RuntimeError, match=r"git init timed out after 120s for own/name"):
        run_recall.clone_at("own/name", "a" * 40, tmp_path / "cache")
    assert killed == [4242]
    assert _HangingPopen.instances[0].calls == [120, 10]  # drained with 10s cap
    assert _HangingPopen.instances[0].kw.get("shell") is None


def test_analyzer_timeout_kills_tree_and_becomes_runtime_error(tmp_path, monkeypatch):
    killed = _hang(monkeypatch)
    with pytest.raises(RuntimeError, match="2024-08-x"):
        run_recall.run_audit(tmp_path, label="2024-08-x")
    assert killed == [4242]
    assert _HangingPopen.instances[0].calls[0] == 600


def test_kill_tree_uses_taskkill_on_windows(monkeypatch):
    seen = []
    monkeypatch.setattr(run_recall.os, "name", "nt")
    monkeypatch.setattr(run_recall.subprocess, "run", lambda argv, **kw: seen.append(argv))
    run_recall._kill_tree(type("P", (), {"pid": 7})())
    assert seen == [["taskkill", "/T", "/F", "/PID", "7"]]


def test_clone_at_recovers_from_failed_checkout(tmp_path, monkeypatch):
    src, sha = _make_src(tmp_path)
    monkeypatch.setattr(run_recall, "remote_url", lambda repo: "file://" + src.as_posix())
    cache = tmp_path / "cache"
    d = cache / f"own__name@{sha}"
    d.mkdir(parents=True)
    _git(d, "init", "-q")  # unborn HEAD
    stale = d / "pkg" / "contracts" / "Vault.sol"  # partially populated tree
    stale.parent.mkdir(parents=True)
    stale.write_text("// partial", encoding="utf-8")
    assert run_recall.clone_at("own/name", sha, cache) == d
    assert _git(d, "rev-parse", "HEAD") == sha
    assert stale.read_text(encoding="utf-8") == VAULT


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


def test_production_counts_skips_sol_named_directories(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.sol").write_text("a\nb\nc\n", encoding="utf-8")
    d = tmp_path / "broadcast" / "Deploy.s.sol"
    d.mkdir(parents=True)
    (d / "run-latest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src" / "Lib.sol").mkdir()  # production-looking path, still a directory
    assert run_recall._production_counts(tmp_path) == (1, 3)


def _fake_git_runner(monkeypatch, checkout_results, ls_tree=b"", blobs=None):
    calls = []
    results = list(checkout_results)
    blobs = blobs or {}

    def fake(argv, **kw):
        sub = argv[argv.index("-C") + 2:]
        calls.append(list(sub))
        if sub[0] == "checkout":
            rc, err = results.pop(0)
            return subprocess.CompletedProcess(argv, rc, "", err)
        if sub[0] == "ls-tree":
            return subprocess.CompletedProcess(argv, 0, ls_tree, b"")
        if sub[0] == "cat-file":
            out = b""
            for oid in kw["input"].decode().split():
                out += f"{oid} blob {len(blobs[oid])}\n".encode() + blobs[oid] + b"\n"
            return subprocess.CompletedProcess(argv, 0, out, b"")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_recall, "_run_with_tree_kill", fake)
    return calls


def test_clone_at_plumbing_fallback_on_invalid_path(tmp_path, monkeypatch):
    good = b"contract A {}\r\n\x00bin"
    ls = b"".join([
        b"100644 blob " + b"a" * 40 + b"\tsrc/A.sol\0",
        b"100644 blob " + b"b" * 40 + b"\tx/b:c.json\0",
        b"120000 blob " + b"c" * 40 + b"\tsrc/link.sol\0",
        b"160000 commit " + b"d" * 40 + b"\tlib/sub\0",
        b"100644 blob " + b"e" * 40 + b"\t../evil.sol\0",
    ])
    calls = _fake_git_runner(monkeypatch, [(128, "error: invalid path 'a/b:c.json'")],
                             ls_tree=ls, blobs={"a" * 40: good})
    d = run_recall.clone_at("own/name", "abc123", tmp_path / "cache")
    assert d == tmp_path / "cache" / "own__name@abc123"
    written = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
    assert written == ["src/A.sol"]
    assert (d / "src" / "A.sol").read_bytes() == good
    assert not (tmp_path / "cache" / "evil.sol").exists()
    assert run_recall.LAST_PLUMBING_DROPPED == 4
    assert ["update-ref", "HEAD", "FETCH_HEAD"] in calls
    assert all(c[0] != "sparse-checkout" and "core.protectNTFS=false" not in c for c in calls)


def test_clone_at_other_checkout_failure_raises_without_sparse(tmp_path, monkeypatch):
    calls = _fake_git_runner(monkeypatch, [(128, "fatal: something else")])
    with pytest.raises(RuntimeError, match="checkout failed"):
        run_recall.clone_at("own/name", "abc123", tmp_path / "cache")
    assert all(c[0] != "sparse-checkout" for c in calls)


def test_plumbing_drops_git_dir_and_windows_aliased_paths(tmp_path, monkeypatch):
    bad = [".git/hooks/x", ".git" + chr(92) + "hooks" + chr(92) + "x", "GIT~1/config",
           ".GIT/config", "con.sol", "a/nul", "a/NUL.txt", "lpt1/x.sol"]
    ls = b"".join(b"100644 blob " + b"a" * 40 + b"\t" + p.encode() + b"\0" for p in bad)
    ls += b"100644 blob " + b"b" * 40 + b"\tsrc/ok.sol\0"
    _fake_git_runner(monkeypatch, [(128, "error: invalid path 'x'")], ls_tree=ls,
                     blobs={"a" * 40: b"evil", "b" * 40: b"ok"})
    d = run_recall.clone_at("own/name", "abc123", tmp_path / "cache")
    written = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
    assert written == ["src/ok.sol"]
    assert run_recall.LAST_PLUMBING_DROPPED == len(bad)


def test_plumbing_missing_blob_raises(tmp_path, monkeypatch):
    calls = []

    def fake(argv, **kw):
        sub = argv[argv.index("-C") + 2:]
        calls.append(sub)
        if sub[0] == "checkout":
            return subprocess.CompletedProcess(argv, 128, "", "error: invalid path 'x'")
        if sub[0] == "ls-tree":
            return subprocess.CompletedProcess(
                argv, 0, b"100644 blob " + b"a" * 40 + b"\tsrc/A.sol\0", b"")
        if sub[0] == "cat-file":
            return subprocess.CompletedProcess(argv, 0, b"a" * 40 + b" missing\n", b"")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_recall, "_run_with_tree_kill", fake)
    with pytest.raises(RuntimeError, match="unexpected header"):
        run_recall.clone_at("own/name", "abc123", tmp_path / "cache")
    assert not any(c[0] == "update-ref" for c in calls)
