"""Contained reader gateway integrating DeltaSession as a zero-tool-call post-write sideband.

Key properties:
- Arm A (`arm="A"`) and Arm B (`arm="B"`) expose the exact same 7 tool schemas
  (`list`, `read`, `search`, `continue`, `write`, `test`, `finalize`). No `mip_*` tools exist.
- `read` and `search` never attach sideband footers (`0` extra wire bytes).
- `write` runs `DeltaSession.apply_write` in Arm B:
  - When `advisory_utf8_bytes == 0` (all ordinary local edits), `write` returns the
    exact same JSON payload as Arm A (`0` extra wire bytes).
  - When `advisory_utf8_bytes > 0` (contract-mutating write with candidate findings),
    `write` attaches `'impact': advisory_text` (`<= 250` UTF-8 bytes; total JSON wire
    delta `<= 300` bytes, never forcing single-frame paging).
- `test` optionally enables `ScopedCallCollector` (`disable_k8`) via `sitecustomize.py`
  in Arm B and ingests observed runtime call edges into `DeltaSession`.
- Arm L (`arm="L"`) is Arm B plus a completion check: the first `finalize` while
  obligations are still open returns them instead of closing the session. A second
  `finalize` closes it as is. The tool schemas are unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping

from .facts import parse_file
from .model import ObservedCallEdge, WriteDeltaResult
from .monitoring import load_runtime_edges
from .session import DeltaSession

MAX_IMPACT_WIRE_DELTA_BYTES = 350
GENERIC_CONTRACT_REMINDER = (
    "[reminder] Contract change detected in this file: before finalizing, check callers, "
    "delegating wrappers, subclass overrides, return unpackers, and try/except handlers across the repository."
)


def _has_local_contract_change(path: str, before_bytes: bytes | None, after_bytes: bytes | None) -> bool:
    """Single-file AST check (no repo index, stubs, or runtime trace) for Arm R generic reminder."""
    if before_bytes is None or after_bytes is None or not path.endswith(".py"):
        return False
    try:
        before_text = before_bytes.decode("utf-8")
        after_text = after_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    before_facts = parse_file(path, before_text)
    after_facts = parse_file(path, after_text)
    if before_facts.parse_error or after_facts.parse_error:
        return False
    for qualname, new_fn in after_facts.functions.items():
        old_fn = before_facts.functions.get(qualname)
        if old_fn is None:
            continue
        if (
            old_fn.parameters != new_fn.parameters
            or old_fn.return_tuple_arities != new_fn.return_tuple_arities
            or old_fn.has_explicit_none_return != new_fn.has_explicit_none_return
            or bool(new_fn.raised_exceptions - old_fn.raised_exceptions)
            or bool(new_fn.field_writes - old_fn.field_writes)
        ):
            return True
    return False



def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GatewayError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def apply_exact_edits(text: str, edits: list[dict[str, str]]) -> str:
    if not isinstance(edits, list) or not (1 <= len(edits) <= 50):
        raise GatewayError("INVALID_ARGUMENTS", "edits must contain 1 to 50 exact replacements")
    for edit in edits:
        if (
            not isinstance(edit, dict)
            or set(edit) != {"old_text", "new_text"}
            or not isinstance(edit["old_text"], str)
            or not edit["old_text"]
            or not isinstance(edit["new_text"], str)
        ):
            raise GatewayError("INVALID_ARGUMENTS", "Each edit requires nonempty old_text and string new_text")
        first = text.find(edit["old_text"])
        if first < 0 or text.find(edit["old_text"], first + 1) >= 0:
            raise GatewayError("PATCH_CONTEXT_NOT_UNIQUE", "old_text must match exactly once")
        text = text.replace(edit["old_text"], edit["new_text"], 1)
    return text


def gateway_tool_schemas(test_commands: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Identical tool catalog for both Arm A (control) and Arm B (post-write delta advisory)."""
    def schema(
        name: str,
        description: str,
        properties: dict[str, Any] | None = None,
        required: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        }

    string = {"type": "string"}
    return [
        schema(
            "list",
            "List contained source files with exact digests; filter by prefix and glob, and page using next_offset.",
            {
                "prefix": string,
                "glob": string,
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        ),
        schema(
            "read",
            "Read at most 500 physical LF/CRLF/CR lines (default 200) with exact file digest and numbered rows.",
            {
                "path": string,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ("path",),
        ),
        schema(
            "search",
            "Search contained source with rg or line-regex fallback.",
            {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 4096},
                "path": string,
                "glob": string,
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ("pattern",),
        ),
        schema(
            "continue",
            "Continue a lossless canonical JSON result; concatenate fragments and verify payload_sha256.",
            {"continuation_id": string},
            ("continuation_id",),
        ),
        schema(
            "write",
            "Atomic single-file compare-and-swap. Provide exactly one of content or edits.",
            {
                "path": string,
                "expected_sha256": {"type": ["string", "null"]},
                "content": {"type": ["string", "null"]},
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string", "minLength": 1},
                            "new_text": string,
                        },
                        "required": ["old_text", "new_text"],
                        "additionalProperties": False,
                    },
                },
            },
            ("path", "expected_sha256"),
        ),
        schema(
            "test",
            "Run one frozen named test command through the isolated runner.",
            {"command": {"type": "string", "enum": sorted(test_commands)} if test_commands else string},
            ("command",),
        ),
        schema(
            "finalize",
            "Freeze exact final source changes and close this reader session.",
        ),
    ]


class DeltaImpactSideband:
    """Post-write contract-delta sideband backed by DeltaSession.

    - `prepare_read` and `prepare_search` are no-ops (0 bytes added).
    - `before_write` verifies custody of the workspace Python files against `DeltaSession`.
    - `after_write` runs `DeltaSession.apply_write` on `.py` writes and attaches `'impact'`
      iff findings are non-empty and within the bounded wire budget.
    """

    def __init__(
        self,
        root: Path,
        *,
        stub_texts: Mapping[str, str] | None = None,
        runtime_edges: list[ObservedCallEdge] | None = None,
        max_advisory_bytes: int = 250,
        enable: Iterable[str] = (),
    ) -> None:
        self.root = Path(root).resolve(strict=True)
        self.max_advisory_bytes = max_advisory_bytes
        py_files = self._scan_python_files()
        effective_stubs = dict(stub_texts) if stub_texts is not None else self._scan_stub_files()
        self.delta_session = DeltaSession.from_workspace(
            py_files,
            stub_texts=effective_stubs,
            runtime_edges=runtime_edges,
            max_advisory_bytes=max_advisory_bytes,
            enable=enable,
        )
        self.write_events: list[WriteDeltaResult] = []
        self.ingested_runtime_edges_count: int = len(runtime_edges or ())

    def _scan_python_files(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in sorted(self.root.rglob("*.py")):
            if p.is_symlink() or not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(self.root).as_posix()
            try:
                out[rel] = p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
        return out

    def _scan_stub_files(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in sorted(self.root.rglob("*.pyi")):
            if p.is_symlink() or not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(self.root).as_posix()
            try:
                out[rel] = p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
        return out

    def prepare_read(self, payload: dict[str, Any], max_response_bytes: int) -> tuple[dict[str, Any], None]:
        return payload, None

    def prepare_search(self, payload: dict[str, Any], max_response_bytes: int) -> tuple[dict[str, Any], None]:
        return payload, None

    def before_write(self, path: str, before_bytes: bytes | None, after_bytes: bytes | None) -> dict[str, Any]:
        norm = path.replace("\\", "/")
        if not norm.endswith(".py"):
            return {"is_python": False, "path": norm}
        current_py = self._scan_python_files()
        custody = self.delta_session.verify_custody(current_py)
        if not custody.clean:
            return {"is_python": True, "path": norm, "custody_clean": False}
        if before_bytes is not None:
            expected_digest = self.delta_session.digest_for(norm)
            actual_digest = sha256_bytes(before_bytes)
            if expected_digest is not None and expected_digest != actual_digest:
                return {"is_python": True, "path": norm, "custody_clean": False}
        return {
            "is_python": True,
            "path": norm,
            "custody_clean": True,
            "after_text": after_bytes.decode("utf-8") if after_bytes is not None else None,
        }

    def after_write(
        self,
        plan: dict[str, Any],
        mip_status: str,
        payload: dict[str, Any],
        max_response_bytes: int,
    ) -> dict[str, Any]:
        if not isinstance(plan, dict) or not plan.get("is_python"):
            return payload
        if mip_status != "READY" or not plan.get("custody_clean"):
            return self._attach_bounded(
                payload,
                "Impact unavailable; source and write status remain authoritative.",
                max_response_bytes,
            )
        after_text = plan.get("after_text")
        norm = plan["path"]
        if after_text is None:
            # File deleted
            self.delta_session.index.remove_file(norm)
            self.delta_session._active_digests.pop(norm, None)
            self.delta_session._active_facts.pop(norm, None)
            return payload

        res = self.delta_session.apply_write(norm, after_text)
        self.write_events.append(res)
        if not res.advisory_text:
            return payload
        return self._attach_bounded(payload, res.advisory_text, max_response_bytes)

    def ingest_runtime_edges(self, edges: list[ObservedCallEdge]) -> int:
        if not edges:
            return 0
        self.delta_session.add_runtime_edges(edges)
        self.ingested_runtime_edges_count += len(edges)
        return len(edges)

    def after_test(self, payload: dict[str, Any], max_response_bytes: int) -> dict[str, Any]:
        res = self.delta_session.evaluate_new_runtime_findings()
        if res is None or not res.advisory_text:
            return payload
        self.write_events.append(res)
        return self._attach_bounded(payload, res.advisory_text, max_response_bytes)

    @staticmethod
    def _attach_bounded(payload: dict[str, Any], impact_text: str, max_response_bytes: int) -> dict[str, Any]:
        base_len = len(json_bytes(payload))
        proposed = {**payload, "impact": impact_text}
        new_len = len(json_bytes(proposed))
        if new_len - base_len > MAX_IMPACT_WIRE_DELTA_BYTES:
            trimmed = impact_text.encode("utf-8")[:220].decode("utf-8", errors="ignore") + "..."
            proposed = {**payload, "impact": trimmed}
            new_len = len(json_bytes(proposed))
        if base_len <= max_response_bytes < new_len:
            return payload
        return proposed


class ContainedGatewaySession:
    """Self-contained episode gateway supporting Arm A (control) and Arm B (DeltaImpactSideband)."""

    def __init__(
        self,
        root: Path,
        arm: str,
        evidence_dir: Path,
        *,
        stub_texts: Mapping[str, str] | None = None,
        test_commands: Mapping[str, list[str]] | None = None,
        enable_test_monitoring: bool = True,
        enable: Iterable[str] = (),
        max_calls: int = 80,
        max_response_bytes: int = 16384,
        max_visible_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if arm not in ("A", "B", "R", "L"):
            raise ValueError("arm must be 'A', 'B', 'R', or 'L'")
        self.root = Path(root).resolve(strict=True)
        self.arm = arm
        self.evidence_dir = Path(evidence_dir).resolve()
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.evidence_dir / "ledger.jsonl"
        if self.ledger_path.exists():
            raise ValueError("Evidence directory already contains ledger.jsonl")
        self.ledger_path.touch(exist_ok=False)

        self.test_commands = dict(test_commands or {})
        self.enable_test_monitoring = enable_test_monitoring and arm in ("B", "L")
        self.max_calls = max_calls
        self.max_response_bytes = max_response_bytes
        self.max_visible_bytes = max_visible_bytes
        self.generation = 0
        self.finalized = False
        self.patch_sha256: str | None = None
        self.metrics = {
            "calls": 0,
            "errors": 0,
            "visible_bytes": 0,
            "request_bytes": 0,
            "test_calls": 0,
            "write_calls": 0,
            "advisory_writes": 0,
            "advisory_utf8_bytes": 0,
        }
        self._baseline = self._snapshot()
        self._expected = {name: sha256_bytes(data) for name, data in self._baseline.items()}
        self.impact: DeltaImpactSideband | None = (
            DeltaImpactSideband(self.root, stub_texts=stub_texts, enable=enable)
            if arm in ("B", "L")
            else None
        )
        self._finalize_held = False
        if arm == "L":
            self.metrics["finalize_holds"] = 0
        self._reminder_written_paths: set[str] = set()
        self._reminder_contract_changed: bool = False
        self._reminder_test_emitted: bool = False

    def tool_schemas(self) -> list[dict[str, Any]]:
        return gateway_tool_schemas(tuple(self.test_commands.keys()))

    def _resolve_contained(self, rel_path: str) -> Path:
        if not isinstance(rel_path, str) or not rel_path or "\\" in rel_path or ":" in rel_path:
            raise GatewayError("PATH_DENIED", "Use a contained relative slash-separated path")
        parts = rel_path.split("/")
        if rel_path.startswith("/") or any(p in ("", ".", "..", ".git") for p in parts):
            raise GatewayError("PATH_DENIED", "Invalid relative path")
        target = self.root.joinpath(*parts).resolve()
        if not target.is_relative_to(self.root):
            raise GatewayError("PATH_DENIED", "Path escaped source root")
        return target

    def _snapshot(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_symlink() or not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(self.root).as_posix()
            out[rel] = p.read_bytes()
        return out

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        self.metrics["calls"] += 1
        req_bytes = len(json_bytes(request))
        self.metrics["request_bytes"] += req_bytes
        tool = request.get("tool")
        args = request.get("args", {})
        try:
            if self.metrics["calls"] > self.max_calls:
                raise GatewayError("CALL_LIMIT", "Episode call allowance exhausted")
            if not isinstance(tool, str):
                raise GatewayError("INVALID_ARGUMENTS", "tool must be a string")
            result = self._execute(tool, args)
        except GatewayError as exc:
            result = {"status": "ERROR", "code": exc.code, "message": str(exc)}
            self.metrics["errors"] += 1
        except Exception as exc:
            result = {"status": "ERROR", "code": type(exc).__name__, "message": "Request could not be completed"}
            self.metrics["errors"] += 1

        resp_raw = json_bytes(result)
        self.metrics["visible_bytes"] += len(resp_raw)
        record = {
            "sequence": self.metrics["calls"],
            "request": request,
            "request_utf8_bytes": req_bytes,
            "response": result,
            "response_utf8_bytes": len(resp_raw),
            "elapsed_seconds": time.perf_counter() - started,
            "generation": self.generation,
            "metrics": dict(self.metrics),
        }
        with self.ledger_path.open("ab") as stream:
            stream.write(json_bytes(record) + b"\n")
        return result

    def _execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.finalized and tool != "finalize":
            raise GatewayError("FINALIZED", "Session is closed after finalize")

        if tool == "list":
            snap = self._snapshot()
            prefix = args.get("prefix")
            files = [
                {"path": k, "sha256": sha256_bytes(v), "bytes": len(v)}
                for k, v in sorted(snap.items())
                if not prefix or k.startswith(prefix.rstrip("/") + "/")
            ]
            return {"status": "OK", "generation": self.generation, "files": files, "total_files": len(files)}

        if tool == "read":
            path = args["path"]
            target = self._resolve_contained(path)
            data = target.read_bytes()
            text = data.decode("utf-8")
            lines = text.splitlines()
            start = int(args.get("start_line", 1))
            max_lines = int(args.get("max_lines", 200))
            end = int(args.get("end_line", start + max_lines - 1))
            selected = lines[start - 1 : end]
            payload = {
                "status": "OK",
                "path": path,
                "sha256": sha256_bytes(data),
                "content": "\n".join(selected),
                "lines": [{"line": start + i, "text": ln} for i, ln in enumerate(selected)],
                "total_lines": len(lines),
                "start_line": start,
                "end_line": min(end, len(lines)),
                "next_line": end + 1 if end < len(lines) else None,
                "generation": self.generation,
            }
            if self.impact is not None:
                payload, _ = self.impact.prepare_read(payload, self.max_response_bytes)
            return payload

        if tool == "search":
            pattern = args["pattern"]
            rx = re.compile(pattern)
            target_path = args.get("path")
            snap = self._snapshot()
            matches = []
            for rel, raw in sorted(snap.items()):
                if target_path and rel != target_path:
                    continue
                try:
                    txt = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for idx, line in enumerate(txt.splitlines(), 1):
                    if rx.search(line):
                        matches.append({"path": rel, "line": idx, "text": line})
            limit = int(args.get("limit", 100))
            offset = int(args.get("offset", 0))
            payload = {
                "status": "OK",
                "matches": matches[offset : offset + limit],
                "total_matches": len(matches),
                "next_offset": offset + limit if offset + limit < len(matches) else None,
                "generation": self.generation,
            }
            if self.impact is not None:
                payload, _ = self.impact.prepare_search(payload, self.max_response_bytes)
            return payload

        if tool == "write":
            path = args["path"]
            expected = args["expected_sha256"]
            if ("content" in args) == ("edits" in args):
                raise GatewayError("INVALID_ARGUMENTS", "Provide exactly one of content or edits")
            target = self._resolve_contained(path)
            current = target.read_bytes() if target.exists() else None
            actual = sha256_bytes(current) if current is not None else None
            if expected != actual:
                raise GatewayError("CAS_MISMATCH", "Current file digest does not match expected_sha256")
            if "edits" in args:
                if current is None:
                    raise GatewayError("INVALID_ARGUMENTS", "Exact edits require an existing file")
                content = apply_exact_edits(current.decode("utf-8"), args["edits"])
            else:
                content = args.get("content")

            after_bytes = content.encode("utf-8") if content is not None else None
            plan = (
                self.impact.before_write(path, current, after_bytes)
                if self.impact is not None
                else None
            )
            if after_bytes is None:
                if target.exists():
                    target.unlink()
                new_digest = None
                self._expected.pop(path, None)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(after_bytes)
                new_digest = sha256_bytes(after_bytes)
                self._expected[path] = new_digest

            self.generation += 1
            self.metrics["write_calls"] += 1
            payload: dict[str, Any] = {
                "status": "OK",
                "path": path,
                "sha256": new_digest,
                "generation": self.generation,
                "atomicity": "SINGLE_FILE_ONLY",
            }
            if self.impact is not None and plan is not None:
                payload = self.impact.after_write(plan, "READY", payload, self.max_response_bytes)
                if "impact" in payload:
                    self.metrics["advisory_writes"] += 1
                    self.metrics["advisory_utf8_bytes"] += len(payload["impact"].encode("utf-8"))
            elif self.arm == "R":
                is_first_file_edit = path not in self._reminder_written_paths
                self._reminder_written_paths.add(path)
                if is_first_file_edit and _has_local_contract_change(path, current, after_bytes):
                    self._reminder_contract_changed = True
                    payload = DeltaImpactSideband._attach_bounded(
                        payload, GENERIC_CONTRACT_REMINDER, self.max_response_bytes
                    )
                    if "impact" in payload:
                        self.metrics["advisory_writes"] += 1
                        self.metrics["advisory_utf8_bytes"] += len(payload["impact"].encode("utf-8"))
            return payload

        if tool == "test":
            cmd_name = args.get("command")
            if cmd_name not in self.test_commands:
                raise GatewayError("TEST_COMMAND_DENIED", f"Unknown test command: {cmd_name}")
            self.metrics["test_calls"] += 1
            argv = self.test_commands[cmd_name]
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            shard_dir: Path | None = None
            bootstrap_dir: Path | None = None
            src_root = Path(__file__).resolve().parents[1]
            py_paths = [str(self.root)]
            if self.enable_test_monitoring and self.impact is not None:
                shard_dir = self.evidence_dir / f"monitor_shards_call_{self.metrics['calls']}"
                bootstrap_dir = self.evidence_dir / "monitor_bootstrap"
                bootstrap_dir.mkdir(parents=True, exist_ok=True)
                (bootstrap_dir / "sitecustomize.py").write_text(
                    "import os\n"
                    "if os.environ.get('MIPDELTA_MONITOR_CONFIG'):\n"
                    "    from mipdelta.monitoring import install_process_collector_from_env\n"
                    "    install_process_collector_from_env()\n",
                    encoding="utf-8",
                )
                py_paths = [str(bootstrap_dir), str(src_root), str(self.root)]
                env["MIPDELTA_MONITOR_CONFIG"] = json.dumps({
                    "include_roots": [str(self.root)],
                    "max_events_per_site": 8,
                    "shard_dir": str(shard_dir),
                })
            if env.get("PYTHONPATH"):
                py_paths.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(py_paths)

            proc = subprocess.run(
                argv,
                cwd=str(self.root),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )
            payload = {
                "status": "OK" if proc.returncode == 0 else "FAIL",
                "command": cmd_name,
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-4096:],
                "stderr": proc.stderr[-4096:],
                "generation": self.generation,
            }
            if shard_dir is not None and shard_dir.exists() and self.impact is not None:
                edges = load_runtime_edges(shard_dir)
                self.impact.ingest_runtime_edges(edges)
                payload = self.impact.after_test(payload, self.max_response_bytes)
                if "impact" in payload:
                    self.metrics["advisory_writes"] += 1
                    self.metrics["advisory_utf8_bytes"] += len(payload["impact"].encode("utf-8"))
            elif (
                self.arm == "R"
                and self._reminder_contract_changed
                and not self._reminder_test_emitted
                and len(self._reminder_written_paths) <= 1
            ):
                self._reminder_test_emitted = True
                payload = DeltaImpactSideband._attach_bounded(
                    payload, GENERIC_CONTRACT_REMINDER, self.max_response_bytes
                )
                if "impact" in payload:
                    self.metrics["advisory_writes"] += 1
                    self.metrics["advisory_utf8_bytes"] += len(payload["impact"].encode("utf-8"))

            return payload

        if tool == "finalize":
            return self.finalize()

        raise GatewayError("TOOL_DENIED", f"Unknown tool: {tool}")

    def finalize(self) -> dict[str, Any]:
        if self.finalized:
            return {"status": "FINALIZED", "patch_sha256": self.patch_sha256}
        if self.arm == "L" and self.impact is not None and not self._finalize_held:
            open_text = self.impact.delta_session.finalize_advisory()
            if open_text:
                # Listed once. The agent decides whether the remaining sites are intentional.
                self._finalize_held = True
                self.metrics["finalize_holds"] += 1
                self.metrics["advisory_utf8_bytes"] += len(open_text.encode("utf-8"))
                return {
                    "status": "OPEN_OBLIGATIONS",
                    "impact": open_text,
                    "detail": "Review these sites, then call finalize again to close the session.",
                }
        current = self._snapshot()
        now = {name: sha256_bytes(data) for name, data in current.items()}
        if now != self._expected:
            raise GatewayError("OUT_OF_BAND_CHANGE", "Source changed outside the gateway writer")
        changes = []
        for name in sorted(set(current) | set(self._baseline)):
            before = self._baseline.get(name)
            after = current.get(name)
            if before == after:
                continue
            changes.append({
                "path": name,
                "before_sha256": sha256_bytes(before) if before is not None else None,
                "after_sha256": sha256_bytes(after) if after is not None else None,
                "before_base64": base64.b64encode(before).decode("ascii") if before is not None else None,
                "content_base64": base64.b64encode(after).decode("ascii") if after is not None else None,
            })
        artifact = {
            "schema": "mipcode.tier2.final-patch.v2",
            "arm": self.arm,
            "generation": self.generation,
            "changes": changes,
            "baseline_sha256": {n: sha256_bytes(d) for n, d in self._baseline.items()},
            "final_sha256": now,
            "metrics_at_freeze": dict(self.metrics),
        }
        raw = json_bytes(artifact)
        patch_path = self.evidence_dir / "final-patch.json"
        patch_path.write_bytes(raw)
        self.patch_sha256 = sha256_bytes(raw)
        self.finalized = True
        return {
            "status": "FINALIZED",
            "patch_sha256": self.patch_sha256,
            "changed_files": len(changes),
        }



class StdioMcpGatewayServer:
    """Newline-delimited JSON-RPC 2.0 stdio MCP server wrapping ContainedGatewaySession."""

    def __init__(self, session: ContainedGatewaySession, evidence_dir: Path) -> None:
        self.session = session
        self.schemas = session.tool_schemas()
        self.evidence_dir = evidence_dir.resolve()
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.inbound_bytes = 0
        self.outbound_bytes = 0
        self.initialized = False
        self.transport_log = self.evidence_dir / "mcp-transport.jsonl"

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "params must be object"}}
        if method == "initialize":
            self.initialized = True
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "tier2-contained-gateway", "version": "2.0.0"},
                },
            }
        if req_id is None:
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self.schemas}}
        if method in ("resources/list", "resources/templates/list"):
            key = "resources" if method == "resources/list" else "resourceTemplates"
            return {"jsonrpc": "2.0", "id": req_id, "result": {key: []}}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            result = self.session.dispatch({"tool": name, "args": arguments})
            visible = json_bytes(result).decode("utf-8")
            is_err = result.get("status") not in ("OK", "FINALIZED")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": visible}],
                    "isError": is_err,
                },
            }
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not allowed: {method}"}}

    def serve(self, stdin_stream: Any, stdout_stream: Any) -> None:
        with self.transport_log.open("ab") as log_fp:
            while True:
                if self.session.finalized:
                    break
                raw = stdin_stream.readline()
                if not raw:
                    break
                self.inbound_bytes += len(raw)
                log_fp.write(json_bytes({"dir": "in", "bytes": len(raw), "utf8": raw.decode("utf-8", errors="replace")}) + b"\n")
                log_fp.flush()
                try:
                    req = json.loads(raw)
                    resp = self.handle(req) if isinstance(req, dict) else None
                except json.JSONDecodeError:
                    resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
                if resp is None:
                    continue
                out_raw = json_bytes(resp) + b"\n"
                self.outbound_bytes += len(out_raw)
                log_fp.write(json_bytes({"dir": "out", "bytes": len(out_raw), "utf8": out_raw.decode("utf-8")}) + b"\n")
                log_fp.flush()
                stdout_stream.write(out_raw)
                stdout_stream.flush()
        if not self.session.finalized:
            try:
                self.session.finalize()
            except GatewayError:
                pass
        summary = {
            "inbound_transport_bytes": self.inbound_bytes,
            "outbound_transport_bytes": self.outbound_bytes,
            "schema_utf8_bytes": len(json_bytes(self.schemas)),
            "finalized": self.session.finalized,
            "patch_sha256": self.session.patch_sha256,
            "metrics": dict(self.session.metrics),
        }
        (self.evidence_dir / "mcp-summary.json").write_bytes(json_bytes(summary))


def main_mcp_cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Serve ContainedGatewaySession over stdio MCP.")
    parser.add_argument("--episode-config", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = json.loads(args.episode_config.read_text(encoding="utf-8"))
    session = ContainedGatewaySession(
        Path(cfg["root"]),
        cfg["arm"],
        Path(cfg["evidence"]) / "session",
        test_commands=cfg.get("test_commands"),
        enable_test_monitoring=(cfg["arm"] in ("B", "L")),
    )
    server = StdioMcpGatewayServer(session, Path(cfg["evidence"]) / "transport")
    server.serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_mcp_cli())
