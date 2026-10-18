"""
MipCode (`.mip` / `.mip.json`) — Architectural Mipmaps, Compact Markdown Guardrails & Stateful Fog-of-War MCP Runtime.
"""
__version__ = "0.4.0"

from semantic_lod.compiler import build_mip_container, build_lod_container
from semantic_lod.mcp_server import (
    mip_map,
    mip_symbols,
    mip_zoom,
    mip_blast,
    mip_guardrail,
    mip_verify_edit,
    mip_propose_delta,
    render_compact_markdown,
)

__all__ = [
    "__version__",
    "build_mip_container",
    "build_lod_container",
    "mip_map",
    "mip_symbols",
    "mip_zoom",
    "mip_blast",
    "mip_guardrail",
    "mip_verify_edit",
    "mip_propose_delta",
    "render_compact_markdown",
]
