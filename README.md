# MipCode v0.6 research preview

[![CI](https://github.com/Nitefawkes/mipcode/actions/workflows/ci.yml/badge.svg)](https://github.com/Nitefawkes/mipcode/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

MipCode checks a Python change for places it left inconsistent. When a change alters a function's contract, the `mip-delta` command names the other files and lines that may need the same change: a caller that omits a newly required argument, a wrapper that accepts a new option and does not pass it on, an override that no longer matches its base.

Run it before a commit, on a pull request, or after a merge. It reads source only and needs no configuration. Most changes produce no output. Treat what it prints as a prompt for review, because most of its reports are not defects.

## What it finds

In `httpx` (#1624, later fixed in #3023), adding `Client(http1=False)` forwarded `http1=http1` everywhere except `AsyncClient._init_proxy_transport`:

```diff
     def _init_proxy_transport(
         self,
         proxy: Proxy,
         verify: VerifyTypes = True,
         cert: CertTypes = None,
+        http1: bool = True,
         http2: bool = False,
         limits: Limits = DEFAULT_LIMITS,
         trust_env: bool = True,
     ) -> AsyncBaseTransport:
         return AsyncHTTPTransport(
             verify=verify,
             cert=cert,
             http2=http2,
             limits=limits,
             trust_env=trust_env,
             proxy=proxy,
         )
```

Accepting an unused optional parameter with a default is valid Python typing, so a type checker flags zero errors on those changed lines. Run on that commit, `mip-delta` prints one line:

```text
[mip-delta] httpx/_client.py:1402 parameter_sink_review[static] AsyncClient._init_proxy_transport (new parameter http1 is not read in the function body; verify its intended behavioral sink)
```

Across held-out replays of mature repos, this same class of checks caught silent parameter drops and unawaited async calls in `httpx` (#3023), `paramiko` (`AgentKey.sign_ssh_data`, `disable_algorithms`), `scrapy` (#7730), and `rich` (`Traceback.__init__` wrapper drops related to [Textualize/rich#3301](https://github.com/Textualize/rich/issues/3301)).

We replayed the check over recent history in ten open-source repositories that it had not been run on. Each plan, with its thresholds, was committed before its run.

- **It is quiet.** The ten repositories had 1,045 commits that changed a function's contract. 93.1% of them produced no report in the [first five repositories](results/held_out/README.md) and 97.4% in the [second five](results/held_out/round2/README.md), which ran a narrower set of checks.
- **It finds omissions that passed review.** In 10 of those commits, the checks that both rounds ran reported a place the author had missed. Upstream later fixed the omission in six of them. The other four belong to two gaps in rich with no later fix that we found.
- **Those omissions are type-correct code.** A new optional parameter that one layer accepts and does not forward passes a type checker.
- **Most reports are not omissions.** About one report in three was a real omission or worth a reviewer's look. The rest were false.
- **A type checker covers stale calls as well or better.** For calls that no longer match a changed or removed function, mypy reported as much as the check or more.

The causes of three kinds of false report were fixed after those runs. A [regression check](results/held_out/regression/README.md) on the same repositories shows the baseline default set (`f4361fd`) silent on 96.5% of the contract-changing commits, with a real omission or a site worth review in 18 of the 37 commits it flags. Those figures are fitted to these repositories and are not a new validation.

### Two-Stage Precision Sieve (Optional LLM Micro-Triage)

Why not simply ask a fast LLM (`claude-haiku-5-5`) to inspect every git diff instead of running AST checks first? In [Experiment 1](results/two_stage_sieve/README.md), we benchmarked unguided Haiku diff review against AST-gated two-stage triage across all `37` flagged commits plus `30` silent contract-changing controls (`N = 67` blinded commits, no workspace tools) from the `f4361fd` replay:

| Arm | Flagged Kept (`R+RW+F+S`) | Useful Prec | Strict Prec | Real Recall (`/13`) | Silent FP (`/30`) | LLM Calls | Nominal Cost |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Arm A:** `mip-delta` deterministic (`f4361fd`) | `13+5+19+0 = 37` | 48.6% | 35.1% | 13/13 (100.0%) | 0/30 (0.0%) | 0 | `$0.0000` |
| **Arm B:** Two-stage (`mip-delta` + `--review` Haiku, keep) | `10+2+1+0 = 13` | **92.3%** | **76.9%** | 10/13 (76.9%) | **0/30 (0.0%)** | 37 | `$0.0882` |
| **Arm C:** Unguided Haiku on diff (`confirm` + `review_worthy`) | `11+3+12+14 = 40` | 35.0% | 27.5% | 11/13 (84.6%) | **14/30 (46.7%)** | 67 | `$0.1367` |
| **Arm C (strict):** Unguided Haiku (`confirm_omission` only) | `6+1+2+0 = 9` | 77.8% | 66.7% | **6/13 (46.2%)** | 0/30 (0.0%) | 67 | `$0.1367` |

- **Two stages beat diff-only LLM review on precision and noise:** Unguided Haiku either floods clean commits with `46.7%` false warnings (`35.0%` precision) or, when restricted to high-confidence alerts, misses over half of real omissions (`46.2%` strict recall — including `httpx#3023`). With Stage 1 suppressing `96.5%` of commits at `$0`, Haiku triage on the remaining `3.5%` cuts false alarms by `94.7%` (`18/19`) for ~`$0.0024` per flagged commit (`9/13` real recall and `100.0%` useful precision on `confirm_omission` alone).
- **Available as `--review`:** `mip-delta --check-git --review` runs Stage-2 triage only when Stage 1 emits findings, using `ANTHROPIC_API_KEY` (stdlib `urllib`) or an authenticated `claude` CLI with zero extra dependencies.
- **Post-Experiment-1 Stage-1 & rubric refinements:** Demoting `state_field_review` to opt-in lifted deterministic precision (`48.6% -> 50.0%`, `37 -> 36` flagged commits, `0` recall loss), adding dead `self.<param>` attribute-store detection in `parameter_sink_review` added `0` false alarms across the 8,000-commit replay while directly flagging `rich:c856e6b46901` (`locals_hide_dunder`, `locals_hide_sunder`) and `urllib3:52392654b301` (`version_string`), broadening the `--review` async rubric (`eager-to-deferred` execution) recovered `scrapy:9bfa58e36cca` (`confirm_omission`) while keeping `httpx:e0b4528b170b` suppressed, and excluding vendored subtrees (`vendor/`, `_vendor/`, `third_party/`, `<pkg>/packages/`) plus Invoke `@task` context args eliminated `24/24` targeted persistent FPs (`70 -> 46` merge-head open sites, `0` TPs lost) across the Experiment 2 PR set.

### Runtime Observation & Ledger Audit (Experiment 2)

To make sure every subsystem in `src/mipdelta/` earned its keep against real test suites and multi-commit PRs at `$0` API cost, [Experiment 2](results/audit_rt_ledger/README.md) audited both optional subsystems across four Python repositories (`marshmallow`, `rich`, `httpx`, `mipcode`) and 20 multi-commit upstream PRs (`74` commits across 8 repositories):

- **Opt-in PEP 669 runtime observation (`--run-pytest`, `max_events_per_site=1` default):** Default `mip-delta --check-git` stays zero-dependency static AST (~5 ms). As an explicit **opt-in dynamic supplement**, fixing 5 runtime/MRO lookup defects exposed by the audit turned `--run-pytest` into `0` tool-ID/Mock crashes (`pytest-cov` and `pytest-xdist` clean), **`+5.26%` cold / `+14.17%` median warm `pytest` overhead**, and **`27/30 (90.0%)` mutation recall** (`14/15 = 93.3%` on optional delegating wrappers where installed `mypy` is `0.0%`; `+56.7%` net recall lift over static AST + `mypy`).
- **Session & multi-push deduplication (`--ledger`, `ObligationLedger`):** Single-shot CI PR checks get identical results from stateless `--base-ref`, because WIP commits only naturally resolved `9.1%–13.2%` of opened sites before merge. We keep `ledger.py` scoped strictly to **agent-hook / multi-push deduplication**, where it cut repeat push alerts by **`52.7%–66.2%`** with **`0` anchor churn across `19` line shifts**.

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

- **Before a commit.** Add the [pre-commit hook](.pre-commit-hooks.yaml) to `.pre-commit-config.yaml` (`rev: main`, `id: mip-delta`) to stop the commit while reported sites are open. A site you judge intentional can be `--waive`d (see [usage guide](docs/usage.md#run-before-a-commit)).
- **On a pull request.** Copy the [GitHub Actions job](docs/usage.md#run-on-a-pull-request) to compare a pull request against its base branch and print advisories in the CI log.
- **After a merge.** Two branches can merge without a textual conflict and still disagree about a contract. A [merge check](docs/usage.md#check-a-merge) compares the merged tree with the merge base.

A reported site stays open until a later edit resolves it. Stateless `--base-ref` covers single-shot CI PR gates; when running repeated local checks or agent hooks, `--ledger` deduplicates open sites (`0` churn across line edits), `--open` lists what remains, and `--waive` records an intentional one. The [usage guide](docs/usage.md) covers these, opt-in checks, and observing dynamic calls during tests (`--run-pytest`).

## With coding agents

*(Looking for v0.5? MipCode started as an experimental MCP architectural-mipmap server in [`src/mipcode/`](src/mipcode/) and [`src/semantic_lod/`](src/semantic_lod/); benchmarks showed custom navigation tools lost to `ripgrep`, so v0.6+ pivoted to `mip-delta`—see [`docs/release-notes-v0.6.md`](docs/release-notes-v0.6.md) for v0.5 pointers.)*

MipCode began as a check for coding agents. In our early transcripts, agents failed multi-file tasks because they stopped early: the edited file looked finished, the existing tests passed, and the agent never looked for the caller it had just broken. In paired Codex runs on 14 selected tasks, agents that received the report passed 14 and agents without it passed 8. Most of those tasks were constructed, and a generic reminder to look again later recovered five of six selected misses.

The newer evidence is weaker for this use. In a [small pilot](results/pilot/README.md), a current frontier model was given three tasks built from omissions that human authors had made. It covered the missed place in six of six sessions with no help, so the pilot stopped before comparing the check with a reminder.

The check can still run as [Claude Code hooks](docs/usage.md#run-as-claude-code-hooks), which add new sites after an edit and list open ones before the agent finishes. Treat the hooks as experimental.

## Research and development

- [Two-stage precision sieve](results/two_stage_sieve/README.md) compares AST-only `mip-delta`, AST-gated `--review` Haiku triage, and unguided Haiku diff review across 67 blinded commits from the ten-repository replay.
- [Experiment 2 audit](results/audit_rt_ledger/README.md) stress-tests opt-in `--run-pytest` PEP 669 dynamic call observation across four real test suites (`90.0%` mutation recall, `+14.17%` median warm overhead) and audits `ObligationLedger` across 20 multi-commit upstream PRs (`0` anchor churn across line shifts, scoped strictly to hook/session deduplication).
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
