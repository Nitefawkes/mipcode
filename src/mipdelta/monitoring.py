from __future__ import annotations

import atexit
from itertools import islice
import json
import os
import sys
from pathlib import Path
from typing import Any

from .model import ObservedCallEdge


def _is_mock_object(obj: Any) -> bool:
    """Detect unittest.mock proxies without triggering dynamic __getattr__."""
    mod = getattr(type(obj), "__module__", "")
    return isinstance(mod, str) and mod.startswith("unittest.mock")


def _safe_str_attr(obj: Any, attr: str) -> str | None:
    try:
        val = getattr(obj, attr, None)
    except Exception:
        return None
    return val if isinstance(val, str) and val else None


def _instruction_line(code: Any, offset: int) -> int | None:
    """Return 1-based source line for bytecode offset using Python 3.12+ co_positions()."""
    if offset < 0:
        return None
    try:
        pos = next(islice(code.co_positions(), offset // 2, None), None)
    except Exception:
        return None
    if pos is None:
        return None
    line = pos[0]
    return line if isinstance(line, int) and line > 0 else None


class ScopedCallCollector:
    """Python 3.12+ sys.monitoring CALL collector with per-site DISABLE budgets.

    - Non-target code objects (pytest, pluggy, stdlib, third-party) immediately return
      `sys.monitoring.DISABLE`, removing bytecode traps on non-package frames.
    - Target code objects record `(caller_qualname, offset, callee_qualname, receiver_type)`:
      - `max_events_per_site = 1`: returns `DISABLE` on first execution of `(code, offset)`
        (`O(unique_executed_call_sites)`).
      - `max_events_per_site = 8`: samples up to 8 dynamic dispatches at `(code, offset)`
        to capture polymorphic receiver types before returning `DISABLE`.
      - `max_events_per_site = None`: unbounded observation on target frames.
    - Records positive observed edges only (`edge_label="observed_runtime"`). Unexecuted
      branches produce zero events and remain `unresolved_not_observed`.
    """

    def __init__(
        self,
        include_roots: list[str] | tuple[str, ...],
        *,
        max_events_per_site: int | None = 1,
        tool_name: str = "mipdelta-pytest-collector",
    ) -> None:
        roots: list[str] = []
        for p in include_roots:
            for candidate in (os.path.abspath(p), str(Path(p).resolve())):
                norm = candidate.replace("\\", "/").rstrip("/") + "/"
                if norm.lower() not in {r.lower() for r in roots}:
                    roots.append(norm)
        self.include_roots = tuple((r.lower(), len(r)) for r in roots)
        self.max_events_per_site = max_events_per_site
        self.tool_name = tool_name
        self.tool_id: int | None = None
        self.total_callbacks: int = 0
        self.target_callbacks: int = 0
        self.disabled_non_target_sites: int = 0
        self.disabled_target_sites: int = 0
        self._code_allowed_cache: dict[object, tuple[bool, str]] = {}
        self._file_allowed_cache: dict[str, tuple[bool, str]] = {}
        self._site_counts: dict[tuple[object, int], int] = {}
        self._edges: dict[tuple[str, str, int, str, str, str | None], ObservedCallEdge] = {}
        self._pid = os.getpid()

    def _classify_code(self, code: Any) -> tuple[bool, str]:
        cached = self._code_allowed_cache.get(code)
        if cached is not None:
            return cached
        raw_file = getattr(code, "co_filename", "") or ""
        file_cached = self._file_allowed_cache.get(raw_file)
        if file_cached is not None:
            self._code_allowed_cache[code] = file_cached
            return file_cached
        if not raw_file or raw_file.startswith("<"):
            res = (False, "")
            self._file_allowed_cache[raw_file] = res
            self._code_allowed_cache[code] = res
            return res
        resolved = os.path.abspath(raw_file).replace("\\", "/")
        lower = resolved.lower()
        for root_lower, root_len in self.include_roots:
            if lower.startswith(root_lower):
                res = (True, resolved[root_len:])
                self._file_allowed_cache[raw_file] = res
                self._code_allowed_cache[code] = res
                return res
        res = (False, "")
        self._file_allowed_cache[raw_file] = res
        self._code_allowed_cache[code] = res
        return res

    def _on_call(self, code: Any, offset: int, callable_obj: Any, arg0: Any) -> Any:
        self.total_callbacks += 1
        monitoring = sys.monitoring
        allowed, rel_path = self._classify_code(code)
        if not allowed:
            self.disabled_non_target_sites += 1
            return monitoring.DISABLE

        self.target_callbacks += 1
        if not _is_mock_object(callable_obj):
            callee_name = _safe_str_attr(callable_obj, "__name__")
            if not callee_name:
                callee_qual = _safe_str_attr(type(callable_obj), "__qualname__") or type(callable_obj).__name__
                callee_name = callee_qual.split(".")[-1]
            else:
                callee_qual = _safe_str_attr(callable_obj, "__qualname__") or callee_name

            # Determine receiver type when callable is a bound method or method_descriptor/function with instance arg0
            receiver_type: str | None = None
            try:
                self_obj = getattr(callable_obj, "__self__", None)
            except Exception:
                self_obj = None
            if self_obj is not None and not isinstance(self_obj, type) and not _is_mock_object(self_obj):
                receiver_type = _safe_str_attr(type(self_obj), "__qualname__") or type(self_obj).__name__
            elif (
                arg0 is not None
                and arg0 is not monitoring.MISSING
                and "." in callee_qual
                and not isinstance(arg0, type)
                and not _is_mock_object(arg0)
            ):
                receiver_type = _safe_str_attr(type(arg0), "__qualname__") or type(arg0).__name__

            caller_qual = _safe_str_attr(code, "co_qualname") or _safe_str_attr(code, "co_name") or "<unknown>"
            edge_key = (rel_path, caller_qual, offset, callee_qual, callee_name, receiver_type)
            if edge_key not in self._edges:
                self._edges[edge_key] = ObservedCallEdge(
                    caller_path=rel_path,
                    caller_qualname=caller_qual,
                    instruction_offset=offset,
                    callee_qualname=callee_qual,
                    callee_name=callee_name,
                    receiver_type=receiver_type,
                    pid=self._pid,
                    caller_line=_instruction_line(code, offset),
                )

        limit = self.max_events_per_site
        if limit is not None:
            if limit <= 1:
                self.disabled_target_sites += 1
                return monitoring.DISABLE
            skey = (code, offset)
            cnt = self._site_counts.get(skey, 0) + 1
            self._site_counts[skey] = cnt
            if cnt >= limit:
                self.disabled_target_sites += 1
                return monitoring.DISABLE
        return None

    def start(self) -> int:
        if sys.version_info < (3, 12) or not hasattr(sys, "monitoring"):
            raise RuntimeError("sys.monitoring requires Python 3.12+")
        monitoring = sys.monitoring
        available = [tid for tid in range(6) if monitoring.get_tool(tid) is None]
        if not available:
            raise RuntimeError("No free sys.monitoring tool IDs available")
        tid = available[0]
        monitoring.use_tool_id(tid, self.tool_name)
        monitoring.register_callback(tid, monitoring.events.CALL, self._on_call)
        monitoring.set_events(tid, monitoring.events.CALL)
        self.tool_id = tid
        return tid

    def stop(self) -> None:
        if self.tool_id is None:
            return
        monitoring = sys.monitoring
        tid = self.tool_id
        self.tool_id = None
        try:
            monitoring.set_events(tid, monitoring.events.NO_EVENTS)
            monitoring.register_callback(tid, monitoring.events.CALL, None)
            if monitoring.get_tool(tid) is not None:
                monitoring.free_tool_id(tid)
            monitoring.restart_events()
        except Exception:
            pass

    @property
    def observed_edges(self) -> list[ObservedCallEdge]:
        return list(self._edges.values())

    def to_shard_dict(self) -> dict[str, Any]:
        return {
            "pid": self._pid,
            "max_events_per_site": self.max_events_per_site,
            "total_callbacks": self.total_callbacks,
            "target_callbacks": self.target_callbacks,
            "disabled_non_target_sites": self.disabled_non_target_sites,
            "disabled_target_sites": self.disabled_target_sites,
            "unique_edge_count": len(self._edges),
            "edges": [
                {
                    "caller_path": e.caller_path,
                    "caller_qualname": e.caller_qualname,
                    "instruction_offset": e.instruction_offset,
                    "callee_qualname": e.callee_qualname,
                    "callee_name": e.callee_name,
                    "receiver_type": e.receiver_type,
                    "pid": e.pid,
                    "edge_label": e.edge_label,
                    "caller_line": e.caller_line,
                }
                for e in self._edges.values()
            ],
        }


def load_runtime_edges(runtime_dir: Path) -> list[ObservedCallEdge]:
    """Load deduplicated runtime edges from sys.monitoring edges-*.json shards."""
    if not runtime_dir.exists():
        return []
    seen: set[tuple[str, str, int, str, str, str | None]] = set()
    edges: list[ObservedCallEdge] = []
    for sf in sorted(runtime_dir.glob("edges-*.json")):
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for e in data.get("edges", []):
            key = (
                e["caller_path"],
                e["caller_qualname"],
                int(e["instruction_offset"]),
                e["callee_qualname"],
                e["callee_name"],
                e.get("receiver_type"),
            )
            if key in seen:
                continue
            seen.add(key)
            raw_line = e.get("caller_line")
            caller_line = int(raw_line) if isinstance(raw_line, int) else None
            edges.append(
                ObservedCallEdge(
                    caller_path=e["caller_path"],
                    caller_qualname=e["caller_qualname"],
                    instruction_offset=int(e["instruction_offset"]),
                    callee_qualname=e["callee_qualname"],
                    callee_name=e["callee_name"],
                    receiver_type=e.get("receiver_type"),
                    pid=int(e.get("pid", 0)),
                    edge_label=e.get("edge_label", "observed_runtime"),
                    caller_line=caller_line,
                )
            )
    return edges


def install_process_collector_from_env() -> ScopedCallCollector | None:
    """Entry point invoked by bootstrap sitecustomize.py so worker/child processes opt in cleanly."""
    raw_cfg = os.environ.get("MIPDELTA_MONITOR_CONFIG")
    if not raw_cfg:
        return None
    cfg = json.loads(raw_cfg)
    collector = ScopedCallCollector(
        include_roots=cfg["include_roots"],
        max_events_per_site=cfg.get("max_events_per_site", 1),
    )
    collector.start()
    shard_dir = Path(cfg["shard_dir"])

    def _flush() -> None:
        collector.stop()
        shard_dir.mkdir(parents=True, exist_ok=True)
        out_file = shard_dir / f"edges-{os.getpid()}.json"
        out_file.write_text(json.dumps(collector.to_shard_dict(), indent=2) + "\n", encoding="utf-8")

    atexit.register(_flush)
    return collector
