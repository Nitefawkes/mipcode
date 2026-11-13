"""Experiment 2 — Part A (Track A1): Operational, memory, shard, & runner compatibility audit
for src/mipdelta/monitoring.py (--run-pytest) across marshmallow, rich, httpx, and mipcode.

Runs inside isolated C:\\tmp\\exp2-venvs\\* and C:\\tmp\\exp2-work\\*.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments" / "audit_rt_ledger"))
from win_mem import run_with_metrics  # noqa: E402

OUT_DIR = ROOT / "results" / "audit_rt_ledger"
SHARD_KEEP_DIR = Path(r"C:\tmp\exp2-shards")

TARGETS: tuple[dict[str, Any], ...] = (
    {
        "name": "marshmallow",
        "repo": Path(r"C:\tmp\exp2-work\marshmallow"),
        "python": Path(r"C:\tmp\exp2-venvs\marshmallow\Scripts\python.exe"),
        "pkg": "marshmallow",
        "prod_prefixes": ("src/marshmallow/",),
        "pytest_args": "-q tests",
    },
    {
        "name": "rich",
        "repo": Path(r"C:\tmp\exp2-work\rich"),
        "python": Path(r"C:\tmp\exp2-venvs\rich\Scripts\python.exe"),
        "pkg": "rich",
        "prod_prefixes": ("rich/",),
        "pytest_args": "-q tests",
    },
    {
        "name": "httpx",
        "repo": Path(r"C:\tmp\exp2-work\httpx"),
        "python": Path(r"C:\tmp\exp2-venvs\httpx\Scripts\python.exe"),
        "pkg": "httpx",
        "prod_prefixes": ("httpx/",),
        "pytest_args": "-q tests",
    },
    {
        "name": "mipcode",
        "repo": Path(r"C:\tmp\exp2-work\mipcode"),
        "python": Path(r"C:\tmp\exp2-venvs\mipcode\Scripts\python.exe"),
        "pkg": "mipdelta",
        "prod_prefixes": ("src/mipdelta/",),
        "pytest_args": "-q tests",
    },
)


def _pytest_passed_summary(stdout: str, stderr: str) -> str:
    combined = stdout + "\n" + stderr
    matches = re.findall(r"(\d+\s+(?:passed|failed|error|skipped|xfailed|warnings?).*)", combined)
    return matches[-1].strip() if matches else ""


def _analyze_shards(shard_dir: Path, prod_prefixes: tuple[str, ...]) -> dict[str, Any]:
    files = sorted(shard_dir.glob("edges-*.json"))
    total_bytes = sum(f.stat().st_size for f in files)
    raw_rows = 0
    unique_prod = set()
    unique_test = set()
    unique_other = set()
    prod_bytes_est = 0
    pids = set()
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        for row in data.get("edges", []):
            raw_rows += 1
            pids.add(row.get("pid"))
            cpath = str(row.get("caller_path", "")).replace("\\", "/")
            sig = (
                cpath,
                row.get("caller_qualname"),
                row.get("callee_qualname"),
                row.get("callee_name"),
                row.get("receiver_type"),
            )
            if any(cpath.startswith(p) for p in prod_prefixes):
                unique_prod.add(sig)
            elif cpath.startswith("tests/") or "/tests/" in cpath:
                unique_test.add(sig)
            else:
                unique_other.add(sig)
    return {
        "shard_files": len(files),
        "unique_pids": len(pids),
        "total_bytes": total_bytes,
        "total_kb": round(total_bytes / 1024.0, 2),
        "raw_edge_records": raw_rows,
        "unique_prod_edges": len(unique_prod),
        "unique_test_edges": len(unique_test),
        "unique_other_edges": len(unique_other),
    }


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


def _run_cap_pytest(target: dict[str, Any], shard_dir: Path, max_events: int = 8) -> dict[str, Any]:
    """Run pytest under ScopedCallCollector(..., max_events_per_site=max_events) via temp sitecustomize."""
    if shard_dir.exists():
        shutil.rmtree(shard_dir)
    shard_dir.mkdir(parents=True, exist_ok=True)
    bootstrap = Path(tempfile.mkdtemp(prefix=f"exp2-cap{max_events}-"))
    try:
        (bootstrap / "sitecustomize.py").write_text(
            "import json, os\n"
            "try:\n"
            "    from mipdelta.monitoring import install_process_collector_from_env\n"
            "    install_process_collector_from_env()\n"
            "except Exception as exc:\n"
            "    import sys\n"
            f"    print('exp2-cap{max_events} bootstrap error:', exc, file=sys.stderr)\n",
            encoding="utf-8",
        )
        cfg = json.dumps(
            {
                "include_roots": [str(target["repo"].resolve())],
                "max_events_per_site": max_events,
                "shard_dir": str(shard_dir.resolve()),
            }
        )
        target_src = target["repo"] / "src"
        pp_parts = [str(bootstrap)]
        if target_src.is_dir():
            pp_parts.append(str(target_src.resolve()))
        pp_parts.extend([str(target["repo"].resolve()), str(ROOT / "src")])
        env = _clean_env(
            {
                "MIPDELTA_MONITOR_CONFIG": cfg,
                "PYTHONPATH": os.pathsep.join(pp_parts),
            }
        )
        argv = [str(target["python"]), "-m", "pytest", *target["pytest_args"].split()]
        res = run_with_metrics(argv, cwd=target["repo"], env=env, timeout=360.0)
        return {
            "returncode": res["returncode"],
            "timed_out": res["timed_out"],
            "wall_ms": res["wall_ms"],
            "peak_process_mb": res["peak_process_mb"],
            "peak_job_mb": res["peak_job_mb"],
            "summary": _pytest_passed_summary(str(res["stdout"]), str(res["stderr"])),
            "shards": _analyze_shards(shard_dir, target["prod_prefixes"]),
        }
    finally:
        shutil.rmtree(bootstrap, ignore_errors=True)


def run_repo_suite(target: dict[str, Any]) -> dict[str, Any]:
    name = target["name"]
    repo = target["repo"]
    py = str(target["python"])
    p_args = target["pytest_args"].split()
    mip_src = str(ROOT / "src")

    repo_shards = SHARD_KEEP_DIR / name
    if repo_shards.exists():
        shutil.rmtree(repo_shards)
    repo_shards.mkdir(parents=True, exist_ok=True)

    print(f"\n=== [{name}] Track A1 operational & compatibility audit ===", flush=True)

    # Clear pytest cache for genuine cold baseline
    pc = repo / ".pytest_cache"
    if pc.exists():
        shutil.rmtree(pc, ignore_errors=True)

    base_env = _clean_env()
    cli_env = _clean_env({"PYTHONPATH": mip_src})

    # 1. Baseline pytest (cold + warm)
    base_cold = run_with_metrics([py, "-m", "pytest", *p_args], cwd=repo, env=base_env, timeout=360.0)
    print(f"  baseline_cold: rc={base_cold['returncode']} ms={base_cold['wall_ms']} rss={base_cold['peak_job_mb']}MB | {_pytest_passed_summary(str(base_cold['stdout']), str(base_cold['stderr']))}", flush=True)

    base_warm = run_with_metrics([py, "-m", "pytest", *p_args], cwd=repo, env=base_env, timeout=360.0)
    print(f"  baseline_warm: rc={base_warm['returncode']} ms={base_warm['wall_ms']} rss={base_warm['peak_job_mb']}MB", flush=True)

    # 2. Monitored mip-delta --run-pytest (cold cache clear, max_events=1 CLI default)
    if pc.exists():
        shutil.rmtree(pc, ignore_errors=True)
    cold_shards = repo_shards / "default_cold"
    cold_shards.mkdir(parents=True, exist_ok=True)
    rt_cold = run_with_metrics(
        [py, "-m", "mipdelta.cli", "--root", str(repo), "--run-pytest", "--pytest-args", target["pytest_args"], "--runtime-dir", str(cold_shards)],
        cwd=repo,
        env=cli_env,
        timeout=360.0,
    )
    cold_shard_stats = _analyze_shards(cold_shards, target["prod_prefixes"])
    cold_oh = round(((float(rt_cold["wall_ms"]) / float(base_cold["wall_ms"])) - 1.0) * 100.0, 2) if base_cold["wall_ms"] else None
    print(f"  rt_obs_cold:   rc={rt_cold['returncode']} ms={rt_cold['wall_ms']} ({cold_oh:+.2f}%) rss={rt_cold['peak_job_mb']}MB shards={cold_shard_stats}", flush=True)

    warm_shards = repo_shards / "default_warm"
    warm_shards.mkdir(parents=True, exist_ok=True)
    rt_warm = run_with_metrics(
        [py, "-m", "mipdelta.cli", "--root", str(repo), "--run-pytest", "--pytest-args", target["pytest_args"], "--runtime-dir", str(warm_shards)],
        cwd=repo,
        env=cli_env,
        timeout=360.0,
    )
    warm_shard_stats = _analyze_shards(warm_shards, target["prod_prefixes"])
    warm_oh = round(((float(rt_warm["wall_ms"]) / float(base_warm["wall_ms"])) - 1.0) * 100.0, 2) if base_warm["wall_ms"] else None
    print(f"  rt_obs_warm:   rc={rt_warm['returncode']} ms={rt_warm['wall_ms']} ({warm_oh:+.2f}%) rss={rt_warm['peak_job_mb']}MB", flush=True)

    # Check-git baseline overhead (part included at end of --run-pytest)
    chk_git = run_with_metrics(
        [py, "-m", "mipdelta.cli", "--root", str(repo), "--check-git", "--runtime-dir", str(warm_shards)],
        cwd=repo,
        env=cli_env,
        timeout=120.0,
    )

    # 3. max_events_per_site=8 stress comparison
    cap8 = _run_cap_pytest(target, repo_shards / "cap8_warm", max_events=8)
    cap8_oh = round(((float(cap8["wall_ms"]) / float(base_warm["wall_ms"])) - 1.0) * 100.0, 2) if base_warm["wall_ms"] else None
    print(f"  rt_obs_cap8:   rc={cap8['returncode']} ms={cap8['wall_ms']} ({cap8_oh:+.2f}%) edges_prod={cap8['shards']['unique_prod_edges']}", flush=True)

    # 4. Compatibility checks: pytest-cov + pytest-xdist -n 2 (apples-to-apples baseline + monitored)
    cov_shards = repo_shards / "compat_cov"
    cov_shards.mkdir(parents=True, exist_ok=True)
    cov_args_list = ["-q", f"--cov={target['pkg']}", "--cov-report=", "tests"]
    cov_args = " ".join(cov_args_list)
    cov_base = run_with_metrics([py, "-m", "pytest", *cov_args_list], cwd=repo, env=base_env, timeout=360.0)
    cov_res = run_with_metrics(
        [py, "-m", "mipdelta.cli", "--root", str(repo), "--run-pytest", "--pytest-args", cov_args, "--runtime-dir", str(cov_shards)],
        cwd=repo,
        env=cli_env,
        timeout=360.0,
    )
    cov_shard_stats = _analyze_shards(cov_shards, target["prod_prefixes"])
    cov_oh = round(((float(cov_res["wall_ms"]) / float(cov_base["wall_ms"])) - 1.0) * 100.0, 2) if cov_base["wall_ms"] else None
    print(
        f"  compat_cov:    base_rc={cov_base['returncode']}({cov_base['wall_ms']}ms) rt_rc={cov_res['returncode']}({cov_res['wall_ms']}ms, {cov_oh:+.2f}%) prod_edges={cov_shard_stats['unique_prod_edges']}",
        flush=True,
    )

    xdist_shards = repo_shards / "compat_xdist"
    xdist_shards.mkdir(parents=True, exist_ok=True)
    xdist_args_list = ["-q", "-n", "2", "tests"]
    xdist_args = " ".join(xdist_args_list)
    xdist_base = run_with_metrics([py, "-m", "pytest", *xdist_args_list], cwd=repo, env=base_env, timeout=360.0)
    xdist_res = run_with_metrics(
        [py, "-m", "mipdelta.cli", "--root", str(repo), "--run-pytest", "--pytest-args", xdist_args, "--runtime-dir", str(xdist_shards)],
        cwd=repo,
        env=cli_env,
        timeout=360.0,
    )
    xdist_shard_stats = _analyze_shards(xdist_shards, target["prod_prefixes"])
    xdist_oh = round(((float(xdist_res["wall_ms"]) / float(xdist_base["wall_ms"])) - 1.0) * 100.0, 2) if xdist_base["wall_ms"] else None
    print(
        f"  compat_xdist:  base_rc={xdist_base['returncode']}({xdist_base['wall_ms']}ms) rt_rc={xdist_res['returncode']}({xdist_res['wall_ms']}ms, {xdist_oh:+.2f}%) files={xdist_shard_stats['shard_files']} pids={xdist_shard_stats['unique_pids']} prod_edges={xdist_shard_stats['unique_prod_edges']}",
        flush=True,
    )

    def _compact(res: dict[str, Any]) -> dict[str, Any]:
        err_tail = (str(res["stderr"]) + "\n" + str(res["stdout"]))[-1200:]
        has_tool_err = any(
            tok in (str(res["stdout"]) + "\n" + str(res["stderr"]))
            for tok in ("sys.monitoring", "tool ID", "No free", "INTERNALERROR", "PytestUnraisableExceptionWarning")
        )
        return {
            "returncode": res["returncode"],
            "timed_out": res["timed_out"],
            "wall_ms": res["wall_ms"],
            "peak_process_mb": res["peak_process_mb"],
            "peak_job_mb": res["peak_job_mb"],
            "summary": _pytest_passed_summary(str(res["stdout"]), str(res["stderr"])),
            "has_tool_or_hook_warning": has_tool_err,
            "tail_if_failed": err_tail if res["returncode"] != 0 or has_tool_err else "",
        }

    return {
        "repo": name,
        "baseline_cold": _compact(base_cold),
        "baseline_warm": _compact(base_warm),
        "rt_obs_cold": {**_compact(rt_cold), "overhead_pct": cold_oh, "shards": cold_shard_stats},
        "rt_obs_warm": {**_compact(rt_warm), "overhead_pct": warm_oh, "shards": warm_shard_stats},
        "check_git_post_ms": chk_git["wall_ms"],
        "rt_obs_cap8_warm": {**cap8, "overhead_pct": cap8_oh},
        "compat_pytest_cov": {"baseline": _compact(cov_base), "monitored": {**_compact(cov_res), "overhead_pct": cov_oh, "shards": cov_shard_stats}},
        "compat_pytest_xdist_n2": {"baseline": _compact(xdist_base), "monitored": {**_compact(xdist_res), "overhead_pct": xdist_oh, "shards": xdist_shard_stats}},
    }


def main() -> None:
    t0 = time.perf_counter()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SHARD_KEEP_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for t in TARGETS:
        results.append(run_repo_suite(t))

    warm_overheads = [r["rt_obs_warm"]["overhead_pct"] for r in results if r["rt_obs_warm"]["overhead_pct"] is not None]
    cold_overheads = [r["rt_obs_cold"]["overhead_pct"] for r in results if r["rt_obs_cold"]["overhead_pct"] is not None]
    summary = {
        "repos_tested": [r["repo"] for r in results],
        "median_cold_overhead_pct": round(statistics.median(cold_overheads), 2) if cold_overheads else None,
        "median_warm_overhead_pct": round(statistics.median(warm_overheads), 2) if warm_overheads else None,
        "all_warm_rt_obs_passed": all(r["rt_obs_warm"]["returncode"] == 0 for r in results),
        "all_pytest_cov_passed": all(r["compat_pytest_cov"]["monitored"]["returncode"] == 0 for r in results),
        "all_xdist_passed": all(r["compat_pytest_xdist_n2"]["monitored"]["returncode"] == 0 for r in results),
        "any_tool_or_hook_warnings": any(
            (r[k]["monitored"] if k.startswith("compat_") else r[k]).get("has_tool_or_hook_warning")
            for r in results
            for k in ("rt_obs_cold", "rt_obs_warm", "compat_pytest_cov", "compat_pytest_xdist_n2")
        ),
        "wall_seconds": round(time.perf_counter() - t0, 2),
    }
    payload = {"summary": summary, "results": results}
    (OUT_DIR / "part_a_stress.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("\n=== PART A STRESS SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
