---
name: mipcode
description: Run mip-delta static checks on Python and Solidity changes or repositories to surface unforwarded wrapper arguments, stale callers, unused parameters/state variables, and override mismatches.
version: v0.6.5
license: Apache-2.0
homepage: https://coherion.net/mipcode
repository: https://github.com/Nitefawkes/mipcode
---

# MipCode (`mip-delta`) Agent Skill

Use `mip-delta` (`coherion-mipcode` v0.6.5) after multi-file edits, before opening or merging a pull request, or when starting a Python or Solidity code audit.

> **Treat findings as pointers to inspect, never verdicts.** Always open and read the cited file and line before changing code or reporting an upstream issue.

## 1. Install (Python >= 3.10, zero runtime dependencies)

```sh
python -m pip install coherion-mipcode
mip-delta --version
```

`--check-git` and `--check-merge` require `git` in the workspace; `--audit-repo`, `--audit-solidity`, and `--before`/`--after` read files directly without git history.

## 2. Commands by Task

- **Check uncommitted Python changes against `HEAD`:**
  ```sh
  mip-delta --check-git --root . --json
  ```
- **Check a pull request branch against a baseline ref:**
  ```sh
  mip-delta --check-git --root . --base-ref main --json
  ```
- **Check a merge (`MERGE_HEAD`, `HEAD^1..HEAD^2`, or `--base-ref`):**
  ```sh
  mip-delta --check-merge --root . --json
  ```
- **Audit a resting Python codebase (no git diff required):**
  ```sh
  mip-delta --audit-repo --root . --json
  ```
- **Audit Solidity contracts (zero external Solidity toolchain required):**
  ```sh
  mip-delta --audit-solidity --root contracts/ --json
  ```
- **Compare a before/after Python or Solidity file pair directly:**
  ```sh
  mip-delta --before old_file.py --after new_file.py --json
  ```
- **Candidate check on one proposed Python replacement file:**
  ```sh
  mip-delta --root . --write-path pkg/mod.py --candidate-file candidate_mod.py --json
  ```
- **Session deduplication across repeated agent edits (`ObligationLedger`):**
  ```sh
  mip-delta --check-git --ledger .mipdelta/ledger.json --json
  mip-delta --check-git --ledger .mipdelta/ledger.json --open
  mip-delta --waive <site_id> --reason "Intentional compatibility parameter" --ledger .mipdelta/ledger.json
  ```
- **Optional Stage-2 LLM micro-triage (`ANTHROPIC_API_KEY` or local `claude` CLI; runs only when Stage 1 emits findings):**
  ```sh
  mip-delta --check-git --review --json
  ```
- **Opt-in Python 3.12+ `sys.monitoring` dynamic call observation during pytest:**
  ```sh
  mip-delta --run-pytest --pytest-args "-q" --root . --json
  ```
- **Enable opt-in noisy detectors only when desired (`callback_binding_review`, `parameter_truthiness_review`, `state_field_review`):**
  ```sh
  mip-delta --check-git --enable callback_binding_review --json
  ```

## 3. Reading Output & Exit Codes

- Pass `--json` (or `--format json`) for structured output (`findings[]`, `skipped_unparsed_paths`, and `review` when `--review` triggers). Pass `--format github` for GitHub Actions `::warning` annotations.
- Each finding includes `detector`, `symbol`, `path`, `start_line`, `end_line`, `resolution_source`, and `evidence`.
- Exit status is `0` on clean runs and (by default) on findings; pass `--fail-on-findings` to exit `1` when unwaived findings remain, and `2` on Git/parse/CLI configuration errors. `--max-bytes` caps compact text advisory length (default 250 bytes, or 600 for `--open`).

## 4. Pre-Commit, GitHub Action & Experimental Hooks

- **Pre-commit (`.pre-commit-config.yaml`):**
  ```yaml
  repos:
    - repo: https://github.com/Nitefawkes/mipcode
      rev: v0.6.4
      hooks:
        - id: mip-delta
  ```
- **GitHub Actions (`.github/workflows/mipcode.yml`):**
  ```yaml
  on: pull_request
  permissions: { contents: read }
  jobs:
    mipcode:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
          with: { fetch-depth: 0, persist-credentials: false }
        - uses: Nitefawkes/mipcode-action@fe72dc8f7ab4cb55375e268a36b0c36836b5770f # v1.0.1
  ```
  The PR check shows warnings for affected Python callers, including callers outside the diff. Set `fail-on-findings` to `'true'` to block the job.
- **Experimental Claude Code hooks:** `mip-delta --hook claude-post-edit` and `mip-delta --hook claude-stop` (treat as experimental; read JSON hook events from stdin and optionally combine with `--runtime-dir`).

## 5. Known Scope, Evidence & Limitations

- Supports **Python and Solidity only** (and Solidity support is newer than Python).
- Across the public finding ledger (transcribed 2026-11-16; no scan dates recorded): **20 findings over 14 distinct sites across 43 distinct repositories** (44 scan targets, 7,778 files), with **35 of 43 distinct repositories silent** and **4 of 14 sites published as false reports** (`false_report`).
- Fitted development benchmarks (`15 of 68` adjudicated replay sites useful across 5 Python repos; `18 of 37` flagged commits useful in baseline sieve Arm A over 67 commits) were measured on repositories the detectors had already seen and are not a forecast for unseen code.
- In our only measurement on unseen code (the frozen **8-repository Solidity holdout**, `448` `.sol` files in `1.25 s`, `7 of 8` silent), `mip-delta` emitted `2` findings and **0 of 2** turned out to be real issues — too small a sample to estimate unseen precision.
- Where a published row carries `upstream_fixed`, another contributor fixed it in an independent pull request we did not write and had nothing to do with; human-spotted `adjacent_reports` (such as `WormholeGatewayAdapter._sendMessage` `#321`) were not emitted by any detector.

## 6. Public Discovery & Feedback

- Public overview: https://coherion.net/mipcode
- Agent discovery index: https://coherion.net/mipcode/llms.txt
- Hosted skill mirror: https://coherion.net/mipcode/skill.md
- Public findings JSON: https://coherion.net/mipcode/findings.json
- Methodology & results: https://coherion.net/mipcode/results
- Report false positives or edge cases: https://github.com/Nitefawkes/mipcode/issues
