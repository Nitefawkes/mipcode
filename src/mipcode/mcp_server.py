#!/usr/bin/env python3
"""
mipcode.mcp_server — MipCode Stateful Fog-of-War & Pre-Edit Guardrail MCP Server (`mip.*`)
"""
import json
import sys
from semantic_lod.mcp_server import *  # noqa: F401,F403
from semantic_lod.mcp_server import main_verify_edit_cli, mcp_app, mip_map

if __name__ == "__main__":
    if "--verify-edit" in sys.argv:
        main_verify_edit_cli()
    elif mcp_app and "--stdio" in sys.argv:
        mcp_app.run()
    else:
        print(json.dumps(mip_map(token_budget=500), indent=2))
