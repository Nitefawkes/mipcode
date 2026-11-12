"""Run a mip-delta Claude Code hook from a pinned engine snapshot beside this script.

Usage: python mipdelta_hook.py claude-post-edit|claude-stop [--enable DETECTOR ...]

Layout: <dir>/bin/mipdelta_hook.py and <dir>/engine/src/mipdelta. Keep <dir> somewhere
only the owning account can write, because the session executes this file.
"""
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "engine" / "src"
sys.path.insert(0, str(ENGINE))
os.environ.setdefault("MIPDELTA_LOG_ALL_RUNS", "1")

from mipdelta.cli import main_cli  # noqa: E402

raise SystemExit(main_cli(["--hook", sys.argv[1], *sys.argv[2:]]))
