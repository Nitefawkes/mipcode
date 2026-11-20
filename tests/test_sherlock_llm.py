"""Model client: preflight, cost ledger, refusal handling (no network)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "sherlock_recall"))

llm = importlib.import_module("llm")

SONNET, HAIKU, OPUS = "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}


def resp(text='{"a": 1}', model=SONNET, stop="end_turn", in_tok=1000, out_tok=500,
         blocks=None, category=None, iterations=None):
    content = blocks if blocks is not None else [NS(type="text", text=text)]
    usage = NS(input_tokens=in_tok, output_tokens=out_tok, iterations=iterations)
    return NS(content=content, usage=usage, model=model, stop_reason=stop,
              stop_details=NS(category=category) if category else None)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []  # (surface, kwargs)
        outer = self
        def make(surface):
            def create(**kw):
                outer.calls.append((surface, kw))
                return outer.responses.pop(0)
            return NS(create=create)
        self.messages = make("plain")
        self.beta = NS(messages=make("beta"))
        self.models = NS(retrieve=self._retrieve)
        self.known = {}

    def _retrieve(self, mid):
        if mid not in self.known:
            raise KeyError(mid)
        return self.known[mid]

    @property
    def last_kwargs(self):
        return self.calls[-1][1]


def make(tmp_path, *responses, cap=15.0):
    ledger = llm.CostLedger(path=tmp_path / "ledger.json", cap_usd=cap)
    client = FakeClient(*responses)
    return client, ledger, llm.Reader(client, ledger, tmp_path / "cache")


def call(reader, model=SONNET, key="k1"):
    return reader.call(model, "low", "sys", "usr", SCHEMA, key)


def test_ok_response_parsed_and_charged(tmp_path):
    client, ledger, reader = make(tmp_path, resp())
    r = call(reader)
    assert r.status == "ok" and r.data == {"a": 1}
    assert (r.served_model, r.fallback_ran, r.in_tok, r.out_tok) == (SONNET, False, 1000, 500)
    pi, po = llm.PRICES[SONNET]
    assert ledger.spent_usd == pytest.approx(1000 * pi / 1e6 + 500 * po / 1e6)


def test_prices_values():
    assert llm.PRICES == {SONNET: (2.0, 10.0), HAIKU: (0.10, 0.50), OPUS: (4.0, 20.0)}


def test_refusal_recorded_not_raised(tmp_path):
    client, ledger, reader = make(tmp_path, resp(stop="refusal", category="cyber", blocks=[]))
    r = call(reader)
    assert (r.status, r.refusal_category) == ("refused", "cyber")
    assert r.data is None and ledger.spent_usd > 0


def test_malformed_json_and_max_tokens_are_invalid(tmp_path):
    client, ledger, reader = make(
        tmp_path,
        resp(text="not json"),
        resp(stop="max_tokens"),
        resp(text="[1, 2]"),
        resp(blocks=[NS(type="thinking", thinking="hm")]),
    )
    for i in range(4):
        r = call(reader, key=f"k{i}")
        assert r.status == "invalid" and r.data is None
    assert ledger.spent_usd > 0


def test_thinking_and_fallback_blocks_skipped(tmp_path):
    blocks = [NS(type="thinking", thinking="x"), NS(type="fallback"), NS(type="text", text='{"a": 2}')]
    client, ledger, reader = make(tmp_path, resp(blocks=blocks))
    r = call(reader)
    assert r.status == "ok" and r.data == {"a": 2} and r.fallback_ran is True


def test_fallback_served_model_recorded(tmp_path):
    client, ledger, reader = make(tmp_path, resp(model=OPUS, in_tok=1000, out_tok=1000))
    r = call(reader)
    assert r.fallback_ran is True and r.served_model == OPUS
    # charged at served model's price
    assert ledger.spent_usd == pytest.approx(1000 * 4 / 1e6 + 1000 * 20 / 1e6)


def test_fallback_detected_from_usage_iterations(tmp_path):
    client, ledger, reader = make(tmp_path, resp(iterations=[NS(type="fallback_message")]))
    assert call(reader).fallback_ran is True


def test_unknown_served_model_uses_requested_price(tmp_path):
    client, ledger, reader = make(tmp_path, resp(model="mystery", in_tok=1000, out_tok=0))
    r = call(reader)
    assert r.served_model == "mystery" and r.fallback_ran is True
    assert ledger.spent_usd == pytest.approx(1000 * 2 / 1e6)


def test_cache_hit_costs_nothing(tmp_path):
    client, ledger, reader = make(tmp_path, resp())
    first = call(reader)
    spent = ledger.spent_usd
    second = call(reader)
    assert len(client.calls) == 1 and ledger.spent_usd == spent
    assert second == first
    # a fresh Reader over the same cache dir also hits
    reader2 = llm.Reader(client, ledger, tmp_path / "cache")
    assert call(reader2) == first and len(client.calls) == 1


def test_cache_stores_refusals_too(tmp_path):
    client, ledger, reader = make(tmp_path, resp(stop="refusal", category="cyber", blocks=[]))
    call(reader)
    r = call(reader)
    assert r.status == "refused" and len(client.calls) == 1


def test_cap_raises_before_call(tmp_path):
    client, ledger, reader = make(tmp_path, resp(), cap=0.001)
    with pytest.raises(llm.BudgetExceeded):
        call(reader)
    assert client.calls == []


def test_ledger_persists_and_resumes(tmp_path):
    p = tmp_path / "ledger.json"
    a = llm.CostLedger(path=p, cap_usd=15.0)
    a.charge(SONNET, 1_000_000, 0)
    b = llm.CostLedger(path=p, cap_usd=15.0)
    assert b.spent_usd == pytest.approx(2.0)
    assert json.loads(p.read_text())["spent_usd"] == pytest.approx(2.0)
    assert b.would_exceed(13.5) and not b.would_exceed(12.0)


def test_no_forbidden_params_and_request_shape(tmp_path):
    client, ledger, reader = make(tmp_path, resp(), resp(model=HAIKU), resp(model=OPUS))
    call(reader, SONNET, "a")
    surface, kw = client.calls[-1]
    assert surface == "beta"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert kw["max_tokens"] == 4096 and kw["model"] == SONNET
    assert kw["system"] == "sys" and kw["messages"] == [{"role": "user", "content": "usr"}]
    assert kw["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    call(reader, HAIKU, "b")
    surface, kw = client.calls[-1]
    assert surface == "plain" and "betas" not in kw and "fallbacks" not in kw
    assert kw["output_config"]["effort"] == "low"
    call(reader, OPUS, "c")
    assert client.calls[-1][0] == "beta"
    for _, kw in client.calls:
        assert not {"temperature", "top_p", "top_k", "tool_choice", "tools", "thinking"} & set(kw)


def test_preflight_maps_display_names(tmp_path):
    c = FakeClient()
    c.known = {m: NS(id=m, display_name=f"Name {m}") for m in (SONNET, HAIKU)}
    assert llm.preflight(c, [SONNET, HAIKU]) == {SONNET: f"Name {SONNET}", HAIKU: f"Name {HAIKU}"}


def test_preflight_raises_on_missing_or_mismatched(tmp_path):
    c = FakeClient()
    c.known = {SONNET: NS(id="other", display_name="x")}
    with pytest.raises(RuntimeError, match=SONNET):
        llm.preflight(c, [SONNET])
    with pytest.raises(RuntimeError, match=OPUS):
        llm.preflight(c, [OPUS])


# ---------------------------------------------------------------- CLI transport (R19)

CLI_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def cli_out(structured=None, *, model=HAIKU, keys=None, is_error=False, stop="tool_use",
            api_status=None, result="", subtype="success", cost=0.00077, **extra):
    d = {"type": "result", "subtype": subtype, "is_error": is_error, "stop_reason": stop,
         "api_error_status": api_status, "num_turns": 2, "duration_ms": 1601,
         "total_cost_usd": cost,
         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1395,
                   "cache_read_input_tokens": 0, "output_tokens": 172},
         "modelUsage": {k: {"inputTokens": 2} for k in (keys if keys is not None else [model])},
         "result": result}
    if structured is not None:
        d["structured_output"] = structured
    d.update(extra)
    return d


class FakeRun:
    def __init__(self, *outs, rc=0, stderr=""):
        self.outs = list(outs)
        self.rc, self.stderr = rc, stderr
        self.calls = []
        self.snapshot = None

    def __call__(self, argv, stdin, cwd, timeout_s):
        if "--system-prompt-file" in argv:
            sys_path = argv[argv.index("--system-prompt-file") + 1]
            self.snapshot = dict(
                sys_text=Path(sys_path).read_text("utf-8"),
                files=sorted(p.name for p in Path(cwd).iterdir()),
                cwd_exists=Path(cwd).is_dir())
        self.calls.append(dict(argv=argv, stdin=stdin, cwd=cwd, timeout_s=timeout_s))
        out = self.outs.pop(0)
        return self.rc, out if isinstance(out, str) else json.dumps(out), self.stderr


def cli_reader(tmp_path, *outs, rc=0, stderr="", cap=15.0):
    run = FakeRun(*outs, rc=rc, stderr=stderr)
    client = llm.CliClient(exe="claude-fake", run=run)
    ledger = llm.CostLedger(tmp_path / "ledger.json", cap_usd=cap)
    return llm.Reader(client, ledger, tmp_path / "cache"), run, ledger


def test_cli_argv_flags_and_isolation(tmp_path):
    reader, run, _ = cli_reader(tmp_path, cli_out({"answer": "ok"}))
    reader.call(HAIKU, "low", "SYSTEM TEXT", "USER ÜTEXT", CLI_SCHEMA, "k1")
    c = run.calls[0]
    a = c["argv"]
    assert a[0] == "claude-fake" and a[1] == "-p"

    def val(flag):
        return a[a.index(flag) + 1]

    assert val("--model") == HAIKU and val("--effort") == "low"
    assert val("--output-format") == "json"
    assert val("--tools") == "" and val("--setting-sources") == ""
    assert json.loads(val("--json-schema")) == CLI_SCHEMA
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
                 "--exclude-dynamic-system-prompt-sections"):
        assert flag in a
    assert run.snapshot["sys_text"] == "SYSTEM TEXT"
    assert run.snapshot["cwd_exists"]
    assert run.snapshot["files"] == [Path(val("--system-prompt-file")).name]
    assert ROOT.resolve() not in Path(c["cwd"]).resolve().parents
    assert c["stdin"] == "USER ÜTEXT"


def test_cli_ok_structured_output(tmp_path):
    reader, _, ledger = cli_reader(tmp_path, cli_out({"answer": "ok"}))
    r = reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert r.status == "ok" and r.data == {"answer": "ok"}
    assert r.in_tok == 2 + 1395 + 0 and r.out_tok == 172
    assert r.served_model == HAIKU and r.fallback_ran is False
    assert ledger.spent_usd == pytest.approx(0.00077)
    assert json.loads(ledger.path.read_text("utf-8"))["spent_usd"] == pytest.approx(0.00077)
    raw = json.loads(next((tmp_path / "cache").glob("*.json")).read_text("utf-8"))["raw"]
    assert raw["served_keys"] == [HAIKU] and raw["total_cost_usd"] == 0.00077
    assert raw["duration_ms"] == 1601 and raw["num_turns"] == 2


def test_cli_refusal_recorded_and_cached(tmp_path):
    out = cli_out(None, stop="refusal", stop_details={"category": "cyber"})
    reader, run, ledger = cli_reader(tmp_path, out)
    r = reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert r.status == "refused" and r.refusal_category == "cyber" and r.data is None
    assert ledger.spent_usd > 0
    r2 = reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert r2 == r and len(run.calls) == 1


def test_cli_refusal_via_subtype_has_no_category(tmp_path):
    reader, _, _ = cli_reader(tmp_path, cli_out(None, subtype="error_refusal"))
    r = reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert r.status == "refused" and r.refusal_category is None


def test_cli_usage_limit_halts_uncached(tmp_path):
    limit = cli_out(None, is_error=True, api_status=429, result="boom")
    reader, run, ledger = cli_reader(tmp_path, limit, cli_out({"answer": "ok"}))
    with pytest.raises(llm.UsageLimitReached):
        reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert ledger.spent_usd == 0 and not list((tmp_path / "cache").glob("*.json"))
    assert reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1").status == "ok"
    assert len(run.calls) == 2


def test_cli_usage_limit_by_wording(tmp_path):
    reader, _, _ = cli_reader(tmp_path, cli_out(None, is_error=True, result="5-hour Usage Limit reached"))
    with pytest.raises(llm.UsageLimitReached):
        reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")


def test_cli_transport_error_halts(tmp_path):
    reader, _, ledger = cli_reader(tmp_path, "garbage not json", rc=1, stderr="crash")
    with pytest.raises(llm.TransportError):
        reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    reader2, _, _ = cli_reader(tmp_path / "b", cli_out(None, is_error=True, result="weird failure"))
    with pytest.raises(llm.TransportError) as ei:
        reader2.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert not isinstance(ei.value, llm.UsageLimitReached)
    reader3, _, _ = cli_reader(tmp_path / "c", "[1, 2]")
    with pytest.raises(llm.TransportError):
        reader3.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert ledger.spent_usd == 0
    assert issubclass(llm.BudgetExceeded, llm.RunHalted)


def test_cli_served_model_mismatch_flags_fallback(tmp_path):
    reader, _, _ = cli_reader(tmp_path, cli_out({"answer": "x"}, keys=[SONNET]))
    r = reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert r.fallback_ran is True and r.served_model == SONNET
    reader2, _, _ = cli_reader(tmp_path / "b", cli_out({"answer": "x"}, keys=[OPUS, HAIKU]))
    r2 = reader2.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert r2.fallback_ran is True and r2.served_model == f"{HAIKU}+{OPUS}"
    reader3, _, _ = cli_reader(tmp_path / "c", cli_out({"answer": "x"}, keys=[]))
    assert reader3.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1").served_model == "unknown"


def test_cli_missing_structured_output_invalid(tmp_path):
    reader, run, ledger = cli_reader(tmp_path, cli_out(None, result="plain prose"))
    r = reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert r.status == "invalid" and r.data is None and ledger.spent_usd > 0
    reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert len(run.calls) == 1


def test_cli_budget_cap_still_enforced(tmp_path):
    reader, run, _ = cli_reader(tmp_path, cli_out({"answer": "x"}), cap=0.0001)
    with pytest.raises(llm.BudgetExceeded):
        reader.call(HAIKU, "low", "s", "u", CLI_SCHEMA, "k1")
    assert run.calls == []


def test_cli_preflight_checks_model_usage(tmp_path):
    outs = [cli_out({"ok": True}, model=m) for m in (SONNET, HAIKU, OPUS)]
    run = FakeRun(*outs)
    client = llm.CliClient(exe="claude-fake", run=run)
    assert llm.preflight(client, [SONNET, HAIKU, OPUS]) == {SONNET: SONNET, HAIKU: HAIKU, OPUS: OPUS}
    assert run.calls[0]["stdin"] == "Return ok=true."
    assert run.calls[0]["argv"][run.calls[0]["argv"].index("--effort") + 1] == "low"
    bad = llm.CliClient(exe="claude-fake", run=FakeRun(cli_out({"ok": True}, keys=[OPUS])))
    with pytest.raises(RuntimeError):
        llm.preflight(bad, [SONNET])
    bad2 = llm.CliClient(exe="claude-fake", run=FakeRun(cli_out({"nope": 1}, model=SONNET)))
    with pytest.raises(RuntimeError):
        llm.preflight(bad2, [SONNET])


def test_cli_version():
    def run(argv, stdin, cwd, timeout_s):
        assert argv == ["claude-fake", "--version"]
        return 0, "2.1.322 (Claude Code)\n", ""
    assert llm.CliClient(exe="claude-fake", run=run).version() == "2.1.322 (Claude Code)"


def test_make_client_transport_choice(monkeypatch):
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    # CI runners have no claude CLI; resolve it to a stand-in path (nothing is executed).
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/fake/bin/" + name)
    assert isinstance(llm.make_client("cli"), llm.CliClient)
    assert "anthropic" not in sys.modules
    with pytest.raises(ValueError):
        llm.make_client("bogus")
