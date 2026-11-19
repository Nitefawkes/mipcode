"""Model client for the Sherlock recall study: preflight, cost ledger, refusal handling.

This is the only module that talks to Claude models. The ``anthropic`` SDK is
imported lazily (inside ``make_client``/``main``) so tests run with fakes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / ".mipdelta" / "sherlock_cache" / "ledger.json"
CAP_USD = 15.0
MAX_TOKENS = 4096
FALLBACK_BETA = "server-side-fallback-2026-07-01"
HAIKU = "claude-haiku-5-5"
STUDY_MODELS = ["claude-sonnet-5-5", HAIKU, "claude-opus-5-5"]

# USD per million (input, output) tokens.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.0, 20.0),
}


class BudgetExceeded(Exception):
    pass


def _cost(model: str, in_tok: int, out_tok: int) -> float:
    pi, po = PRICES[model]
    return (in_tok * pi + out_tok * po) / 1e6


class CostLedger:
    def __init__(self, path: Path | None = None, cap_usd: float = CAP_USD):
        self.path = Path(path) if path is not None else DEFAULT_LEDGER
        self.cap_usd = cap_usd
        self.spent_usd = 0.0
        if self.path.exists():
            try:
                self.spent_usd = float(json.loads(self.path.read_text("utf-8")).get("spent_usd", 0.0))
            except (ValueError, OSError):
                raise RuntimeError(f"unreadable ledger at {self.path}; refusing to reset spend")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"spent_usd": self.spent_usd, "cap_usd": self.cap_usd}), "utf-8")

    def charge(self, model: str, in_tok: int, out_tok: int) -> None:
        self.spent_usd += _cost(model, in_tok, out_tok)
        self._save()

    def would_exceed(self, est_usd: float) -> bool:
        return self.spent_usd + est_usd > self.cap_usd


@dataclass
class CallResult:
    status: str  # ok | refused | invalid
    data: dict | None
    served_model: str
    fallback_ran: bool
    refusal_category: str | None
    in_tok: int
    out_tok: int


class Reader:
    def __init__(self, client, ledger: CostLedger, cache_dir: Path):
        self.client = client
        self.ledger = ledger
        self.cache_dir = Path(cache_dir)

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")

    def _create(self, model, effort, system, user, schema):
        kwargs = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if model == HAIKU:  # Haiku has no server-side fallback
            return self.client.messages.create(**kwargs)
        return self.client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")

    def call(self, model: str, effort: str, system: str, user: str, schema: dict, key: str) -> CallResult:
        cpath = self._cache_path(key)
        if cpath.exists():
            return CallResult(**json.loads(cpath.read_text("utf-8"))["result"])

        pi, po = PRICES[model]
        est = ((len(system) + len(user)) // 3) * pi / 1e6 + MAX_TOKENS * po / 1e6
        if self.ledger.would_exceed(est):
            raise BudgetExceeded(
                f"estimated ${est:.4f} would exceed cap ${self.ledger.cap_usd:.2f} "
                f"(spent ${self.ledger.spent_usd:.4f})")

        response = self._create(model, effort, system, user, schema)

        served = response.model
        in_tok = int(response.usage.input_tokens)
        out_tok = int(response.usage.output_tokens)
        self.ledger.charge(served if served in PRICES else model, in_tok, out_tok)

        content = list(getattr(response, "content", None) or [])
        iterations = getattr(response.usage, "iterations", None) or []
        fallback_ran = (
            any(getattr(b, "type", None) == "fallback" for b in content)
            or any(getattr(i, "type", None) == "fallback_message" for i in iterations)
            or served != model
        )

        status, data, category = "invalid", None, None
        if response.stop_reason == "refusal":
            status = "refused"
            category = getattr(getattr(response, "stop_details", None), "category", None)
        elif response.stop_reason != "max_tokens":
            block = next((b for b in content if getattr(b, "type", None) == "text"), None)
            if block is not None:
                try:
                    parsed = json.loads(block.text)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    status, data = "ok", parsed

        result = CallResult(status, data, served, fallback_ran, category, in_tok, out_tok)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps({
            "key": key,
            "result": asdict(result),
            "raw": {"model": served, "usage": {"input_tokens": in_tok, "output_tokens": out_tok}},
        }), "utf-8")
        return result


def preflight(client, model_ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for mid in model_ids:
        try:
            m = client.models.retrieve(mid)
        except Exception as exc:
            raise RuntimeError(f"preflight failed for model {mid}: {exc}") from exc
        if getattr(m, "id", None) != mid:
            raise RuntimeError(f"preflight failed for model {mid}: got id {getattr(m, 'id', None)!r}")
        out[mid] = m.display_name
    return out


def make_client():
    import anthropic  # lazy: only the real client needs the SDK
    return anthropic.Anthropic()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preflight", action="store_true")
    args = ap.parse_args(argv)
    if args.preflight:
        print(json.dumps(preflight(make_client(), STUDY_MODELS), indent=2))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
