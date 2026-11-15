"""Run the 24-repository Cold Repo Bug-Hunter sweep (`mip-delta --audit-repo`).

Clones shallow ``--depth 1`` snapshots of 23 popular open-source Python repositories
into ``--work-dir`` (outside this repo) plus evaluates MipCode itself at ``.``,
captures Stage-1 static audit outputs, optionally runs Stage-2 ``--review`` on
flagged repositories, and writes sanitized summaries to ``results/cold_audit/``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "results" / "cold_audit"
NOISE_GATE_THRESHOLD = 50

TARGETS: tuple[tuple[str, str], ...] = (
    ("httpx", "https://github.com/encode/httpx.git"),
    ("marshmallow", "https://github.com/marshmallow-code/marshmallow.git"),
    ("rich", "https://github.com/Textualize/rich.git"),
    ("attrs", "https://github.com/python-attrs/attrs.git"),
    ("mkdocs", "https://github.com/mkdocs/mkdocs.git"),
    ("paramiko", "https://github.com/paramiko/paramiko.git"),
    ("pytest", "https://github.com/pytest-dev/pytest.git"),
    ("requests", "https://github.com/psf/requests.git"),
    ("scrapy", "https://github.com/scrapy/scrapy.git"),
    ("urllib3", "https://github.com/urllib3/urllib3.git"),
    ("click", "https://github.com/pallets/click.git"),
    ("flask", "https://github.com/pallets/flask.git"),
    ("werkzeug", "https://github.com/pallets/werkzeug.git"),
    ("jinja2", "https://github.com/pallets/jinja.git"),
    ("fastapi", "https://github.com/fastapi/fastapi.git"),
    ("typer", "https://github.com/fastapi/typer.git"),
    ("starlette", "https://github.com/encode/starlette.git"),
    ("uvicorn", "https://github.com/encode/uvicorn.git"),
    ("anyio", "https://github.com/agronholm/anyio.git"),
    ("tenacity", "https://github.com/jd/tenacity.git"),
    ("aiohttp", "https://github.com/aio-libs/aiohttp.git"),
    ("black", "https://github.com/psf/black.git"),
    ("textual", "https://github.com/Textualize/textual.git"),
    ("mipcode", "https://github.com/Nitefawkes/mipcode.git"),
)

_LOCAL_PATH_RE = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[/\\]|file://)", re.ASCII)


def _git_head_sha(repo_dir: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
    ).strip()


def _ensure_checkout(name: str, url: str, work_dir: Path, *, pull: bool = False) -> tuple[Path, str]:
    if name == "mipcode":
        return ROOT, _git_head_sha(ROOT)
    dest = work_dir / name
    if dest.exists() and (dest / ".git").exists():
        if pull:
            subprocess.run(
                ["git", "-C", str(dest), "fetch", "--depth", "1", "origin"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
        return dest, _git_head_sha(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest, _git_head_sha(dest)


def _run_cli_audit(repo_dir: Path, *, review: bool = False, review_model: str | None = None) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "mipdelta.cli",
        "--root",
        str(repo_dir),
        "--audit-repo",
        "--json",
    ]
    if review:
        cmd.append("--review")
    if review_model:
        cmd.extend(["--review-model", review_model])
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    if proc.returncode not in {0, 1}:
        raise RuntimeError(
            f"mip-delta --audit-repo failed on {repo_dir.name} (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    payload = json.loads(proc.stdout)
    payload["process_wall_ms"] = elapsed_ms
    return payload


def _assert_no_local_paths(obj: Any) -> None:
    dumped = json.dumps(obj, ensure_ascii=False)
    match = _LOCAL_PATH_RE.search(dumped)
    if match is not None:
        raise ValueError(f"Sanitized summary contains local path pattern {match.group(0)!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run 24-repo Cold Repo Bug-Hunter sweep.")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "cold-audit-repos",
        help="Directory outside this repo where shallow target clones are cached.",
    )
    parser.add_argument("--pull", action="store_true", help="Refresh existing shallow clones to upstream HEAD.")
    parser.add_argument("--review", action="store_true", help="Also run Stage-2 --review on flagged repos.")
    parser.add_argument("--review-model", default=None, help="Optional --review-model override.")
    parser.add_argument(
        "--repos",
        nargs="*",
        default=None,
        help="Optional subset of repository names to audit.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=RESULTS_DIR / "summary.json",
        help="Output JSON path (must remain sanitized).",
    )
    args = parser.parse_args(argv)

    selected = set(args.repos) if args.repos else None
    targets = [(n, u) for n, u in TARGETS if selected is None or n in selected]
    if not targets:
        raise SystemExit("No repositories matched --repos")

    existing_reviews: dict[str, dict[str, Any]] = {}
    if args.out.exists() and not args.review:
        try:
            prior = json.loads(args.out.read_text(encoding="utf-8"))
            for row in prior.get("repositories", []):
                if isinstance(row, dict) and "review" in row and "repo" in row:
                    existing_reviews[row["repo"]] = row["review"]
        except (OSError, json.JSONDecodeError):
            pass

    repo_rows: list[dict[str, Any]] = []
    noisy_repos: list[tuple[str, int]] = []

    for name, url in targets:
        repo_dir, head_sha = _ensure_checkout(name, url, args.work_dir, pull=args.pull)
        stage1 = _run_cli_audit(repo_dir, review=False)
        findings = stage1.get("findings", [])
        n_findings = len(findings)
        if n_findings > NOISE_GATE_THRESHOLD:
            noisy_repos.append((name, n_findings))

        row: dict[str, Any] = {
            "repo": name,
            "url": url,
            "head_sha": head_sha,
            "head_sha_short": head_sha[:12],
            "files_scanned": stage1["files_scanned"],
            "skipped_unparsed_count": len(stage1.get("skipped_unparsed_paths", [])),
            "candidate_calls_inspected": stage1["candidate_calls_inspected"],
            "wall_ms": stage1["wall_ms"],
            "process_wall_ms": stage1["process_wall_ms"],
            "stage1_findings_count": n_findings,
            "advisory_text": stage1.get("advisory_text", ""),
            "findings": findings,
        }
        if name in existing_reviews and not args.review and n_findings > 0:
            row["review"] = existing_reviews[name]
        repo_rows.append(row)
        print(
            f"[stage1] {name:12s} sha={head_sha[:10]} files={stage1['files_scanned']:4d} "
            f"calls={stage1['candidate_calls_inspected']:5d} wall={stage1['wall_ms']:7.1f}ms "
            f"findings={n_findings}",
            flush=True,
        )

    if noisy_repos:
        print(
            f"[GATE] Noise threshold ({NOISE_GATE_THRESHOLD}) exceeded by: {noisy_repos}. "
            "Investigate deterministic filtering before running Stage-2 --review.",
            file=sys.stderr,
        )

    if args.review and not noisy_repos:
        for row in repo_rows:
            if row["stage1_findings_count"] == 0:
                continue
            repo_dir = ROOT if row["repo"] == "mipcode" else args.work_dir / row["repo"]
            stage2 = _run_cli_audit(repo_dir, review=True, review_model=args.review_model)
            row["review"] = stage2.get("review")
            verdict = (row["review"] or {}).get("verdict")
            print(
                f"[stage2] {row['repo']:12s} verdict={verdict} "
                f"wall={(row['review'] or {}).get('wall_ms')}ms "
                f"cost=${(row['review'] or {}).get('cost_usd')}",
                flush=True,
            )

    total_files = sum(r["files_scanned"] for r in repo_rows)
    total_calls = sum(r["candidate_calls_inspected"] for r in repo_rows)
    total_findings = sum(r["stage1_findings_count"] for r in repo_rows)
    flagged_repos = [r["repo"] for r in repo_rows if r["stage1_findings_count"] > 0]
    wall_sorted = sorted(r["wall_ms"] for r in repo_rows)
    median_wall = wall_sorted[len(wall_sorted) // 2] if wall_sorted else 0.0

    summary = {
        "experiment": "cold_audit",
        "mipdelta_sha": _git_head_sha(ROOT)[:12],
        "noise_gate_threshold": NOISE_GATE_THRESHOLD,
        "noise_gate_exceeded": [{"repo": n, "count": c} for n, c in noisy_repos],
        "totals": {
            "repositories": len(repo_rows),
            "files_scanned": total_files,
            "candidate_calls_inspected": total_calls,
            "repositories_silent": len(repo_rows) - len(flagged_repos),
            "repositories_flagged": len(flagged_repos),
            "stage1_findings": total_findings,
            "median_wall_ms": round(median_wall, 2),
        },
        "repositories": repo_rows,
    }
    _assert_no_local_paths(summary)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out.relative_to(ROOT)} ({len(repo_rows)} repos, {total_findings} findings)")
    return 2 if noisy_repos else 0


if __name__ == "__main__":
    raise SystemExit(main())
