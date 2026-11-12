"""Run one pilot episode: a headless Claude Code session working on one task in one arm.

The episode starts from a fresh directory holding the task's parent tree as a single
commit, so no later history is visible. The arm decides which hooks the session gets:

- plain:    no hooks
- reminder: a Stop hook that blocks once with a generic consistency reminder
- ledger:   the mip-delta post-edit and stop hooks

The script writes the transcript, the final patch, the hook event log, and a summary.
It refuses to start when the episode's cap would take total spending over the limit.
Standard library only.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

INSTRUCTIONS = (
    "Implement this request in the repository in the current directory. "
    "Work only on the library source. Do not write or change tests or documentation. "
    "The project's dependencies are not installed, so do not try to run the code or the tests. "
    "When you are finished, reply with a short summary of what you changed."
)
ARMS = ("plain", "reminder", "ledger")
TOOLS = ("Read", "Edit", "Write", "Grep", "Glob")


def run(cmd: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, check=True, **kwargs)


def materialize(clone: Path, commit: str, target: Path) -> None:
    """Write one commit's tree into target as a new repository with a single commit."""
    archive = subprocess.run(["git", "-C", str(clone), "archive", commit], capture_output=True, check=True).stdout
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    run(["git", "init", "-q"], target)
    run(["git", "config", "user.email", "episode@example.invalid"], target)
    run(["git", "config", "user.name", "Episode"], target)
    run(["git", "config", "core.autocrlf", "false"], target)
    run(["git", "add", "-A"], target)
    run(["git", "commit", "-q", "-m", "base"], target)
    # Keep harness state out of the session's view of the working tree.
    with open(target / ".git" / "info" / "exclude", "a", encoding="utf-8") as handle:
        handle.write(".claude/\n.mipdelta/\n")


def hook(command: str) -> dict:
    return {"type": "command", "command": command, "timeout": 30}


def settings_for(arm: str, hook_dir: Path) -> dict | None:
    python = Path(sys.executable).as_posix()
    if arm == "plain":
        return None
    if arm == "reminder":
        return {"hooks": {"Stop": [{"hooks": [hook(f'"{python}" "{(hook_dir / "reminder_hook.py").as_posix()}"')]}]}}
    engine = f'"{python}" "{(hook_dir / "mipdelta_hook.py").as_posix()}"'
    return {
        "hooks": {
            "PostToolUse": [{"matcher": "Edit|Write", "hooks": [hook(f"{engine} claude-post-edit")]}],
            "Stop": [{"hooks": [hook(f"{engine} claude-stop")]}],
        }
    }


def spent(ledger: Path) -> float:
    if not ledger.exists():
        return 0.0
    return sum(json.loads(line)["cost_usd"] for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip())


def summarize_transcript(path: Path) -> dict:
    edits = stop_feedback = 0
    result: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "result":
            result = row
        content = row.get("message", {}).get("content") if row.get("type") in ("assistant", "user") else None
        for part in content if isinstance(content, list) else []:
            if part.get("type") == "tool_use" and part.get("name") in ("Edit", "Write"):
                edits += 1
            if row["type"] == "user" and part.get("type") == "text" and part.get("text", "").startswith("Stop hook feedback"):
                stop_feedback += 1
    return {
        "cost_usd": result.get("total_cost_usd"),
        "turns": result.get("num_turns"),
        "is_error": result.get("is_error"),
        "terminal_reason": result.get("terminal_reason"),
        "permission_denials": len(result.get("permission_denials") or []),
        "edit_calls": edits,
        "stop_hook_blocks": stop_feedback,
        "final_message": result.get("result"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--rep", type=int, required=True)
    parser.add_argument("--clone", type=Path, required=True, help="Local clone that holds the task's parent commit")
    parser.add_argument("--work", type=Path, required=True, help="Directory for episode working trees")
    parser.add_argument("--out", type=Path, required=True, help="Directory for episode results")
    parser.add_argument("--hook-dir", type=Path, required=True, help="Directory holding the hook scripts; must not be shared-writable")
    parser.add_argument("--spend-ledger", type=Path, required=True)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--budget-usd", type=float, default=1.5)
    parser.add_argument("--total-cap-usd", type=float, required=True)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args(argv)

    task = json.loads(args.task.read_text(encoding="utf-8"))
    already = spent(args.spend_ledger)
    if already + args.budget_usd > args.total_cap_usd:
        print(f"refusing to start: spent {already:.2f}, cap {args.total_cap_usd:.2f}", file=sys.stderr)
        return 3

    episode = f"{task['task_id']}-{args.arm}-{args.rep}-{uuid.uuid4().hex[:8]}"
    # A unique path per episode: headless sessions keep per-directory memory.
    workdir = args.work.resolve() / episode
    outdir = args.out.resolve() / episode
    outdir.mkdir(parents=True)
    materialize(args.clone.resolve(), task["parent_commit"], workdir)
    settings = settings_for(args.arm, args.hook_dir.resolve())
    if settings is not None:
        (workdir / ".claude").mkdir()
        (workdir / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    command = [
        shutil.which("claude") or "claude", "-p",
        "--model", args.model,
        "--max-budget-usd", str(args.budget_usd),
        "--output-format", "stream-json", "--verbose",
        "--setting-sources", "project",
        "--strict-mcp-config", "--disable-slash-commands",
        "--permission-mode", "acceptEdits",
        "--allowedTools", *TOOLS,
    ]
    prompt = task["ticket"].strip() + "\n\n" + INSTRUCTIONS + "\n"
    started = time.time()
    timed_out = False
    with open(outdir / "transcript.jsonl", "wb") as transcript, open(outdir / "stderr.txt", "wb") as errors:
        try:
            proc = subprocess.run(
                command, cwd=workdir, env=env, input=prompt.encode("utf-8"),
                stdout=transcript, stderr=errors, timeout=args.timeout, check=False,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, None

    run(["git", "add", "-A"], workdir)
    patch = subprocess.run(["git", "diff", "--cached", "HEAD"], cwd=workdir, capture_output=True, check=True).stdout
    (outdir / "patch.diff").write_bytes(patch)
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "HEAD"], cwd=workdir, capture_output=True, check=True, text=True
    ).stdout.split()
    state = workdir / ".mipdelta"
    if state.is_dir():
        for log in state.glob("*.jsonl"):
            shutil.copy(log, outdir / log.name)

    summary = {
        "episode": episode,
        "task_id": task["task_id"],
        "arm": args.arm,
        "rep": args.rep,
        "model": args.model,
        "budget_usd": args.budget_usd,
        "wall_seconds": round(time.time() - started, 1),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "changed_files": names,
        "patch_bytes": len(patch),
        **summarize_transcript(outdir / "transcript.jsonl"),
    }
    (outdir / "episode.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    # A run that dies without a result line is charged its full cap, to stay under the limit.
    cost = summary["cost_usd"] if summary["cost_usd"] is not None else args.budget_usd
    with open(args.spend_ledger, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"run": episode, "cost_usd": cost}) + "\n")
    print(json.dumps({k: summary[k] for k in ("episode", "cost_usd", "turns", "edit_calls", "stop_hook_blocks", "changed_files", "terminal_reason")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
