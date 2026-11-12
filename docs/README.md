# Documentation

- [Quick start](../README.md#quick-start) installs the preview and shows a caller warning.
- [Usage](usage.md) covers Git checks, candidate edits, the checks that are off by default, the obligation ledger, pre-commit, pull request and merge checks, agent hooks, runtime observations, and exit codes.
- [Held-out validation](../results/held_out/README.md) and its [second round](../results/held_out/round2/README.md) report the check's behavior on ten repositories it had not been run on.
- [Live pilot](../results/pilot/README.md) reports six agent sessions on tasks built from omissions in upstream history.
- [Engineering article](engineering_blog_post_mipdelta.md) describes the experiments and remaining questions.
- [Research evidence](../results/v2_delta_readout/README.md) lists reports and machine-readable data.
- [One-shot brief pilot](../results/one_shot/README.md) reports the offline and live comparisons.
- [History replay](../results/history_replay/README.md) reports the check's behavior on upstream commit history, with its [protocol](../experiments/history_replay/README.md).
- [v0.6 release notes](release-notes-v0.6.md) summarize the preview scope.
- [v0.5 architecture](NAMING_AND_TOOL_ALIGNMENT_V05.md), [design notes](../ANISOTROPIC_INSIGHTS.md), and [earlier benchmark results](../BENCHMARK_REPORT.md) cover the earlier navigation and MCP tools.

The preview quick start uses `mip-delta` in `src/mipdelta`. The earlier v0.5 MCP tools have their own setup. In the audit of the earlier public revision, MCP 1.26.0 with Pydantic 2.10.4 failed during tool registration.
