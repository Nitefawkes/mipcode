"""Experiment 2 Part A (Track A2): Edge Coverage + 4-Arm 30-Mutation Recall Benchmark.

Evaluates monitoring.py (--run-pytest) and resolve.py [rt-obs] WITHOUT modifying
D:\\Projects\\mipcode\\src\\mipdelta\\:
  - Arm 1: Static AST only (mip-delta analyze_delta without runtime_edges)
  - Arm 2a: Static AST + [rt-obs] as-shipped shards & unmodified resolve.py
  - Arm 2b: Diagnostic [rt-obs] in harness (MRO + callee_qualname lookup fix +
            Mock-safe/diag_safe shards for rich and self-monitoring-safe mipcode)
  - Arm 3: Installed mypy inside each repo's isolated uv venv against gitless
            shadow copies (subtracting baseline errors)
  - Targeted pytest verification on B01..B15 in sequential try/finally blocks

Usage:
  python run_part_a_recall.py --preflight        # Fast (<3s) in-memory check of all 30 mutations
  python run_part_a_recall.py --collect-diag     # Collect diag_safe shards for rich & mipcode
  python run_part_a_recall.py                    # Full benchmark -> results/audit_rt_ledger/part_a_recall.json
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

ROOT = Path(r"D:\Projects\mipcode")
sys.path.insert(0, str(ROOT / "src"))

from mipdelta.cli import _load_runtime_edges
from mipdelta.delta import analyze_delta
from mipdelta.facts import parse_file
from mipdelta.model import FileFacts, FunctionFact, ObservedCallEdge
import mipdelta.resolve as resolve_mod

WORK_ROOT = Path(r"C:\tmp\exp2-work")
SHARDS_ROOT = Path(r"C:\tmp\exp2-shards")
VENV_ROOT = Path(r"C:\tmp\exp2-venvs")
MUT_WORK_ROOT = Path(r"C:\tmp\exp2-mut-work")
OUT_PATH = ROOT / "results" / "audit_rt_ledger" / "part_a_recall.json"


@dataclass(frozen=True)
class RepoMeta:
    name: str
    pkg_rel: str
    mypy_target: str
    mypy_path_rel: str


REPOS: dict[str, RepoMeta] = {
    "httpx": RepoMeta("httpx", "httpx", "httpx", "."),
    "rich": RepoMeta("rich", "rich", "rich", "."),
    "marshmallow": RepoMeta("marshmallow", "src/marshmallow", "src/marshmallow", "src"),
    "mipcode": RepoMeta("mipcode", "src/mipdelta", "src/mipdelta", "src"),
}


@dataclass(frozen=True)
class MutationCase:
    mid: str
    group: str  # "breaking" or "optional_wrapper"
    repo: str
    target_file: str
    callee_symbol: str
    expected_caller_file: str
    expected_caller_symbol: str
    callee_name: str
    old_text: str
    new_text: str
    pytest_target: str | None
    description: str


MUTATIONS: tuple[MutationCase, ...] = (
    # ---------------- 15 Breaking Signature Mutations (B01..B15) ----------------
    MutationCase(
        mid="B01",
        group="breaking",
        repo="httpx",
        target_file="httpx/_urls.py",
        callee_symbol="URL.join",
        expected_caller_file="httpx/_client.py",
        expected_caller_symbol="BaseClient._redirect_url",
        callee_name="join",
        old_text="    def join(self, url: URL | str) -> URL:",
        new_text="    def join(self, url: URL | str, *, strict: bool) -> URL:",
        pytest_target="tests/client/test_redirects.py",
        description="Add required kw-only strict: bool to URL.join",
    ),
    MutationCase(
        mid="B02",
        group="breaking",
        repo="httpx",
        target_file="httpx/_client.py",
        callee_symbol="Client.request",
        expected_caller_file="httpx/_api.py",
        expected_caller_symbol="request",
        callee_name="request",
        old_text=(
            "    def request(\n"
            "        self,\n"
            "        method: str,\n"
            "        url: URL | str,\n"
            "        *,\n"
            "        content: RequestContent | None = None,\n"
            "        data: RequestData | None = None,\n"
            "        files: RequestFiles | None = None,\n"
            "        json: typing.Any | None = None,\n"
            "        params: QueryParamTypes | None = None,"
        ),
        new_text=(
            "    def request(\n"
            "        self,\n"
            "        method: str,\n"
            "        url: URL | str,\n"
            "        *,\n"
            "        content: RequestContent | None = None,\n"
            "        data: RequestData | None = None,\n"
            "        files: RequestFiles | None = None,\n"
            "        json: typing.Any | None = None,\n"
            "        query_params: QueryParamTypes | None = None,"
        ),
        pytest_target="tests/test_api.py",
        description="Rename params -> query_params on Client.request",
    ),
    MutationCase(
        mid="B03",
        group="breaking",
        repo="httpx",
        target_file="httpx/_transports/default.py",
        callee_symbol="HTTPTransport.handle_request",
        expected_caller_file="httpx/_client.py",
        expected_caller_symbol="Client._send_single_request",
        callee_name="handle_request",
        old_text=(
            "    def handle_request(\n"
            "        self,\n"
            "        request: Request,\n"
            "    ) -> Response:"
        ),
        new_text=(
            "    def handle_request(\n"
            "        self,\n"
            "        request: Request,\n"
            "        *,\n"
            "        trace_id: str,\n"
            "    ) -> Response:"
        ),
        pytest_target="tests/client/test_client.py",
        description="Add required kw-only trace_id: str to HTTPTransport.handle_request",
    ),
    MutationCase(
        mid="B04",
        group="breaking",
        repo="httpx",
        target_file="httpx/_models.py",
        callee_symbol="Response.read",
        expected_caller_file="httpx/_client.py",
        expected_caller_symbol="Client.send",
        callee_name="read",
        old_text=(
            "    def read(self) -> bytes:\n"
            '        """\n'
            "        Read and return the response content.\n"
            '        """'
        ),
        new_text=(
            "    def read(self, *, decode: bool) -> bytes:\n"
            '        """\n'
            "        Read and return the response content.\n"
            '        """'
        ),
        pytest_target="tests/client/test_client.py",
        description="Add required kw-only decode: bool to Response.read",
    ),
    MutationCase(
        mid="B05",
        group="breaking",
        repo="rich",
        target_file="rich/syntax.py",
        callee_symbol="Syntax.stylize_range",
        expected_caller_file="rich/traceback.py",
        expected_caller_symbol="Traceback._render_stack",
        callee_name="stylize_range",
        old_text=(
            "    def stylize_range(\n"
            "        self,\n"
            "        style: StyleType,\n"
            "        start: SyntaxPosition,\n"
            "        end: SyntaxPosition,\n"
            "        style_before: bool = False,\n"
            "    ) -> None:"
        ),
        new_text=(
            "    def stylize_range(\n"
            "        self,\n"
            "        range_style: StyleType,\n"
            "        start: SyntaxPosition,\n"
            "        end: SyntaxPosition,\n"
            "        style_before: bool = False,\n"
            "    ) -> None:"
        ),
        pytest_target="tests/test_traceback.py",
        description="Rename style -> range_style on Syntax.stylize_range",
    ),
    MutationCase(
        mid="B06",
        group="breaking",
        repo="rich",
        target_file="rich/console.py",
        callee_symbol="Console.print_json",
        expected_caller_file="rich/__init__.py",
        expected_caller_symbol="print_json",
        callee_name="print_json",
        old_text=(
            "    def print_json(\n"
            "        self,\n"
            "        json: str | None = None,\n"
            "        *,\n"
            "        data: Any = None,\n"
            "        indent: None | int | str = 2,"
        ),
        new_text=(
            "    def print_json(\n"
            "        self,\n"
            "        json: str | None = None,\n"
            "        *,\n"
            "        data: Any = None,\n"
            "        indent_width: None | int | str = 2,"
        ),
        pytest_target="tests/test_rich_print.py",
        description="Rename indent -> indent_width on Console.print_json",
    ),
    MutationCase(
        mid="B07",
        group="breaking",
        repo="rich",
        target_file="rich/live.py",
        callee_symbol="Live.update",
        expected_caller_file="rich/status.py",
        expected_caller_symbol="Status.update",
        callee_name="update",
        old_text="    def update(self, renderable: RenderableType, *, refresh: bool = False) -> None:",
        new_text="    def update(self, renderable: RenderableType, *, force_refresh: bool = False) -> None:",
        pytest_target="tests/test_status.py",
        description="Rename refresh -> force_refresh on Live.update",
    ),
    MutationCase(
        mid="B08",
        group="breaking",
        repo="rich",
        target_file="rich/table.py",
        callee_symbol="Table.add_column",
        expected_caller_file="rich/scope.py",
        expected_caller_symbol="render_scope",
        callee_name="add_column",
        old_text=(
            "        style: StyleType | None = None,\n"
            '        justify: "JustifyMethod" = "left",\n'
            '        vertical: "VerticalAlignMethod" = "top",'
        ),
        new_text=(
            "        style: StyleType | None = None,\n"
            '        text_justify: "JustifyMethod" = "left",\n'
            '        vertical: "VerticalAlignMethod" = "top",'
        ),
        pytest_target="tests/test_traceback.py",
        description="Rename justify -> text_justify on Table.add_column",
    ),
    MutationCase(
        mid="B09",
        group="breaking",
        repo="marshmallow",
        target_file="src/marshmallow/schema.py",
        callee_symbol="Schema.dump",
        expected_caller_file="src/marshmallow/fields.py",
        expected_caller_symbol="Nested._serialize",
        callee_name="dump",
        old_text="    def dump(self, obj: typing.Any, *, many: bool | None = None):",
        new_text="    def dump(self, obj: typing.Any, *, as_many: bool | None = None):",
        pytest_target="tests/test_fields.py",
        description="Rename many -> as_many on Schema.dump",
    ),
    MutationCase(
        mid="B10",
        group="breaking",
        repo="marshmallow",
        target_file="src/marshmallow/fields.py",
        callee_symbol="Nested._load",
        expected_caller_file="src/marshmallow/fields.py",
        expected_caller_symbol="Pluck._deserialize",
        callee_name="_load",
        old_text=(
            "    def _load(\n"
            "        self,\n"
            "        value: typing.Any,\n"
            "        partial: bool | types.StrSequenceOrSet | None = None,  # noqa: FBT001\n"
            "    ):"
        ),
        new_text=(
            "    def _load(\n"
            "        self,\n"
            "        value: typing.Any,\n"
            "        partial_fields: bool | types.StrSequenceOrSet | None = None,  # noqa: FBT001\n"
            "    ):"
        ),
        pytest_target="tests/test_fields.py",
        description="Rename partial -> partial_fields on Nested._load",
    ),
    MutationCase(
        mid="B11",
        group="breaking",
        repo="marshmallow",
        target_file="src/marshmallow/error_store.py",
        callee_symbol="ErrorStore.store_error",
        expected_caller_file="src/marshmallow/schema.py",
        expected_caller_symbol="Schema._call_and_store",
        callee_name="store_error",
        old_text="    def store_error(self, messages, field_name=SCHEMA, index=None):",
        new_text="    def store_error(self, messages, field_name=SCHEMA, item_index=None):",
        pytest_target="tests/test_deserialization.py",
        description="Rename index -> item_index on ErrorStore.store_error",
    ),
    MutationCase(
        mid="B12",
        group="breaking",
        repo="marshmallow",
        target_file="src/marshmallow/fields.py",
        callee_symbol="Field.make_error",
        expected_caller_file="src/marshmallow/fields.py",
        expected_caller_symbol="Nested._test_collection",
        callee_name="make_error",
        old_text="    def make_error(self, key: str, **kwargs) -> ValidationError:",
        new_text="    def make_error(self, key: str, *, code: str, **kwargs) -> ValidationError:",
        pytest_target="tests/test_fields.py",
        description="Add required kw-only code: str to Field.make_error",
    ),
    MutationCase(
        mid="B13",
        group="breaking",
        repo="mipcode",
        target_file="src/mipdelta/cache.py",
        callee_symbol="FactCache.load",
        expected_caller_file="src/mipdelta/cli.py",
        expected_caller_symbol="_collect_git_findings.anchors.anchor_for",
        callee_name="load",
        old_text="    def load(self, path: str, text: str, *, prior_facts: FileFacts | None = None) -> FileFacts:",
        new_text="    def load(self, path: str, text: str, strict: bool, *, prior_facts: FileFacts | None = None) -> FileFacts:",
        pytest_target="tests/test_cli_analysis.py",
        description="Add required strict: bool to FactCache.load",
    ),
    MutationCase(
        mid="B14",
        group="breaking",
        repo="mipcode",
        target_file="src/mipdelta/ledger.py",
        callee_symbol="ObligationLedger.unreported_at_stop",
        expected_caller_file="src/mipdelta/cli.py",
        expected_caller_symbol="_run_hook",
        callee_name="unreported_at_stop",
        old_text="    def unreported_at_stop(self) -> tuple[Obligation, ...]:",
        new_text="    def unreported_at_stop(self, *, strict: bool) -> tuple[Obligation, ...]:",
        pytest_target="tests/test_cli_ledger_hooks.py",
        description="Add required kw-only strict: bool to ObligationLedger.unreported_at_stop",
    ),
    MutationCase(
        mid="B15",
        group="breaking",
        repo="mipcode",
        target_file="src/mipdelta/ledger.py",
        callee_symbol="ObligationLedger.waive",
        expected_caller_file="src/mipdelta/session.py",
        expected_caller_symbol="DeltaSession.waive",
        callee_name="waive",
        old_text='    def waive(self, identifier: str, reason: str = "") -> bool:',
        new_text='    def waive(self, identifier: str, reason: str = "", *, actor: str) -> bool:',
        pytest_target="tests/test_v2_session_stubs_monitoring.py",
        description="Add required kw-only actor: str to ObligationLedger.waive",
    ),
    # -------- 15 Optional Delegating-Wrapper Omissions (O01..O15) --------
    MutationCase(
        mid="O01",
        group="optional_wrapper",
        repo="httpx",
        target_file="httpx/_client.py",
        callee_symbol="Client.request",
        expected_caller_file="httpx/_api.py",
        expected_caller_symbol="request",
        callee_name="request",
        old_text=(
            "    def request(\n"
            "        self,\n"
            "        method: str,\n"
            "        url: URL | str,\n"
            "        *,\n"
            "        content: RequestContent | None = None,\n"
            "        data: RequestData | None = None,\n"
            "        files: RequestFiles | None = None,\n"
            "        json: typing.Any | None = None,\n"
            "        params: QueryParamTypes | None = None,\n"
            "        headers: HeaderTypes | None = None,\n"
            "        cookies: CookieTypes | None = None,\n"
            "        auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "        follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        timeout: TimeoutTypes | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        extensions: RequestExtensions | None = None,\n"
            "    ) -> Response:"
        ),
        new_text=(
            "    def request(\n"
            "        self,\n"
            "        method: str,\n"
            "        url: URL | str,\n"
            "        *,\n"
            "        content: RequestContent | None = None,\n"
            "        data: RequestData | None = None,\n"
            "        files: RequestFiles | None = None,\n"
            "        json: typing.Any | None = None,\n"
            "        params: QueryParamTypes | None = None,\n"
            "        headers: HeaderTypes | None = None,\n"
            "        cookies: CookieTypes | None = None,\n"
            "        auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "        follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        timeout: TimeoutTypes | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        extensions: RequestExtensions | None = None,\n"
            "        trust_env: bool = True,\n"
            "    ) -> Response:"
        ),
        pytest_target=None,
        description="Add optional trust_env to Client.request (httpx.request omits forwarding)",
    ),
    MutationCase(
        mid="O02",
        group="optional_wrapper",
        repo="httpx",
        target_file="httpx/_client.py",
        callee_symbol="Client.stream",
        expected_caller_file="httpx/_api.py",
        expected_caller_symbol="stream",
        callee_name="stream",
        old_text=(
            "    @contextmanager\n"
            "    def stream(\n"
            "        self,\n"
            "        method: str,\n"
            "        url: URL | str,\n"
            "        *,\n"
            "        content: RequestContent | None = None,\n"
            "        data: RequestData | None = None,\n"
            "        files: RequestFiles | None = None,\n"
            "        json: typing.Any | None = None,\n"
            "        params: QueryParamTypes | None = None,\n"
            "        headers: HeaderTypes | None = None,\n"
            "        cookies: CookieTypes | None = None,\n"
            "        auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "        follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        timeout: TimeoutTypes | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        extensions: RequestExtensions | None = None,\n"
            "    ) -> typing.Iterator[Response]:"
        ),
        new_text=(
            "    @contextmanager\n"
            "    def stream(\n"
            "        self,\n"
            "        method: str,\n"
            "        url: URL | str,\n"
            "        *,\n"
            "        content: RequestContent | None = None,\n"
            "        data: RequestData | None = None,\n"
            "        files: RequestFiles | None = None,\n"
            "        json: typing.Any | None = None,\n"
            "        params: QueryParamTypes | None = None,\n"
            "        headers: HeaderTypes | None = None,\n"
            "        cookies: CookieTypes | None = None,\n"
            "        auth: AuthTypes | UseClientDefault | None = USE_CLIENT_DEFAULT,\n"
            "        follow_redirects: bool | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        timeout: TimeoutTypes | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        extensions: RequestExtensions | None = None,\n"
            "        trust_env: bool = True,\n"
            "    ) -> typing.Iterator[Response]:"
        ),
        pytest_target=None,
        description="Add optional trust_env to Client.stream (httpx.stream omits forwarding)",
    ),
    MutationCase(
        mid="O03",
        group="optional_wrapper",
        repo="httpx",
        target_file="httpx/_client.py",
        callee_symbol="BaseClient.build_request",
        expected_caller_file="httpx/_client.py",
        expected_caller_symbol="Client.request",
        callee_name="build_request",
        old_text=(
            "        cookies: CookieTypes | None = None,\n"
            "        timeout: TimeoutTypes | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        extensions: RequestExtensions | None = None,\n"
            "    ) -> Request:"
        ),
        new_text=(
            "        cookies: CookieTypes | None = None,\n"
            "        timeout: TimeoutTypes | UseClientDefault = USE_CLIENT_DEFAULT,\n"
            "        follow_redirects: bool = True,\n"
            "        extensions: RequestExtensions | None = None,\n"
            "    ) -> Request:"
        ),
        pytest_target=None,
        description="Add optional follow_redirects to BaseClient.build_request (Client.request omits forwarding)",
    ),
    MutationCase(
        mid="O04",
        group="optional_wrapper",
        repo="httpx",
        target_file="httpx/_transports/default.py",
        callee_symbol="ResponseStream.close",
        expected_caller_file="httpx/_client.py",
        expected_caller_symbol="BoundSyncStream.close",
        callee_name="close",
        old_text=(
            "    def close(self) -> None:\n"
            '        if hasattr(self._httpcore_stream, "close"):'
        ),
        new_text=(
            "    def close(self, *, force: bool = False) -> None:\n"
            '        if hasattr(self._httpcore_stream, "close"):'
        ),
        pytest_target=None,
        description="Add optional force to ResponseStream.close (BoundSyncStream.close omits forwarding)",
    ),
    MutationCase(
        mid="O05",
        group="optional_wrapper",
        repo="rich",
        target_file="rich/console.py",
        callee_symbol="Console.print",
        expected_caller_file="rich/__init__.py",
        expected_caller_symbol="print",
        callee_name="print",
        old_text=(
            "        soft_wrap: bool | None = None,\n"
            "        new_line_start: bool = False,\n"
            "    ) -> None:\n"
            '        """Print to the console.'
        ),
        new_text=(
            "        soft_wrap: bool | None = None,\n"
            "        new_line_start: bool = False,\n"
            "        flush: bool = False,\n"
            "    ) -> None:\n"
            '        """Print to the console.'
        ),
        pytest_target=None,
        description="Add optional flush to Console.print (rich.print omits forwarding)",
    ),
    MutationCase(
        mid="O06",
        group="optional_wrapper",
        repo="rich",
        target_file="rich/table.py",
        callee_symbol="Table.add_column",
        expected_caller_file="rich/_log_render.py",
        expected_caller_symbol="LogRender.__call__",
        callee_name="add_column",
        old_text=(
            "        ratio: int | None = None,\n"
            "        no_wrap: bool = False,\n"
            "    ) -> None:\n"
            '        """Add a column to the table.'
        ),
        new_text=(
            "        ratio: int | None = None,\n"
            "        no_wrap: bool = False,\n"
            "        line_no: int | None = None,\n"
            "    ) -> None:\n"
            '        """Add a column to the table.'
        ),
        pytest_target=None,
        description="Add optional line_no to Table.add_column (LogRender.__call__ omits forwarding)",
    ),
    MutationCase(
        mid="O07",
        group="optional_wrapper",
        repo="rich",
        target_file="rich/palette.py",
        callee_symbol="Palette.match",
        expected_caller_file="rich/color.py",
        expected_caller_symbol="Color.downgrade",
        callee_name="match",
        old_text="    def match(self, color: tuple[int, int, int]) -> int:",
        new_text="    def match(self, color: tuple[int, int, int], *, system: int | None = None) -> int:",
        pytest_target=None,
        description="Add optional system to Palette.match (Color.downgrade omits forwarding)",
    ),
    MutationCase(
        mid="O08",
        group="optional_wrapper",
        repo="rich",
        target_file="rich/containers.py",
        callee_symbol="Lines.justify",
        expected_caller_file="rich/text.py",
        expected_caller_symbol="Text.wrap",
        callee_name="justify",
        old_text=(
            "    def justify(\n"
            "        self,\n"
            '        console: "Console",\n'
            "        width: int,\n"
            '        justify: "JustifyMethod" = "left",\n'
            '        overflow: "OverflowMethod" = "fold",\n'
            "    ) -> None:"
        ),
        new_text=(
            "    def justify(\n"
            "        self,\n"
            '        console: "Console",\n'
            "        width: int,\n"
            '        justify: "JustifyMethod" = "left",\n'
            '        overflow: "OverflowMethod" = "fold",\n'
            "        no_wrap: bool = False,\n"
            "    ) -> None:"
        ),
        pytest_target=None,
        description="Add optional no_wrap to Lines.justify (Text.wrap omits forwarding)",
    ),
    MutationCase(
        mid="O09",
        group="optional_wrapper",
        repo="marshmallow",
        target_file="src/marshmallow/fields.py",
        callee_symbol="Nested._load",
        expected_caller_file="src/marshmallow/fields.py",
        expected_caller_symbol="Pluck._deserialize",
        callee_name="_load",
        old_text=(
            "    def _load(\n"
            "        self,\n"
            "        value: typing.Any,\n"
            "        partial: bool | types.StrSequenceOrSet | None = None,  # noqa: FBT001\n"
            "    ):"
        ),
        new_text=(
            "    def _load(\n"
            "        self,\n"
            "        value: typing.Any,\n"
            "        partial: bool | types.StrSequenceOrSet | None = None,  # noqa: FBT001\n"
            "        *,\n"
            "        attr: str | None = None,\n"
            "    ):"
        ),
        pytest_target=None,
        description="Add optional attr to Nested._load (Pluck._deserialize omits forwarding)",
    ),
    MutationCase(
        mid="O10",
        group="optional_wrapper",
        repo="marshmallow",
        target_file="src/marshmallow/fields.py",
        callee_symbol="Nested._test_collection",
        expected_caller_file="src/marshmallow/fields.py",
        expected_caller_symbol="Pluck._deserialize",
        callee_name="_test_collection",
        old_text="    def _test_collection(self, value: typing.Any) -> None:",
        new_text="    def _test_collection(self, value: typing.Any, *, partial: bool | None = None) -> None:",
        pytest_target=None,
        description="Add optional partial to Nested._test_collection (Pluck._deserialize omits forwarding)",
    ),
    MutationCase(
        mid="O11",
        group="optional_wrapper",
        repo="marshmallow",
        target_file="src/marshmallow/schema.py",
        callee_symbol="Schema.dump",
        expected_caller_file="src/marshmallow/fields.py",
        expected_caller_symbol="Nested._serialize",
        callee_name="dump",
        old_text="    def dump(self, obj: typing.Any, *, many: bool | None = None):",
        new_text="    def dump(self, obj: typing.Any, *, many: bool | None = None, attr: str | None = None):",
        pytest_target=None,
        description="Add optional attr to Schema.dump (Nested._serialize omits forwarding)",
    ),
    MutationCase(
        mid="O12",
        group="optional_wrapper",
        repo="marshmallow",
        target_file="src/marshmallow/fields.py",
        callee_symbol="Field.serialize",
        expected_caller_file="src/marshmallow/schema.py",
        expected_caller_symbol="Schema._serialize",
        callee_name="serialize",
        old_text=(
            "        accessor: (\n"
            "            typing.Callable[[typing.Any, str, typing.Any], typing.Any] | None\n"
            "        ) = None,\n"
            "        **kwargs,\n"
            "    ):"
        ),
        new_text=(
            "        accessor: (\n"
            "            typing.Callable[[typing.Any, str, typing.Any], typing.Any] | None\n"
            "        ) = None,\n"
            "        *,\n"
            "        many: bool = False,\n"
            "        **kwargs,\n"
            "    ):"
        ),
        pytest_target=None,
        description="Add optional many to Field.serialize (Schema._serialize omits forwarding)",
    ),
    MutationCase(
        mid="O13",
        group="optional_wrapper",
        repo="mipcode",
        target_file="src/mipdelta/ledger.py",
        callee_symbol="ObligationLedger.mark_reported_at_stop",
        expected_caller_file="src/mipdelta/cli.py",
        expected_caller_symbol="_run_hook",
        callee_name="mark_reported_at_stop",
        old_text="    def mark_reported_at_stop(self, obligations: Iterable[Obligation]) -> None:",
        new_text='    def mark_reported_at_stop(self, obligations: Iterable[Obligation], *, kind: str = "") -> None:',
        pytest_target=None,
        description="Add optional kind to ObligationLedger.mark_reported_at_stop (_run_hook omits forwarding)",
    ),
    MutationCase(
        mid="O14",
        group="optional_wrapper",
        repo="mipcode",
        target_file="src/mipdelta/session.py",
        callee_symbol="DeltaSession.apply_write",
        expected_caller_file="src/mipdelta/gateway.py",
        expected_caller_symbol="DeltaImpactSideband.after_write",
        callee_name="apply_write",
        old_text=(
            "    def apply_write(\n"
            "        self,\n"
            "        path: str,\n"
            "        new_text: str,\n"
            "        *,\n"
            "        legacy_optional_keyword_all_callers: bool = False,\n"
            "    ) -> WriteDeltaResult:"
        ),
        new_text=(
            "    def apply_write(\n"
            "        self,\n"
            "        path: str,\n"
            "        new_text: str,\n"
            "        *,\n"
            "        legacy_optional_keyword_all_callers: bool = False,\n"
            "        max_response_bytes: int | None = None,\n"
            "    ) -> WriteDeltaResult:"
        ),
        pytest_target=None,
        description="Add optional max_response_bytes to DeltaSession.apply_write (DeltaImpactSideband.after_write omits forwarding)",
    ),
    MutationCase(
        mid="O15",
        group="optional_wrapper",
        repo="mipcode",
        target_file="src/mipdelta/resolve.py",
        callee_symbol="MutableResolutionIndex.add_file",
        expected_caller_file="src/mipdelta/session.py",
        expected_caller_symbol="DeltaSession.from_workspace",
        callee_name="add_file",
        old_text="    def add_file(self, facts: FileFacts) -> None:",
        new_text="    def add_file(self, facts: FileFacts, *, enable: tuple[str, ...] = ()) -> None:",
        pytest_target=None,
        description="Add optional enable to MutableResolutionIndex.add_file (DeltaSession.from_workspace omits forwarding)",
    ),
)


def _repo_prod_files(repo_name: str) -> dict[str, str]:
    meta = REPOS[repo_name]
    root = WORK_ROOT / repo_name
    pkg = root / meta.pkg_rel
    out: dict[str, str] = {}
    for p in sorted(pkg.rglob("*.py")):
        rel = str(p.relative_to(root)).replace("\\", "/")
        out[rel] = p.read_text(encoding="utf-8")
    return out


def _shard_dir_for(repo_name: str, prefer_diag: bool = False) -> tuple[str, Path]:
    if prefer_diag:
        for cand in ("cap8_warm", "diag_safe", "default_warm"):
            d = SHARDS_ROOT / repo_name / cand
            if d.exists() and list(d.glob("*.json")):
                return cand, d
    for cand in ("default_warm", "default_cold", "cap8_warm", "cap8_cold", "cap1_warm", "compat_xdist"):
        d = SHARDS_ROOT / repo_name / cand
        if d.exists() and list(d.glob("*.json")):
            return cand, d
    return "none", SHARDS_ROOT / repo_name / "missing"


def _enclosing_fn(ff: FileFacts, line: int) -> FunctionFact | None:
    best: FunctionFact | None = None
    best_span = 10**9
    for fn in ff.functions.values():
        if fn.line <= line <= fn.end_line:
            span = fn.end_line - fn.line
            if span <= best_span:
                best = fn
                best_span = span
    return best


def _ancestor_class_names(index: resolve_mod.MutableResolutionIndex, cls_name: str) -> list[str]:
    visited: list[str] = []
    seen: set[str] = set()
    q: deque[str] = deque([cls_name])
    while q:
        cur = q.popleft()
        if not cur or cur in seen:
            continue
        seen.add(cur)
        visited.append(cur)
        for cls_fact in index.classes_by_short.get(cur, {}).values():
            for base in cls_fact.bases:
                bshort = base.split(".")[-1]
                if bshort and bshort not in seen:
                    q.append(bshort)
    return visited


def _make_arm2b_resolver(orig_resolve):
    def arm2b_resolve(call, facts: FileFacts, index):
        res = orig_resolve(call, facts, index)
        if res:
            # Fix super() self-cycle bug from unmodified resolve.py when runtime edge mis-matched
            if call.callee_chain and call.callee_chain[0] == "super":
                caller_fn = _enclosing_fn(facts, call.line)
                if caller_fn and all(fn.qualname == caller_fn.qualname and src == "observed_runtime" for fn, src in res):
                    res = ()
                else:
                    return res
            else:
                return res
        if not isinstance(index, resolve_mod.MutableResolutionIndex):
            return ()
        caller_fn = _enclosing_fn(facts, call.line)
        if caller_fn is None:
            return ()
        edges = index.runtime_edges_for(caller_fn.qualname, call.callee_name)
        if not edges:
            return ()
        candidates = list(index.by_short_name.get(call.callee_name, {}).values())
        if not candidates:
            return ()

        matched: list[tuple[FunctionFact, str]] = []
        seen_keys: set[tuple[str, str]] = set()
        for edge in edges:
            edge_cq = edge.callee_qualname or ""
            # 1) Exact callee_qualname match verified against receiver MRO (handles super() & inherited self.*)
            mro = set(_ancestor_class_names(index, edge.receiver_type)) if edge.receiver_type else set()
            exact_hits: list[FunctionFact] = []
            for cand in candidates:
                if edge_cq and cand.qualname == edge_cq:
                    if not cand.class_qualname or not mro or cand.class_qualname in mro or edge.receiver_type in ("type", "super"):
                        if not (call.callee_chain and call.callee_chain[0] == "super" and cand.qualname == caller_fn.qualname):
                            exact_hits.append(cand)
            if exact_hits:
                for cand in exact_hits:
                    k = (cand.path, cand.qualname)
                    if k not in seen_keys:
                        seen_keys.add(k)
                        matched.append((cand, "observed_runtime"))
                continue

            # 2) MRO fallback when callee_qualname doesn't match directly
            if edge.receiver_type:
                for cls_anc in _ancestor_class_names(index, edge.receiver_type):
                    anc_hits = [
                        c for c in candidates
                        if c.class_qualname == cls_anc
                        and not (call.callee_chain and call.callee_chain[0] == "super" and c.qualname == caller_fn.qualname)
                    ]
                    if anc_hits:
                        for cand in anc_hits:
                            k = (cand.path, cand.qualname)
                            if k not in seen_keys:
                                seen_keys.add(k)
                                matched.append((cand, "observed_runtime"))
                        break
        return tuple(matched)

    return arm2b_resolve


def _run_arm2b_analyze(before: dict[str, str], after: dict[str, str], edges: list[ObservedCallEdge]):
    orig = resolve_mod.resolve_call_with_provenance
    import mipdelta.delta as delta_mod

    orig_delta_ref = delta_mod.resolve_call_with_provenance
    patched = _make_arm2b_resolver(orig)
    resolve_mod.resolve_call_with_provenance = patched
    delta_mod.resolve_call_with_provenance = patched
    try:
        return analyze_delta(before, after, runtime_edges=edges)
    finally:
        resolve_mod.resolve_call_with_provenance = orig
        delta_mod.resolve_call_with_provenance = orig_delta_ref


def _matches_target(
    findings,
    case: MutationCase,
    caller_fn_span: tuple[int, int] | None = None,
) -> dict[str, Any]:
    for f in findings:
        if f.path != case.expected_caller_file:
            continue
        if case.group == "optional_wrapper":
            hit = f.detector == "delegating_wrapper_review" and f.symbol == case.expected_caller_symbol
        else:
            in_span = caller_fn_span is None or (caller_fn_span[0] <= f.start_line <= caller_fn_span[1])
            hit = f.symbol == case.callee_symbol and in_span
        if hit:
            return {
                "hit": True,
                "detector": f.detector,
                "path": f.path,
                "symbol": f.symbol,
                "line": f.start_line,
                "resolution_source": f.resolution_source,
                "evidence": f.evidence,
            }
    return {
        "hit": False,
        "other_findings_count": len(findings),
        "other_findings_sample": [
            f"{f.detector}:{f.path}:{f.start_line}:{f.symbol}:{f.resolution_source}" for f in findings[:5]
        ],
    }


def collect_diag_shards() -> dict[str, Any]:
    """Collect harness-only Mock-safe (rich) & self-monitoring-enabled (mipcode) shards."""
    out: dict[str, Any] = {}
    for repo_name in ("rich", "mipcode"):
        work = WORK_ROOT / repo_name
        py_exe = VENV_ROOT / repo_name / "Scripts" / "python.exe"
        shard_dir = SHARDS_ROOT / repo_name / "diag_safe"
        boot_dir = shard_dir / "_bootstrap"
        if shard_dir.exists():
            shutil.rmtree(shard_dir)
        boot_dir.mkdir(parents=True, exist_ok=True)

        work_root = str(work.resolve())
        mip_src = str((ROOT / "src").resolve())
        work_src = str((work / "src").resolve())
        site_code = (
            "import json, os, sys\n"
            f"WORK_SRC = {work_src!r}\n"
            f"MIP_SRC = {mip_src!r}\n"
            f"REPO_NAME = {repo_name!r}\n"
            "if REPO_NAME == 'mipcode' and os.path.isdir(WORK_SRC) and WORK_SRC not in sys.path:\n"
            "    sys.path.insert(0, WORK_SRC)\n"
            "elif MIP_SRC not in sys.path:\n"
            "    sys.path.append(MIP_SRC)\n"
            "try:\n"
            "    import unittest.mock as _umock\n"
            "    import mipdelta.monitoring as _m\n"
            "    def _is_mock(obj):\n"
            "        if obj is None:\n"
            "            return False\n"
            "        tname = type(obj).__name__\n"
            "        return isinstance(obj, _umock.Base) or tname in ('_Call', 'MagicMock', 'Mock', 'NonCallableMock', 'AsyncMock', '_SentinelObject')\n"
            "    _orig_on_call = _m.ScopedCallCollector._on_call\n"
            "    def _safe_on_call(self, code, offset, callable_obj, arg0):\n"
            "        if _is_mock(callable_obj) or _is_mock(arg0):\n"
            "            allowed, _ = self._classify_code(code)\n"
            "            if not allowed:\n"
            "                return sys.monitoring.DISABLE\n"
            "            return None\n"
            "        try:\n"
            "            cname = getattr(callable_obj, '__name__', None)\n"
            "            cqual = getattr(callable_obj, '__qualname__', None)\n"
            "            if (cname is not None and not isinstance(cname, str)) or (cqual is not None and not isinstance(cqual, str)):\n"
            "                return None\n"
            "            res = _orig_on_call(self, code, offset, callable_obj, arg0)\n"
            "            return res\n"
            "        except TypeError:\n"
            "            return None\n"
            "    _m.ScopedCallCollector._on_call = _safe_on_call\n"
            "    _orig_to_shard = _m.ScopedCallCollector.to_shard_dict\n"
            "    def _safe_to_shard(self):\n"
            "        d = _orig_to_shard(self)\n"
            "        for e in d.get('edges', []):\n"
            "            for k in ('receiver_type', 'callee_name', 'callee_qualname', 'caller_qualname'):\n"
            "                v = e.get(k)\n"
            "                if v is not None and not isinstance(v, str):\n"
            "                    e[k] = str(type(v).__name__)\n"
            "        return d\n"
            "    _m.ScopedCallCollector.to_shard_dict = _safe_to_shard\n"
            "    _m.install_process_collector_from_env()\n"
            "except Exception as _exc:\n"
            "    sys.stderr.write(f'[diag_site_err] {_exc}\\n')\n"
        )
        (boot_dir / "sitecustomize.py").write_text(site_code, encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["MIPDELTA_MONITOR_CONFIG"] = json.dumps(
            {
                "include_roots": [work_root],
                "max_events_per_site": 8,
                "shard_dir": str(shard_dir),
            }
        )
        pp_parts = [str(boot_dir)]
        if repo_name == "mipcode":
            pp_parts.append(work_src)
        else:
            pp_parts.append(mip_src)
        pp_parts.append(work_root)
        if env.get("PYTHONPATH"):
            pp_parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pp_parts)

        t0 = time.perf_counter()
        proc = subprocess.run(
            [str(py_exe), "-m", "pytest", "-q", "tests"],
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        edges = _load_runtime_edges(shard_dir)
        prod_edges = [e for e in edges if e.caller_path.startswith(("rich/", "src/mipdelta/"))]
        out[repo_name] = {
            "returncode": proc.returncode,
            "wall_ms": wall_ms,
            "shard_files": len(list(shard_dir.glob("*.json"))),
            "total_edges": len(edges),
            "prod_edges": len(prod_edges),
            "tail": (proc.stdout or proc.stderr).splitlines()[-5:],
        }
        print(f"[diag_safe:{repo_name}] rc={proc.returncode} ms={wall_ms} shards={out[repo_name]['shard_files']} total_edges={len(edges)} prod_edges={len(prod_edges)}", flush=True)
    return out


def repo_wide_edge_coverage() -> dict[str, Any]:
    results: dict[str, Any] = {}
    arm2b_fn = _make_arm2b_resolver(resolve_mod.resolve_call_with_provenance)

    for repo_name, meta in REPOS.items():
        sources = _repo_prod_files(repo_name)
        facts_map = {p: parse_file(p, txt) for p, txt in sources.items()}
        static_idx = resolve_mod.build_index(facts_map)
        methods = {fn.name for ff in facts_map.values() for fn in ff.functions.values() if fn.class_qualname}

        static_resolved = 0
        unresolved_calls: list[tuple[str, FileFacts, Any]] = []
        for p, ff in facts_map.items():
            for c in ff.calls:
                if len(c.callee_chain) < 2 or c.callee_name not in methods:
                    continue
                if resolve_mod.resolve_call_with_provenance(c, ff, static_idx):
                    static_resolved += 1
                else:
                    unresolved_calls.append((p, ff, c))

        as_shipped_lbl, as_shipped_dir = _shard_dir_for(repo_name, prefer_diag=False)
        diag_lbl, diag_dir = _shard_dir_for(repo_name, prefer_diag=True)

        repo_cov: dict[str, Any] = {
            "prod_files": len(sources),
            "static_attr_resolved": static_resolved,
            "static_attr_unresolved": len(unresolved_calls),
            "runs": {},
        }
        eval_dirs = [("as_shipped:" + as_shipped_lbl, as_shipped_dir)]
        if diag_dir != as_shipped_dir and diag_dir.exists():
            eval_dirs.append(("diag_safe:" + diag_lbl, diag_dir))

        for label, sdir in eval_dirs:
            edges = _load_runtime_edges(sdir) if sdir.exists() else []
            rt_idx = resolve_mod.build_index(facts_map, runtime_edges=edges)
            arm2a = 0
            arm2b = 0
            raw_edge_present = 0
            for _p, ff, c in unresolved_calls:
                if resolve_mod.resolve_call_with_provenance(c, ff, rt_idx):
                    arm2a += 1
                if arm2b_fn(c, ff, rt_idx):
                    arm2b += 1
                enc = _enclosing_fn(ff, c.line)
                if enc and rt_idx.runtime_edges_for(enc.qualname, c.callee_name):
                    raw_edge_present += 1
            n_unres = len(unresolved_calls) or 1
            repo_cov["runs"][label] = {
                "edge_count_loaded": len(edges),
                "arm2a_rescued": arm2a,
                "arm2a_rescue_pct": round(100.0 * arm2a / n_unres, 1),
                "arm2b_rescued": arm2b,
                "arm2b_rescue_pct": round(100.0 * arm2b / n_unres, 1),
                "raw_edge_present": raw_edge_present,
                "raw_edge_present_pct": round(100.0 * raw_edge_present / n_unres, 1),
            }
        results[repo_name] = repo_cov
    return results


def _prepare_mypy_shadow(repo_name: str) -> Path:
    src_repo = WORK_ROOT / repo_name
    dst_repo = MUT_WORK_ROOT / repo_name
    if dst_repo.exists():
        shutil.rmtree(dst_repo)
    dst_repo.mkdir(parents=True, exist_ok=True)
    for rel_top in (REPOS[repo_name].pkg_rel.split("/")[0], "pyproject.toml"):
        sp = src_repo / rel_top
        dp = dst_repo / rel_top
        if sp.is_dir():
            shutil.copytree(sp, dp)
        elif sp.is_file():
            shutil.copy2(sp, dp)
    return dst_repo


def _run_mypy(repo_name: str, shadow_repo: Path) -> set[tuple[str, int, str, str]]:
    meta = REPOS[repo_name]
    mypy_exe = VENV_ROOT / repo_name / "Scripts" / "mypy.exe"
    env = os.environ.copy()
    env["MYPYPATH"] = str((shadow_repo / meta.mypy_path_rel).resolve())
    cmd = [
        str(mypy_exe),
        "--config-file",
        str((shadow_repo / "pyproject.toml").resolve()),
        "--no-incremental",
        "--no-error-summary",
        "--show-error-codes",
        "--hide-error-context",
        meta.mypy_target,
    ]
    proc = subprocess.run(cmd, cwd=str(shadow_repo), env=env, capture_output=True, text=True, check=False)
    errs: set[tuple[str, int, str, str]] = set()
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        if ": error:" not in line:
            continue
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        fpath = parts[0].strip().replace("\\", "/")
        try:
            lno = int(parts[1].strip())
        except ValueError:
            lno = 0
        msg = parts[3].strip()
        code = ""
        if msg.endswith("]") and "[" in msg:
            code = msg[msg.rfind("[") + 1 : -1]
        errs.add((fpath, lno, code, msg))
    return errs


def run_preflight() -> bool:
    print("=== Running fast preflight across all 30 mutations ===", flush=True)
    repo_sources = {name: _repo_prod_files(name) for name in REPOS}
    all_ok = True
    for case in MUTATIONS:
        before = repo_sources[case.repo]
        src = before[case.target_file]
        cnt = src.count(case.old_text)
        if cnt != 1:
            print(f"[FAIL] {case.mid}: old_text count={cnt} in {case.target_file}")
            all_ok = False
            continue
        after = dict(before)
        after[case.target_file] = src.replace(case.old_text, case.new_text, 1)

        before_facts = parse_file(case.expected_caller_file, before[case.expected_caller_file])
        caller_fn = before_facts.functions.get(case.expected_caller_symbol)
        caller_span = (caller_fn.line, caller_fn.end_line) if caller_fn else None

        arm1 = analyze_delta(before, after)
        arm1_hit = _matches_target(arm1, case, caller_span)
        # Synthesize ideal edge at expected caller -> callee to confirm detector logic fires when resolved
        synth_edges = [
            ObservedCallEdge(
                caller_path=case.expected_caller_file,
                caller_qualname=case.expected_caller_symbol,
                instruction_offset=10,
                callee_qualname=case.callee_symbol,
                callee_name=case.callee_name,
                receiver_type=case.callee_symbol.split(".")[0],
                pid=1,
            )
        ]
        # Verify caller file contains the call to callee_name
        has_call = any(
            c.callee_name == case.callee_name and c.owner_function == case.expected_caller_symbol
            for c in before_facts.calls
        )
        synth_findings = _run_arm2b_analyze(before, after, synth_edges)
        synth_hit = _matches_target(synth_findings, case, caller_span)
        if arm1_hit["hit"]:
            print(f"[LEAK] {case.mid} ({case.group}): Arm 1 unexpectedly HIT -> {arm1_hit}")
            all_ok = False
        elif not has_call or not synth_hit["hit"]:
            print(f"[FAIL] {case.mid} ({case.group}): has_call={has_call} synth_hit={synth_hit}")
            all_ok = False
        else:
            print(f"[OK]   {case.mid} ({case.group:16s}) | Arm1=MISS | SynthArm2b={synth_hit['detector']} ({case.expected_caller_symbol})")
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--collect-diag", action="store_true")
    parser.add_argument("--skip-pytest", action="store_true")
    args = parser.parse_args()

    if args.preflight:
        ok = run_preflight()
        sys.exit(0 if ok else 1)

    if args.collect_diag:
        diag = collect_diag_shards()
        print(json.dumps(diag, indent=2), flush=True)
        return

    t0 = time.perf_counter()
    print("=== 1) Repo-wide dynamic edge coverage across 4 repos ===", flush=True)
    coverage = repo_wide_edge_coverage()
    print(json.dumps(coverage, indent=2), flush=True)

    print("\n=== 2) Running 4-Arm 30-Mutation Benchmark ===", flush=True)
    repo_sources = {name: _repo_prod_files(name) for name in REPOS}
    as_shipped_edges = {
        name: _load_runtime_edges(_shard_dir_for(name, prefer_diag=False)[1])
        if _shard_dir_for(name, prefer_diag=False)[1].exists()
        else []
        for name in REPOS
    }
    diag_edges = {
        name: _load_runtime_edges(_shard_dir_for(name, prefer_diag=True)[1])
        if _shard_dir_for(name, prefer_diag=True)[1].exists()
        else []
        for name in REPOS
    }

    # Baseline mypy per repo in gitless shadow directories
    shadow_dirs = {name: _prepare_mypy_shadow(name) for name in REPOS}
    mypy_baselines = {name: _run_mypy(name, shadow_dirs[name]) for name in REPOS}
    print(
        "Mypy baselines:",
        {k: len(v) for k, v in mypy_baselines.items()},
        flush=True,
    )

    records: list[dict[str, Any]] = []
    for case in MUTATIONS:
        before = repo_sources[case.repo]
        orig_text = before[case.target_file]
        assert orig_text.count(case.old_text) == 1, f"{case.mid}: anchor not unique"
        mut_text = orig_text.replace(case.old_text, case.new_text, 1)
        after = dict(before)
        after[case.target_file] = mut_text

        caller_facts = parse_file(case.expected_caller_file, before[case.expected_caller_file])
        caller_fn = caller_facts.functions.get(case.expected_caller_symbol)
        caller_span = (caller_fn.line, caller_fn.end_line) if caller_fn else None

        arm1_res = _matches_target(analyze_delta(before, after), case, caller_span)
        arm2a_res = _matches_target(
            analyze_delta(before, after, runtime_edges=as_shipped_edges[case.repo]),
            case,
            caller_span,
        )
        arm2b_res = _matches_target(
            _run_arm2b_analyze(before, after, diag_edges[case.repo]),
            case,
            caller_span,
        )

        # Arm 3: Installed mypy against mutated shadow copy
        s_file = shadow_dirs[case.repo] / case.target_file
        s_file.write_text(mut_text, encoding="utf-8")
        try:
            mut_errs = _run_mypy(case.repo, shadow_dirs[case.repo])
        finally:
            s_file.write_text(orig_text, encoding="utf-8")
        new_errs = sorted(mut_errs - mypy_baselines[case.repo])
        caller_file_mypy = [e for e in new_errs if e[0] == case.expected_caller_file]
        caller_mypy = [
            e for e in caller_file_mypy
            if caller_span is None or (caller_span[0] <= e[1] <= caller_span[1])
        ]
        arm3_res = {
            "hit": len(caller_mypy) > 0,
            "caller_errors": [f"{e[0]}:{e[1]} [{e[2]}] {e[3]}" for e in caller_mypy[:5]],
            "caller_file_errors": [f"{e[0]}:{e[1]} [{e[2]}] {e[3]}" for e in caller_file_mypy[:5]],
            "total_new_mypy_errors": len(new_errs),
        }

        # Targeted pytest runtime verification on B01..B15 cleanly sequentially
        pytest_res: dict[str, Any] = {"ran": False}
        if not args.skip_pytest and case.pytest_target:
            w_file = WORK_ROOT / case.repo / case.target_file
            py_exe = VENV_ROOT / case.repo / "Scripts" / "pytest.exe"
            w_file.write_text(mut_text, encoding="utf-8")
            try:
                p_run = subprocess.run(
                    [str(py_exe), "-q", case.pytest_target],
                    cwd=str(WORK_ROOT / case.repo),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                pytest_res = {
                    "ran": True,
                    "target": case.pytest_target,
                    "returncode": p_run.returncode,
                    "failed_as_expected": p_run.returncode != 0,
                    "tail": (p_run.stdout or p_run.stderr).splitlines()[-3:],
                }
            finally:
                w_file.write_text(orig_text, encoding="utf-8")

        rec = {
            "id": case.mid,
            "group": case.group,
            "repo": case.repo,
            "target_file": case.target_file,
            "callee_symbol": case.callee_symbol,
            "expected_caller_file": case.expected_caller_file,
            "expected_caller_symbol": case.expected_caller_symbol,
            "description": case.description,
            "arm1_static": arm1_res,
            "arm2a_as_shipped": arm2a_res,
            "arm2b_diag_fixed": arm2b_res,
            "arm3_mypy": arm3_res,
            "targeted_pytest": pytest_res,
        }
        records.append(rec)
        print(
            f"  {case.mid} [{case.repo:11s}|{case.group:16s}] "
            f"A1={'1' if arm1_res['hit'] else '0'} "
            f"A2a={'1' if arm2a_res['hit'] else '0'} "
            f"A2b={'1' if arm2b_res['hit'] else '0'} "
            f"mypy={'1' if arm3_res['hit'] else '0'} "
            f"pytest={'FAIL' if pytest_res.get('failed_as_expected') else ('PASS' if pytest_res.get('ran') else '-')}",
            flush=True,
        )

    def _agg(sub: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(sub) or 1
        a1 = sum(1 for r in sub if r["arm1_static"]["hit"])
        a2a = sum(1 for r in sub if r["arm2a_as_shipped"]["hit"])
        a2b = sum(1 for r in sub if r["arm2b_diag_fixed"]["hit"])
        a3 = sum(1 for r in sub if r["arm3_mypy"]["hit"])
        a2a_over_m = sum(1 for r in sub if r["arm2a_as_shipped"]["hit"] and not r["arm3_mypy"]["hit"] and not r["arm1_static"]["hit"])
        a2b_over_m = sum(1 for r in sub if r["arm2b_diag_fixed"]["hit"] and not r["arm3_mypy"]["hit"] and not r["arm1_static"]["hit"])
        return {
            "count": len(sub),
            "arm1_static": f"{a1}/{len(sub)} ({100.0 * a1 / n:.1f}%)",
            "arm2a_as_shipped": f"{a2a}/{len(sub)} ({100.0 * a2a / n:.1f}%)",
            "arm2b_diag_fixed": f"{a2b}/{len(sub)} ({100.0 * a2b / n:.1f}%)",
            "arm3_mypy": f"{a3}/{len(sub)} ({100.0 * a3 / n:.1f}%)",
            "arm2a_net_lift_over_static_and_mypy": f"{a2a_over_m}/{len(sub)} ({100.0 * a2a_over_m / n:.1f}%)",
            "arm2b_net_lift_over_static_and_mypy": f"{a2b_over_m}/{len(sub)} ({100.0 * a2b_over_m / n:.1f}%)",
        }

    summary = {
        "overall": _agg(records),
        "breaking": _agg([r for r in records if r["group"] == "breaking"]),
        "optional_wrapper": _agg([r for r in records if r["group"] == "optional_wrapper"]),
        "wall_seconds": round(time.perf_counter() - t0, 2),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"coverage": coverage, "summary": summary, "mutations": records}, indent=2),
        encoding="utf-8",
    )
    print("\n=== Track A2 Summary ===", flush=True)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
