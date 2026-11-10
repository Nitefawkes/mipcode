# One-shot brief comparison protocol

This offline pilot compares the initial evidence available before an agent can call any tools. Four fresh, constructed Python tasks cover a required argument, exception translation, return shape with public compatibility, and a local edit. The fixtures and acceptance checks were authored together. This is a mechanism check, with no independent task sampling or estimates of live effectiveness.

## Arms and budgets

- No brief receives only the ticket.
- Simple map gets task-ranked signatures plus literal source excerpts.
- Semantic LOD gets task-ranked source spans and related caller evidence from the existing Python fact and resolution layer.

Both generators have a 1,024 UTF-8 byte ceiling, including headers and source annotations. This is a common wire budget. Token accounting depends on the live provider. No brief leaves its allocation unused. All arms use the same ticket and source. Acceptance code, expected line anchors, and patch variants stay out of the brief inputs.

For a future live pilot, use the same agent configuration and native tools for each arm, with a 10-call, 16,000-byte tool response budget and 350-word final response cap. Charge initial prompts and briefs to actual total token cost. Hold post-edit advisories constant in all arms. Set a common wall timeout and randomize arm order before launch. Compare independently judged patch acceptance first and measure cost across all episodes. Use fresh tickets with independent acceptance review for any claim of generalization.

## Offline admission

Verify source digests, deterministic rendering, exact physical anchors, budget compliance, and absence of private evaluator inputs in the compiler interface. Every pristine and incomplete variant must fail its acceptance check and every reference must pass. Compare required physical line coverage at startup separately from file names or signatures. Coverage is diagnostic and is not an admission threshold. No provider calls are part of this preflight.
