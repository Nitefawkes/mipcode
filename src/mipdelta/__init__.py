import importlib.metadata
import re
from pathlib import Path

from .anchor import audit_anchor_workspace, parse_anchor_file
from .cache import CacheStats, FactCache
from .delta import (
    OPT_IN_DETECTORS,
    analyze_changed_files_in_index,
    analyze_delta,
    audit_index,
    audit_workspace,
    deduplicate_findings,
    finding_rank_key,
    format_advisory,
    rank_findings,
    summarize_actual_delta,
)
from .facts import parse_file
from .gateway import ContainedGatewaySession, DeltaImpactSideband, gateway_tool_schemas
from .ledger import format_thrash_advisory
from .model import (
    CallFact,
    ClassFact,
    CustodyReport,
    FileFacts,
    Finding,
    FunctionFact,
    ObservedCallEdge,
    ParameterFact,
    SourceFile,
    WriteDeltaResult,
)
from .monitoring import (
    ScopedCallCollector,
    install_process_collector_from_env,
)
from .resolve import MutableResolutionIndex
from .session import DeltaSession
from .solidity import analyze_solidity_delta, audit_solidity_workspace, parse_solidity_file
from .solidity_fork import analyze_solidity_fork_diff
from .stubs import TypeCandidateIndex, parse_stub_tree


def _resolve_package_version() -> str:
    """Return the package version, preferring pyproject.toml in a source checkout."""
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file() and (repo_root / "src" / "mipdelta").is_dir():
        in_project = False
        for raw_line in pyproject.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("["):
                in_project = line == "[project]"
            elif in_project:
                match = re.match(r'^version\s*=\s*"([^"]+)"', line)
                if match:
                    return match.group(1)
    try:
        return importlib.metadata.version("coherion-mipcode")
    except importlib.metadata.PackageNotFoundError:
        return "0.6.5"


__version__ = _resolve_package_version()

__all__ = [
    "__version__",
    "CacheStats",
    "CallFact",
    "ClassFact",
    "ContainedGatewaySession",
    "CustodyReport",
    "DeltaImpactSideband",
    "DeltaSession",
    "FactCache",
    "FileFacts",
    "Finding",
    "FunctionFact",
    "MutableResolutionIndex",
    "OPT_IN_DETECTORS",
    "ObservedCallEdge",
    "ParameterFact",
    "ScopedCallCollector",
    "SourceFile",
    "TypeCandidateIndex",
    "WriteDeltaResult",
    "analyze_changed_files_in_index",
    "analyze_delta",
    "analyze_solidity_delta",
    "analyze_solidity_fork_diff",
    "audit_anchor_workspace",
    "audit_index",
    "audit_solidity_workspace",
    "audit_workspace",
    "deduplicate_findings",
    "finding_rank_key",
    "format_advisory",
    "format_thrash_advisory",
    "gateway_tool_schemas",
    "install_process_collector_from_env",
    "parse_anchor_file",
    "parse_file",
    "parse_solidity_file",
    "parse_stub_tree",
    "rank_findings",
    "summarize_actual_delta",
]
