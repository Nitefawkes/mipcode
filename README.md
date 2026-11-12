# MipCode v0.6 research preview

[![CI](https://github.com/Nitefawkes/mipcode/actions/workflows/ci.yml/badge.svg)](https://github.com/Nitefawkes/mipcode/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

MipCode checks a Python change for places it left inconsistent. When a change alters a function's contract, the `mip-delta` command names the other files and lines that may need the same change: a caller that omits a newly required argument, a wrapper that accepts a new option and does not pass it on, an override that no longer matches its base.

Run it before a commit, on a pull request, or after a merge. It reads source only and needs no configuration. Most changes produce no output. Treat what it prints as a prompt for review, because most of its reports are not defects.

## What it finds

The httpx commit that added `Client(http1=False)` gave the async client's proxy transport an `http1` parameter and never used it. The change is type-correct and passed review. Run on that commit, the check prints one line:

```text
[mip-delta] httpx/_client.py:1402 parameter_sink_review[static] AsyncClient._init_proxy_transport (new parameter http1 is not read in the function body; verify its intended behavioral sink)
```

Upstream fixed the omission later, in httpx #3023.

We replayed the check over recent history in ten open-source repositories that it had not been run on. Each plan, with its thresholds, was committed before its run.

- **It is quiet.** The ten repositories had 1,045 commits that changed a function's contract. 93.1% of them produced no report in the [first five repositories](results/held_out/README.md) and 97.4% in the [second five](results/held_out/round2/README.md), which ran a narrower set of checks.
- **It finds omissions that passed review.** In 10 of those commits, the checks that both rounds ran reported a place the author had missed. Upstream later fixed the omission in six of them. The other four belong to two gaps in rich with no later fix that we found.
- **Those omissions are type-correct code.** A new optional parameter that one layer accepts and does not forward passes a type checker.
- **Most reports are not omissions.** About one report in three was a real omission or worth a reviewer's look. The rest were false.
- **A type checker covers stale calls as well or better.** For calls that no longer match a changed or removed function, mypy reported as much as the check or more.

The causes of three kinds of false report were fixed after those runs. A [regression check](results/held_out/regression/README.md) on the same repositories shows the current default set silent on 96.5% of the contract-changing commits, with a real omission or a site worth review in 18 of the 37 commits it flags. Those figures are fitted to these repositories and are not a new validation.

## Quick start

From the repository root, activate a fresh virtual environment with Python 3.10 or later.

```sh
python -m pip install -e .
mip-delta --help
mip-delta --check-git --root . --json
```

The Git check compares the working tree with `HEAD` by default. A clean checkout returns an empty findings list. Omit `--json` for compact text.

Try a bundled example that adds a required keyword. The command checks the proposed edit in memory.

```sh
mip-delta --root examples/contract-delta/before --write-path formatter.py --candidate-file examples/contract-delta/after/formatter.py
```

The report points to `consumer.py:5` and names `required_keyword_missing`.

## Where to run it

- **Before a commit.** A [pre-commit hook](docs/usage.md#run-before-a-commit) stops the commit while reported sites are open. A site you judge intentional can be waived.
- **On a pull request.** A [CI job](docs/usage.md#run-on-a-pull-request) compares the pull request with its base branch and prints the report in the job log.
- **After a merge.** Two branches can merge without a textual conflict and still disagree about a contract. A [merge check](docs/usage.md#check-a-merge) compares the merged tree with the merge base.

A reported site stays open until a later edit resolves it. With `--ledger`, repeated checks name each site once, `--open` lists what remains, and `--waive` records an intentional one. The [usage guide](docs/usage.md) covers these, the checks that are off by default, and observing calls during tests.

## With coding agents

*(Looking for v0.5? MipCode started as an experimental MCP architectural-mipmap server in [`src/mipcode/`](src/mipcode/) and [`src/semantic_lod/`](src/semantic_lod/); benchmarks showed custom navigation tools lost to `ripgrep`, so v0.6+ pivoted to `mip-delta`—see [`docs/release-notes-v0.6.md`](docs/release-notes-v0.6.md) for v0.5 pointers.)*

MipCode began as a check for coding agents. In our early transcripts, agents failed multi-file tasks because they stopped early: the edited file looked finished, the existing tests passed, and the agent never looked for the caller it had just broken. In paired Codex runs on 14 selected tasks, agents that received the report passed 14 and agents without it passed 8. Most of those tasks were constructed, and a generic reminder to look again later recovered five of six selected misses.

The newer evidence is weaker for this use. In a [small pilot](results/pilot/README.md), a current frontier model was given three tasks built from omissions that human authors had made. It covered the missed place in six of six sessions with no help, so the pilot stopped before comparing the check with a reminder.

The check can still run as [Claude Code hooks](docs/usage.md#run-as-claude-code-hooks), which add new sites after an edit and list open ones before the agent finishes. Treat the hooks as experimental.

## Research and development

- [Held-out validation, round one](results/held_out/README.md) and [round two](results/held_out/round2/README.md) report the ten-repository replays, the reading of reported sites, recall on constructed omissions, and the mypy comparison. The [regression check](results/held_out/regression/README.md) reruns them after the detector fixes.
- [Live pilot](results/pilot/README.md) reports the six agent sessions and why the pilot stopped.
- [History replay](results/history_replay/README.md) runs the check over 2,944 upstream commits in the five repositories the checks were tuned on.
- [Engineering article](docs/engineering_blog_post_mipdelta.md) explains the move from search tools to checks after edits and tests.
- [Evidence index](results/v2_delta_readout/README.md) links the earlier paired-task reports, data, and study limits.
- [One-shot brief pilot](results/one_shot/README.md) compares no brief, a lexical map, and a Semantic LOD brief.
- [Documentation index](docs/README.md) covers the preview and the earlier architecture.

```sh
python -m pip install -e ".[test]"
python -m pytest tests -q
```

[Apache 2.0 license](LICENSE)
