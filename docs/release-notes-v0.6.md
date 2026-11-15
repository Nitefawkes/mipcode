# MipCode v0.6 research preview (v0.6.0 – v0.6.2)

Use `mip-delta` to check proposed edits or Git working tree changes for possible Python caller and contract breaks. Its short advisories point to source locations for review. For supported environments (CPython 3.12+), `--run-pytest` adds evidence about calls executed during tests. Start with the [quick start](../README.md#quick-start) and [usage guide](usage.md).

## Updates through v0.6.2

- **Pre-commit & CI integration:** Added root [`.pre-commit-hooks.yaml`](../.pre-commit-hooks.yaml) (`rev: v0.6.2`, `id: mip-delta`) and documented stateless `--check-git --base-ref` CI checks in the [usage guide](usage.md#run-before-a-commit).
- **Optional Stage-2 LLM micro-triage (`--review`):** Added zero-dependency `--review` confirmation (using `ANTHROPIC_API_KEY` over stdlib `urllib` or local `claude -p`) only when Stage 1 emits findings, benchmarked in [Experiment 1](../results/two_stage_sieve/README.md).
- **Stage-1 detector & noise refinements:** Demoted `state_field_review` to opt-in, added dead `self.<param>` attribute-store detection in `parameter_sink_review`, broadened `--review` async timing guidance, and excluded vendored subtrees (`is_vendored_path`) plus Invoke `@task` context parameters.
- **Hardened `--run-pytest` & scoped `ObligationLedger`:** Fixed 5 PEP 669 runtime observer bugs (`unittest.mock` proxies, `PYTHONPATH` shadowing, MRO/`callee_qualname`, line-span disambiguation, and `<locals>` normalization), lowered `--run-pytest` default `max_events_per_site` to `1`, scoped `ObligationLedger` strictly to hook/session deduplication per [Experiment 2](../results/audit_rt_ledger/README.md), and exposed `--version` / `__version__`.

## Background & earlier work

The [engineering article](engineering_blog_post_mipdelta.md) and [evidence index](../results/v2_delta_readout/README.md) describe the original paired tasks and the generic reminder comparison. The experimental [one-shot compiler](../experiments/one_shot/README.md) is a separate entry point. In its small [live pilot](../results/one_shot/README.md), all arms accepted three patches. No brief used the fewest total tokens. The earlier v0.5 MCP navigation work remains available as [historical documentation](NAMING_AND_TOOL_ALIGNMENT_V05.md).

The preview is intended for evaluation on Python repositories. A quiet result reflects the scope of the configured analysis. Review warnings alongside the ticket and repository checks.
