"""Unit and CLI tests for cold-snapshot repository auditing (`--audit-repo`)."""
from __future__ import annotations

import json
from pathlib import Path

from mipdelta import audit_workspace, cli
from mipdelta.facts import parse_file
from mipdelta.resolve import build_index, resolve_call_with_provenance
from mipdelta.review import ReviewResult, format_review_prompt


def test_cls_method_call_resolves_statically():
    src = """
class Builder:
    @classmethod
    def extract(cls, value, *, limit=10):
        return (value, limit)

    @classmethod
    def from_input(cls, value, *, limit=10):
        return cls.extract(value, limit=limit)
"""
    ff = parse_file("pkg/builder.py", src)
    index = build_index({"pkg/builder.py": ff})
    calls = [c for c in ff.functions["Builder.from_input"].own_calls if c.callee_name == "extract"]
    assert len(calls) == 1
    assert resolve_call_with_provenance(calls[0], ff, index) == (
        (ff.functions["Builder.extract"], "static_ast"),
    )


def test_cold_audit_catches_unread_and_stored_and_recursive_wrapper_drops():
    files = {
        "pkg/client.py": """
def create_transport(verify=True, cert=None, http1=True, http2=False):
    return (verify, cert, http1, http2)

def _init_proxy_transport(verify=True, cert=None, http1=True, http2=False):
    return create_transport(verify=verify, cert=cert, http2=http2)
""",
        "pkg/tb.py": """
class Traceback:
    def __init__(
        self,
        trace=None,
        *,
        show_locals=False,
        locals_max_length=10,
        locals_max_string=80,
        locals_max_depth=None,
        locals_hide_dunder=True,
    ):
        if trace is None:
            trace = self.extract("E", show_locals=show_locals)
        self.trace = trace
        self.show_locals = show_locals
        self.locals_max_length = locals_max_length
        self.locals_max_string = locals_max_string
        self.locals_max_depth = locals_max_depth
        self.locals_hide_dunder = locals_hide_dunder

    def render_settings(self):
        return (self.locals_max_length, self.locals_max_string, self.locals_max_depth)

    @classmethod
    def from_exception(
        cls,
        exc,
        *,
        show_locals=False,
        locals_max_length=10,
        locals_max_string=80,
        locals_max_depth=None,
        locals_hide_dunder=True,
    ):
        trace = cls.extract(
            exc,
            show_locals=show_locals,
            locals_max_length=locals_max_length,
            locals_max_string=locals_max_string,
            locals_max_depth=locals_max_depth,
            locals_hide_dunder=locals_hide_dunder,
        )
        return cls(
            trace,
            show_locals=show_locals,
            locals_max_length=locals_max_length,
            locals_max_string=locals_max_string,
            locals_max_depth=locals_max_depth,
            locals_hide_dunder=locals_hide_dunder,
        )

    @classmethod
    def extract(
        cls,
        exc,
        *,
        show_locals=False,
        locals_max_length=10,
        locals_max_string=80,
        locals_max_depth=None,
        locals_hide_dunder=True,
    ):
        if isinstance(exc, list):
            return cls.extract(
                exc[0],
                show_locals=show_locals,
                locals_max_length=locals_max_length,
                locals_hide_dunder=locals_hide_dunder,
            )
        return (exc, locals_max_length, locals_max_string, locals_max_depth, locals_hide_dunder)
""",
    }
    findings = audit_workspace(files)
    wrapper_msgs = {
        (f.path, f.start_line, f.evidence[0])
        for f in findings
        if f.detector == "delegating_wrapper_review"
    }
    sink_msgs = {
        (f.path, f.symbol, f.evidence[0])
        for f in findings
        if f.detector == "parameter_sink_review"
    }

    assert any("http1" in msg and p == "pkg/client.py" for p, _, msg in wrapper_msgs)
    for dropped in ("locals_max_length", "locals_max_string", "locals_max_depth", "locals_hide_dunder"):
        assert any(
            p == "pkg/tb.py" and "Traceback.__init__ calls Traceback.extract" in msg and dropped in msg
            for p, _, msg in wrapper_msgs
        )
    for dropped in ("locals_max_depth", "locals_max_string"):
        assert any(
            p == "pkg/tb.py" and "Traceback.extract calls Traceback.extract" in msg and dropped in msg
            for p, _, msg in wrapper_msgs
        )
    assert any(
        p == "pkg/tb.py" and sym == "Traceback.__init__" and "locals_hide_dunder" in msg
        for p, sym, msg in sink_msgs
    )


def test_cold_audit_respects_negative_controls():
    files = {
        "pkg/clean.py": """
def core(a, b=1, *, limit=10, strict=False, flush=False):
    return (a, b, limit, strict, flush)

def convenience(a, *, strict=False):
    # Deliberately narrower signature: should not flag omitted limit/flush
    return core(a, strict=strict)

def branch_forwarded(a, *, limit=10):
    if limit > 0:
        return core(a, limit=limit)
    return core(a)

def traverse(obj, root=True, depth=0):
    if depth < 2:
        return traverse(obj, depth=depth + 1)
    return (obj, root, depth)

def compat_print(msg, *, limit=10, flush=False):
    \"\"\"Write a message.

    Args:
        flush (bool): Ignored for compatibility; has no effect.
    \"\"\"
    return core(msg, limit=limit)
""",
        "pkg/errors.py": """
class LibraryError(Exception):
    pass

class ValidationError(LibraryError):
    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data
""",
    }
    findings = audit_workspace(files)
    assert findings == []


def test_cold_audit_detects_override_parameter_kind_conflict():
    files = {
        "pkg/base.py": """
class Base:
    def render(self, item, /, *, color=True):
        return (item, color)

class BrokenOverride(Base):
    def render(self, *, item, color=True):
        return (item, color)
""",
    }
    findings = audit_workspace(files)
    assert [(f.detector, f.symbol) for f in findings] == [
        ("override_signature_review", "BrokenOverride.render")
    ]


def test_cli_audit_repo_json_and_review_includes_callee_contract(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        "def send(url, timeout=5, retries=0, verify=True):\n"
        "    return (url, timeout, retries, verify)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "wrapper.py").write_text(
        "from pkg.core import send\n\n"
        "def fetch(url, timeout=5, retries=0, verify=True):\n"
        "    return send(url, timeout=timeout, verify=verify)\n",
        encoding="utf-8",
    )
    (root / "broken_legacy.py").write_text("print 'py2'\n", encoding="utf-8")

    # 1. Raw JSON check
    rc = cli.main_cli(["--root", str(root), "--audit-repo", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "audit-repo"
    assert out["skipped_unparsed_paths"] == ["broken_legacy.py"]
    assert [(f["detector"], f["symbol"]) for f in out["findings"]] == [
        ("delegating_wrapper_review", "fetch")
    ]

    # 2. Verify format_review_prompt attaches [callee contract] snippet when diff is empty
    prompts = []

    def fake_review_findings(**kwargs):
        prompt = format_review_prompt(
            findings=kwargs["findings"],
            anchor_for=kwargs["anchor_for"],
            after_sources=kwargs["after_sources"],
            diff_text=kwargs["diff_text"],
            changed_test_files=kwargs["changed_test_files"],
            subject=kwargs["subject"],
            cache=kwargs["cache"],
        )
        prompts.append(prompt)
        return ReviewResult(
            verdict="confirm_omission",
            reason="fetch drops retries when delegating to send.",
            backend="mock",
            model="mock-haiku",
            wall_ms=1.0,
            cost_usd=0.0,
        )

    monkeypatch.setattr(cli, "review_findings", fake_review_findings)
    rc_review = cli.main_cli(["--root", str(root), "--audit-repo", "--review", "--fail-on-findings", "--json"])
    assert rc_review == 1
    assert len(prompts) == 1
    assert "pkg/core.py :: send [callee contract]" in prompts[0]
    assert "def send(url, timeout=5, retries=0, verify=True):" in prompts[0]
