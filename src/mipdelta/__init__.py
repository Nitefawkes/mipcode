import importlib.metadata
import re
from pathlib import Path

from .cache import CacheStats, FactCache
from .delta import (
    OPT_IN_DETECTORS,
    analyze_changed_files_in_index,
    analyze_delta,
    deduplicate_findings,
    format_advisory,
    rank_findings,
)
from .facts import parse_file
from .gateway import ContainedGatewaySession, DeltaImpactSideband, gateway_tool_schemas
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
        return importlib.metadata.version("mipcode")
    except importlib.metadata.PackageNotFoundError:
        return "0.6.2"


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
    "deduplicate_findings",
    "format_advisory",
    "gateway_tool_schemas",
    "install_process_collector_from_env",
    "parse_file",
    "parse_stub_tree",
    "rank_findings",
]
