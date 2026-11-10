# MipCode v0.6.0 research preview

Use `mip-delta` to check proposed edits or Git working tree changes for possible Python caller and contract breaks. Its short advisories point to source locations for review. For supported environments, the monitored test runner adds evidence about calls executed during tests. Start with the [quick start](../README.md#quick-start) and [usage guide](usage.md).

The [engineering article](engineering_blog_post_mipdelta.md) and [evidence index](../results/v2_delta_readout/README.md) describe the original paired tasks and the generic reminder comparison. The experimental [one-shot compiler](../experiments/one_shot/README.md) is a separate entry point. In its small [live pilot](../results/one_shot/README.md), all arms accepted three patches. No brief used the fewest total tokens. The earlier v0.5 MCP navigation work remains available as [historical documentation](NAMING_AND_TOOL_ALIGNMENT_V05.md).

The preview is intended for evaluation on Python repositories. A quiet result reflects the scope of the configured analysis. Review warnings alongside the ticket and repository checks.
