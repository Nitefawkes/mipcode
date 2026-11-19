"""Model client for the Sherlock recall study: preflight, cost ledger, refusal handling.

This is the only module that talks to Claude models. The default transport is the
Claude Code CLI in print mode (``CliClient``, subscription billed, Ruling R19); the
``anthropic`` SDK transport is kept as an option and imported lazily (inside
``make_client``) so the CLI path and the tests never import it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


class RunHalted(Exception):
    """The run must stop cleanly; the halting call is neither cached nor charged."""


class BudgetExceeded(RunHalted):
    pass


class UsageLimitReached(RunHalted):
    pass


class TransportError(RunHalted):
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

    def charge_usd(self, usd: float) -> None:
        self.spent_usd += float(usd)
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

_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|quota", re.IGNORECASE)
_CLI_FLAGS = ["--output-format", "json", "--tools", "", "--no-session-persistence",
              "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
              "--exclude-dynamic-system-prompt-sections"]


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill the child and its descendants (run_recall's helper, inlined: importing
    run_recall pulls in the frozen engine, which the CLI path must not need)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    else:
        p.kill()


def _default_run(argv: list[str], stdin: str, cwd: str, timeout_s: int) -> tuple[int, str, str]:
    """Popen with stdin and a tree-kill on timeout."""
    p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        out, err = p.communicate(input=stdin, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(p)
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise TransportError(f"claude -p timed out after {timeout_s}s") from exc
    return p.returncode, out, err


class CliClient:
    """``claude -p`` transport (subscription). One isolated process per call."""

    def __init__(self, exe: str | None = None, run=None, timeout_s: int = 600):
        self.exe = exe or shutil.which("claude.exe") or shutil.which("claude")
        if not self.exe:
            raise TransportError("claude CLI not found on PATH")
        self.run = run or _default_run
        self.timeout_s = timeout_s
        self.last_stderr = ""

    def invoke(self, model: str, effort: str, system: str, user: str, schema: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            sys_file = Path(tmp) / "system_prompt.txt"
            sys_file.write_text(system, "utf-8")
            argv = [self.exe, "-p", "--model", model, "--effort", effort, *_CLI_FLAGS,
                    "--system-prompt-file", str(sys_file), "--json-schema", json.dumps(schema)]
            rc, out, err = self.run(argv, user, tmp, self.timeout_s)
        self.last_stderr = err or ""
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            raise TransportError(
                f"claude -p exit {rc}, no JSON object on stdout; stderr: {(err or '').strip()[:300]}")
        if rc != 0 and not data.get("is_error"):
            raise TransportError(f"claude -p exit {rc}; stderr: {(err or '').strip()[:300]}")
        return data

    def version(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, err = self.run([self.exe, "--version"], "", tmp, 60)
        if rc != 0:
            raise TransportError(f"claude --version exit {rc}: {(err or '').strip()[:300]}")
        return out.strip()


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

        if isinstance(self.client, CliClient):
            return self._call_cli(model, effort, system, user, schema, key, cpath)

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

    def _call_cli(self, model, effort, system, user, schema, key, cpath) -> CallResult:
        d = self.client.invoke(model, effort, system, user, schema)
        if d.get("is_error"):
            text = f"{d.get('result') or ''}\n{self.client.last_stderr}"
            if d.get("api_error_status") == 429 or _LIMIT_RE.search(text):
                raise UsageLimitReached(text.strip()[:300] or "usage limit reached")
            raise TransportError(f"claude -p reported an error: {text.strip()[:300]}")

        keys = set((d.get("modelUsage") or {}).keys())
        if keys == {model}:
            served, fallback_ran = model, False
        else:
            served, fallback_ran = ("+".join(sorted(keys)) or "unknown"), True

        usage = d.get("usage") or {}
        in_tok = sum(int(usage.get(k) or 0) for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        out_tok = int(usage.get("output_tokens") or 0)
        cost = float(d.get("total_cost_usd") or 0.0)
        self.ledger.charge_usd(cost)

        status, data, category = "invalid", None, None
        if (d.get("stop_reason") == "refusal" or "refusal" in str(d.get("subtype") or "")
                or "refusal" in str(d.get("terminal_reason") or "")):
            status = "refused"
            details = d.get("stop_details")
            if isinstance(details, dict):
                category = details.get("category")
        elif isinstance(d.get("structured_output"), dict):
            status, data = "ok", d["structured_output"]

        result = CallResult(status, data, served, fallback_ran, category, in_tok, out_tok)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cpath.write_text(json.dumps({
            "key": key,
            "result": asdict(result),
            "raw": {"model": served, "served_keys": sorted(keys), "total_cost_usd": cost,
                    "duration_ms": d.get("duration_ms"), "num_turns": d.get("num_turns"),
                    "usage": {"input_tokens": in_tok, "output_tokens": out_tok}},
        }), "utf-8")
        return result


_PREFLIGHT_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                     "required": ["ok"], "additionalProperties": False}


def preflight(client, model_ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(client, CliClient):
        for mid in model_ids:
            try:
                d = client.invoke(mid, "low", "Reply only via the schema.", "Return ok=true.",
                                  _PREFLIGHT_SCHEMA)
            except TransportError as exc:
                raise RuntimeError(f"preflight failed for model {mid}: {exc}") from exc
            served = set((d.get("modelUsage") or {}).keys())
            so = d.get("structured_output")
            if (d.get("is_error") or served != {mid} or not isinstance(so, dict)
                    or not isinstance(so.get("ok"), bool)):
                raise RuntimeError(
                    f"preflight failed for model {mid}: modelUsage={sorted(served)} "
                    f"is_error={d.get('is_error')} structured_output={so!r}")
            out[mid] = mid
        return out
    for mid in model_ids:
        try:
            m = client.models.retrieve(mid)
        except Exception as exc:
            raise RuntimeError(f"preflight failed for model {mid}: {exc}") from exc
        if getattr(m, "id", None) != mid:
            raise RuntimeError(f"preflight failed for model {mid}: got id {getattr(m, 'id', None)!r}")
        out[mid] = m.display_name
    return out


def make_client(transport: str = "cli"):
    if transport == "cli":
        return CliClient()
    if transport == "sdk":
        import anthropic  # lazy: only the SDK transport needs it
        return anthropic.Anthropic()
    raise ValueError(f"unknown transport {transport!r} (expected 'cli' or 'sdk')")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--transport", choices=["cli", "sdk"], default="cli")
    args = ap.parse_args(argv)
    if args.preflight:
        client = make_client(args.transport)
        models = preflight(client, STUDY_MODELS)
        if isinstance(client, CliClient):
            print(json.dumps({"cli_version": client.version(), "models": models}, indent=2))
        else:
            print(json.dumps(models, indent=2))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
