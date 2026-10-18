#!/usr/bin/env python3
"""
mipcode.compiler — Polyglot Mipmap Container Compiler (`.mip.json` / `.lod.json`)
"""
from semantic_lod.compiler import *  # noqa: F401,F403
from semantic_lod.compiler import main_cli

if __name__ == "__main__":
    main_cli()
