"""Empirical test matrix for dataciv Moltbook sink & pragma insights."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from mipdelta import analyze_delta, audit_workspace, cli


# (a) Wrapper-side diff bug: param added to wrapper (not callee), logged/validated locally,
# omitted in delegating call to already-accepting callee -> MUST catch.


def test_a_wrapper_diff_bug_logged_and_validated_locally_is_caught(tmp_path):
    before = {
        "core.py": (
            "def send(url, *, timeout=5, retries=0):\n"
            "    return (url, timeout, retries)\n"
        ),
        "client.py": (
            "import logging\n"
            "from core import send\n\n"
            "logger = logging.getLogger(__name__)\n\n"
            "def fetch(url, *, timeout=5):\n"
            "    return send(url, timeout=timeout)\n"
        ),
    }
    after = {
        "core.py": before["core.py"],
        "client.py": (
            "import logging\n"
            "from core import send\n\n"
            "logger = logging.getLogger(__name__)\n\n"
            "def fetch(url, *, timeout=5, retries=0):\n"
            "    logger.debug('fetch retries=%s', retries)\n"
            "    if retries < 0:\n"
            "        raise ValueError(f'invalid retries={retries}')\n"
            "    return send(url, timeout=timeout)\n"
        ),
    }

    findings = analyze_delta(before, after)
    assert [(f.detector, f.symbol, f.path, f.start_line) for f in findings] == [
        ("delegating_wrapper_review", "fetch", "client.py", 10)
    ]
    assert "retries" in findings[0].evidence[0]

    # Also verify end-to-end via CLI --check-git in a git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    for rel, text in before.items():
        (repo / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, capture_output=True)
    for rel, text in after.items():
        (repo / rel).write_text(text, encoding="utf-8")

    rc = cli.main_cli(["--root", str(repo), "--check-git", "--fail-on-findings"])
    assert rc == 1


def test_a_wrapper_diff_bug_with_validation_helper_is_caught():
    before = {
        "core.py": "def send(url, timeout=5, retries=0):\n    return (url, timeout, retries)\n",
        "client.py": (
            "from core import send\n\n"
            "def _validate_retries(val):\n"
            "    if val < 0:\n"
            "        raise ValueError(val)\n\n"
            "def fetch(url, timeout=5):\n"
            "    return send(url, timeout=timeout)\n"
        ),
    }
    after = {
        "core.py": before["core.py"],
        "client.py": (
            "from core import send\n\n"
            "def _validate_retries(val):\n"
            "    if val < 0:\n"
            "        raise ValueError(val)\n\n"
            "def fetch(url, timeout=5, retries=0):\n"
            "    _validate_retries(retries)\n"
            "    return send(url, timeout=timeout)\n"
        ),
    }

    findings = analyze_delta(before, after)
    assert [(f.detector, f.symbol) for f in findings] == [("delegating_wrapper_review", "fetch")]
    assert "retries" in findings[0].evidence[0]


# (b) Cold-audit local-read-only omission -> detection vs abstention carefully tested.


def test_b_cold_audit_catches_local_read_only_omission_and_abstains_appropriately():
    files = {
        "pkg/transport.py": (
            "import logging\n"
            "logger = logging.getLogger(__name__)\n\n"
            "def create_transport(url, verify=True, cert=None, http1=True, http2=False, timeout=5.0):\n"
            "    return (url, verify, cert, http1, http2, timeout)\n\n"
            "def _init_proxy_transport(url, verify=True, cert=None, http1=True, http2=False, timeout=5.0):\n"
            "    logger.debug('proxy transport http1=%s', http1)\n"
            "    msg = f'timeout={timeout}'\n"
            "    logger.info(msg)\n"
            "    return create_transport(url, verify=verify, cert=cert, http2=http2)\n\n"
            "def _narrow_no_optional_forwarded(url, timeout=5.0):\n"
            "    # Abstain: no sibling optional args are forwarded to create_transport\n"
            "    logger.debug('narrow url=%s timeout=%s', url, timeout)\n"
            "    return create_transport(url)\n\n"
            "def _real_side_effect_sink(url, verify=True, http1=True, *, audit_hook=None):\n"
            "    # Abstain: http1 reaches a non-logging outbound call\n"
            "    if audit_hook is not None:\n"
            "        audit_hook(http1)\n"
            "    return create_transport(url, verify=verify)\n"
        ),
    }

    findings = audit_workspace(files)
    flagged = [(f.symbol, f.evidence[0]) for f in findings if f.detector == "delegating_wrapper_review"]
    assert len(flagged) == 2
    assert all(sym == "_init_proxy_transport" for sym, _ in flagged)
    assert any("http1" in ev for _, ev in flagged)
    assert any("timeout" in ev for _, ev in flagged)


# (c) Transformed-and-forwarded -> MUST NOT false-alarm (TN) across diff & cold audit.


def test_c_transformed_and_forwarded_parameters_do_not_false_alarm():
    before = {
        "pkg/transport.py": (
            "def normalize(val):\n    return val\n\n"
            "def transport(url, timeout=5.0, http1=True, p=None, config=None):\n"
            "    return (url, timeout, http1, p, config)\n\n"
            "def wrap_norm(url, timeout=5.0, http1=True):\n"
            "    return transport(url, timeout=timeout, http1=http1)\n"
        )
    }
    after = {
        "pkg/transport.py": (
            "def normalize(val):\n    return val\n\n"
            "def transport(url, timeout=5.0, http1=True, p=None, config=None):\n"
            "    return (url, timeout, http1, p, config)\n\n"
            "def wrap_norm(url, timeout=5.0, http1=True, p=None):\n"
            "    eff = normalize(timeout)\n"
            "    x = p if p is not None else 'default'\n"
            "    return transport(url, eff, http1=http1, config=x)\n\n"
            "def wrap_norm_kw(url, timeout=5.0, http1=True):\n"
            "    eff = normalize(timeout)\n"
            "    return transport(url, timeout=eff, http1=http1)\n\n"
            "def wrap_ternary_pos(url, timeout=5.0, http1=True, p=None):\n"
            "    x = p if p is not None else 'default'\n"
            "    return transport(url, timeout, http1, x)\n\n"
            "def wrap_dict_opts(url, timeout=5.0, http1=True):\n"
            "    opts = {'http1': http1}\n"
            "    return transport(url, timeout=timeout, config=opts)\n\n"
            "def wrap_subscript_opts(url, timeout=5.0, http1=True):\n"
            "    opts = {}\n"
            "    opts['http1'] = http1\n"
            "    return transport(url, timeout=timeout, config=opts)\n\n"
            "def wrap_dict_kwargs(url, timeout=5.0, http1=True):\n"
            "    opts = {'http1': http1}\n"
            "    return transport(url, timeout=timeout, **opts)\n\n"
            "def wrap_branch_assign(url, timeout=5.0, p=None):\n"
            "    x = 'default'\n"
            "    if p is not None:\n"
            "        x = normalize(p)\n"
            "    return transport(url, timeout=timeout, p=x)\n"
        )
    }

    assert analyze_delta(before, after) == []
    assert audit_workspace(after) == []


# (d) Intentional local control-flow guard (`if dry_run: return None`) -> clean TN.


def test_d_intentional_local_control_flow_guard_is_clean_in_diff_and_cold_audit():
    before = {
        "pkg/runner.py": (
            "def execute_step(cmd, timeout=30, dry_run=False):\n"
            "    return (cmd, timeout, dry_run)\n\n"
            "def run_job(cmd, timeout=30):\n"
            "    return execute_step(cmd, timeout=timeout)\n"
        )
    }
    after = {
        "pkg/runner.py": (
            "def execute_step(cmd, timeout=30, dry_run=False):\n"
            "    return (cmd, timeout, dry_run)\n\n"
            "def run_job(cmd, timeout=30, dry_run=False):\n"
            "    if dry_run:\n"
            "        return None\n"
            "    return execute_step(cmd, timeout=timeout)\n\n"
            "def run_job_gated(cmd, timeout=30, dry_run=False):\n"
            "    res = None\n"
            "    if not dry_run:\n"
            "        res = execute_step(cmd, timeout=timeout)\n"
            "    return res\n"
        )
    }

    assert analyze_delta(before, after) == []
    assert audit_workspace(after) == []


# (e) `# mip-delta: intentional-sink(...)` and `# mip-delta: ignore(...)` suppression across diff & cold audit.


def test_e_inline_comment_pragmas_suppress_in_diff_and_cold_audit():
    before = {
        "pkg/service.py": (
            "import logging\n"
            "logger = logging.getLogger(__name__)\n\n"
            "def send(url, timeout=5, retries=0, p1=None, p2=None, p3=None):\n"
            "    return (url, timeout, retries, p1, p2, p3)\n\n"
            "def wrap_def_line(url, timeout=5):\n"
            "    return send(url, timeout=timeout)\n\n"
            "def wrap_ignore_body(url, timeout=5):\n"
            "    return send(url, timeout=timeout)\n\n"
            "def wrap_body_multi(url, timeout=5):\n"
            "    return send(url, timeout=timeout)\n\n"
            "def sink_only(url):\n"
            "    return url\n"
        ),
        "pkg/store.py": (
            "class Client:\n"
            "    def __init__(self, url, timeout=5):\n"
            "        self.url = url\n"
            "        self.timeout = timeout\n"
            "    def send(self, url, timeout=5, legacy_flag=False):\n"
            "        return (url, timeout, legacy_flag)\n"
        ),
    }
    after = {
        "pkg/service.py": (
            "import logging\n"
            "logger = logging.getLogger(__name__)\n\n"
            "def send(url, timeout=5, retries=0, p1=None, p2=None, p3=None):\n"
            "    return (url, timeout, retries, p1, p2, p3)\n\n"
            "def wrap_def_line(url, timeout=5, retries=0):  # mip-delta: intentional-sink(retries)\n"
            "    logger.debug('retries=%s', retries)\n"
            "    return send(url, timeout=timeout)\n\n"
            "def wrap_ignore_body(url, timeout=5, p1=None):\n"
            "    # mip-delta: ignore(p1)\n"
            "    logger.debug('p1=%s', p1)\n"
            "    return send(url, timeout=timeout)\n\n"
            "def wrap_body_multi(url, timeout=5, p1=None, p2=None, p3=None):\n"
            "    # mip-delta: intentional-sink(p1, p2)\n"
            "    logger.debug('%s %s %s', p1, p2, p3)\n"
            "    return send(url, timeout=timeout)\n\n"
            "def sink_only(url, p1=None, p2=None):  # mip-delta: ignore(p1)\n"
            "    # mip-delta: intentional-sink(p2)\n"
            "    return url\n"
        ),
        "pkg/store.py": (
            "class Client:\n"
            "    def __init__(self, url, timeout=5, legacy_flag=False):  # mip-delta: ignore(legacy_flag)\n"
            "        self.url = url\n"
            "        self.timeout = timeout\n"
            "        self.legacy_flag = legacy_flag\n"
            "    def send(self, url, timeout=5, legacy_flag=False):\n"
            "        return (url, timeout, legacy_flag)\n"
        ),
    }

    diff_findings = analyze_delta(before, after)
    assert [(f.detector, f.symbol, f.evidence[0]) for f in diff_findings] == [
        (
            "delegating_wrapper_review",
            "wrap_body_multi",
            "delegating wrapper wrap_body_multi calls send without forwarding optional parameter p3",
        )
    ]

    cold_findings = audit_workspace(after)
    assert [(f.detector, f.symbol, f.evidence[0]) for f in cold_findings] == [
        (
            "delegating_wrapper_review",
            "wrap_body_multi",
            "delegating wrapper wrap_body_multi calls send without forwarding optional parameter p3",
        )
    ]


def test_e_pragma_does_not_match_strings_or_leak_from_nested_functions():
    files = {
        "pkg/traps.py": (
            "import logging\n"
            "logger = logging.getLogger(__name__)\n\n"
            "def callee(url, timeout=5, retries=0):\n"
            "    return (url, timeout, retries)\n\n"
            "def string_trap(url, timeout=5, retries=0):\n"
            "    _hint = '# mip-delta: ignore(retries)'\n"
            "    logger.debug('retries=%s', retries)\n"
            "    return callee(url, timeout=timeout)\n\n"
            "def nested_trap(url, timeout=5, retries=0):\n"
            "    def inner(retries=1):  # mip-delta: ignore(retries)\n"
            "        return retries\n"
            "    logger.debug('retries=%s', retries)\n"
            "    return callee(url, timeout=timeout)\n"
        )
    }

    findings = audit_workspace(files)
    assert sorted(f.symbol for f in findings if f.detector == "delegating_wrapper_review") == [
        "nested_trap",
        "string_trap",
    ]


def test_zero_runtime_dependencies_in_mipdelta():
    src_dir = Path(__file__).resolve().parents[1] / "src" / "mipdelta"
    stdlib = set(sys.stdlib_module_names)
    for py_file in sorted(src_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in stdlib or top == "mipdelta", f"non-stdlib import {top} in {py_file.name}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                assert top in stdlib or top == "mipdelta", f"non-stdlib import {top} in {py_file.name}"
