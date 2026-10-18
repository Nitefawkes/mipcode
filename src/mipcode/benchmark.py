#!/usr/bin/env python3
"""
mipcode.benchmark — Multi-Condition Empirical Benchmark Harness for MipCode v0.4
"""
import sys
from semantic_lod.benchmark import *  # noqa: F401,F403
from semantic_lod.benchmark import run_benchmark_suite

if __name__ == "__main__":
    pos_args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    target = pos_args[0] if pos_args else "demo_engine"
    is_live_agent = any(f in flags for f in ("--live-agent", "--live-gemini", "--live-openai", "--live-anthropic"))
    run_benchmark_suite(target, live_agent=is_live_agent)
