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
