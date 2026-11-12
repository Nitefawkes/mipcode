"""Arm L: the post-write advisory plus one completion check for obligations still open."""
import hashlib

import pytest

from mipdelta.gateway import ContainedGatewaySession

HTTP = "def dump_header(iterable):\n    return ','.join(iterable)\n"
HTTP_STRICT = "def dump_header(iterable, *, strict):\n    return ','.join(iterable) if strict else ''\n"
RESPONSE = "from .http import dump_header\n\ndef render(values):\n    return dump_header(values)\n"
RESPONSE_FIXED = RESPONSE.replace("dump_header(values)", "dump_header(values, strict=True)")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _session(tmp_path, arm: str) -> ContainedGatewaySession:
    root = tmp_path / arm / "source"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "http.py").write_bytes(HTTP.encode("utf-8"))
    (root / "pkg" / "response.py").write_bytes(RESPONSE.encode("utf-8"))
    return ContainedGatewaySession(root, arm, tmp_path / arm / "evidence")


def _write(session, path: str, before: str, after: str):
    return session.dispatch(
        {"tool": "write", "args": {"path": path, "expected_sha256": _sha(before), "content": after}}
    )


def test_tool_schemas_match_the_other_arms(tmp_path):
    assert _session(tmp_path, "L").tool_schemas() == _session(tmp_path, "A").tool_schemas()


def test_finalize_lists_open_obligations_once_then_closes(tmp_path):
    session = _session(tmp_path, "L")
    written = _write(session, "pkg/http.py", HTTP, HTTP_STRICT)
    assert "pkg/response.py:4 required_keyword_missing" in written["impact"]

    held = session.dispatch({"tool": "finalize", "args": {}})
    assert held["status"] == "OPEN_OBLIGATIONS"
    assert held["impact"].startswith("[mip-delta] 1 open: pkg/response.py:4 required_keyword_missing")
    assert not session.finalized and session.metrics["finalize_holds"] == 1

    closed = session.dispatch({"tool": "finalize", "args": {}})
    assert closed["status"] == "FINALIZED" and closed["changed_files"] == 1


def test_finalize_closes_directly_when_the_site_was_repaired(tmp_path):
    session = _session(tmp_path, "L")
    _write(session, "pkg/http.py", HTTP, HTTP_STRICT)
    repaired = _write(session, "pkg/response.py", RESPONSE, RESPONSE_FIXED)
    assert "impact" not in repaired

    closed = session.dispatch({"tool": "finalize", "args": {}})
    assert closed["status"] == "FINALIZED" and session.metrics["finalize_holds"] == 0


@pytest.mark.parametrize("arm", ["A", "B"])
def test_other_arms_finalize_without_a_completion_check(tmp_path, arm):
    session = _session(tmp_path, arm)
    _write(session, "pkg/http.py", HTTP, HTTP_STRICT)
    assert session.dispatch({"tool": "finalize", "args": {}})["status"] == "FINALIZED"
    assert "finalize_holds" not in session.metrics
