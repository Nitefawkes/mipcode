from __future__ import annotations

from mipdelta.delta import analyze_delta, audit_workspace


def test_not_implemented_override_stubs_suppressed_but_conditional_logic_fires():
    files = {
        "pkg/clients.py": """
class BaseClient:
    def rerank(self, query, documents, *, top_n=10):
        return (query, documents, top_n)

    async def arerank(self, query, documents, *, top_n=10):
        return (query, documents, top_n)

class BareStubClient(BaseClient):
    def rerank(self, *, query, documents, top_n=10):
        raise NotImplementedError("unsupported")

class DocstringStubClient(BaseClient):
    def rerank(self, *, query, documents, top_n=10):
        \"\"\"Not supported by this backend.\"\"\"
        raise NotImplementedError

    async def arerank(self, *, query, documents, top_n=10):
        \"\"\"Async stub.\"\"\"
        raise NotImplementedError("unsupported")

class ConditionalClient(BaseClient):
    def rerank(self, *, query, documents, top_n=10):
        if not documents:
            raise NotImplementedError("empty documents unsupported")
        return (query, documents[:top_n])
""",
    }
    findings = audit_workspace(files)
    override_hits = [
        (f.detector, f.symbol)
        for f in findings
        if f.detector == "override_signature_review"
    ]
    assert override_hits == [("override_signature_review", "ConditionalClient.rerank")]

    # Delta mode: adding a base parameter also suppresses NotImplementedError-only stubs
    # while still flagging conditional NotImplementedError overrides.
    before = {
        "pkg/clients.py": (
            "class Base:\n"
            "    def run(self, x):\n"
            "        return x\n\n"
            "class StubChild(Base):\n"
            "    def run(self, x):\n"
            "        '''Unsupported.'''\n"
            "        raise NotImplementedError('no')\n\n"
            "class RealChild(Base):\n"
            "    def run(self, x):\n"
            "        if x is None:\n"
            "            raise NotImplementedError('none')\n"
            "        return x + 1\n"
        )
    }
    after = {
        "pkg/clients.py": before["pkg/clients.py"].replace(
            "def run(self, x):\n        return x",
            "def run(self, x, *, strict=False):\n        return (x, strict)",
        )
    }
    delta_hits = [
        (f.detector, f.symbol)
        for f in analyze_delta(before, after)
        if f.detector == "override_signature_review"
    ]
    assert delta_hits == [("override_signature_review", "RealChild.run")]


def test_deprecation_sentinel_defaults_suppressed_while_plain_defaults_still_fire():
    files = {
        "pkg/settings.py": """
DEPRECATED = object()

class Sentinels:
    _DEPRECATED_ARG = object()

def apply_update(
    data,
    loader_identifier=None,
    is_secret="DeprecatedArgument",
    legacy_flag=DEPRECATED,
    old_mode=Sentinels._DEPRECATED_ARG,
    env=False,
    scroll_visible=True,
    retries=0,
    tag="",
):
    return (data, loader_identifier, is_secret, legacy_flag, old_mode, env, scroll_visible, retries, tag)

def update(
    data,
    loader_identifier=None,
    is_secret="DeprecatedArgument",
    legacy_flag=DEPRECATED,
    old_mode=Sentinels._DEPRECATED_ARG,
    env=False,
    scroll_visible=True,
    retries=0,
    tag="",
):
    return apply_update(data, loader_identifier=loader_identifier)
""",
    }
    findings = audit_workspace(files)
    wrapper_evidence = [
        f.evidence[0]
        for f in findings
        if f.detector == "delegating_wrapper_review" and f.symbol == "update"
    ]
    # Explicit deprecation sentinels must be suppressed.
    assert not any("is_secret" in ev for ev in wrapper_evidence)
    assert not any("legacy_flag" in ev for ev in wrapper_evidence)
    assert not any("old_mode" in ev for ev in wrapper_evidence)
    # Plain False / True / 0 / "" defaults must still fire.
    assert any("env" in ev for ev in wrapper_evidence)
    assert any("scroll_visible" in ev for ev in wrapper_evidence)
    assert any("retries" in ev for ev in wrapper_evidence)
    assert any("tag" in ev for ev in wrapper_evidence)


def test_leaf_callee_ignoring_param_suppressed_without_weakening_non_leaf_delegates():
    files = {
        "pkg/converters.py": """
_ESCAPE_TABLE = str.maketrans({"'": "\\\\'"})

def escape_string(value, mapping=None):
    return value.translate(_ESCAPE_TABLE)

def escape_str(value, mapping=None):
    return f"'{escape_string(value)}'"

class BaseRunner:
    def execute(self, cmd, *, strict=False):
        return (cmd, strict)

class SuperDelegate(BaseRunner):
    def execute(self, cmd, *, strict=False):
        return super().execute(cmd)

def run_via_super(runner: SuperDelegate, cmd, *, strict=False):
    return runner.execute(cmd)
""",
    }
    findings = audit_workspace(files)
    wrapper_hits = [
        (f.detector, f.symbol)
        for f in findings
        if f.detector == "delegating_wrapper_review"
    ]
    # Leaf converter ignores `mapping` and does not delegate -> suppressed.
    assert ("delegating_wrapper_review", "escape_str") not in wrapper_hits
    # SuperDelegate delegates via super().execute(...) -> non-leaf, must NOT suppress.
    assert ("delegating_wrapper_review", "run_via_super") in wrapper_hits


def test_two_hop_trogon_focus_pattern_keeps_both_hops_flagged():
    files = {
        "pkg/widgets.py": """
class Leaf:
    def focus(self, scroll_visible: bool = True):
        return scroll_visible

class Inner:
    child: Leaf

    def focus(self, scroll_visible: bool = True):
        return self.child.focus()

class Outer:
    inner: Inner

    def focus(self, scroll_visible: bool = True):
        return self.inner.focus()
""",
    }
    findings = audit_workspace(files)
    wrapper_hits = {
        (f.detector, f.symbol)
        for f in findings
        if f.detector == "delegating_wrapper_review"
        and any("scroll_visible" in ev for ev in f.evidence)
    }
    assert ("delegating_wrapper_review", "Outer.focus") in wrapper_hits
    assert ("delegating_wrapper_review", "Inner.focus") in wrapper_hits


def test_chonkie_pattern_unread_stored_batch_dynamic_still_fires_parameter_sink_review():
    files = {
        "pkg/handshake.py": """
class WeaviateHandshake:
    def __init__(
        self,
        batch_size: int = 100,
        batch_dynamic: bool = True,
    ):
        self.batch_size = batch_size
        self.batch_dynamic = batch_dynamic

    def write(self, collection, items):
        with collection.batch.fixed_size(batch_size=self.batch_size) as batch:
            for item in items:
                batch.add_object(item)
""",
    }
    findings = audit_workspace(files)
    sink_hits = [
        (f.detector, f.symbol, f.evidence[0])
        for f in findings
        if f.detector == "parameter_sink_review"
    ]
    assert any(
        sym == "WeaviateHandshake.__init__" and "batch_dynamic" in ev
        for _, sym, ev in sink_hits
    )
