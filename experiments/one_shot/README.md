# One-shot brief comparison protocol

This offline pilot compares the initial evidence available before an agent can call any tools. Four fresh, constructed Python tasks cover a crate rounding option, persisted sample counts, exception translation, and local progress clamping. The fixtures and acceptance checks were authored together. This is a mechanism check, with no independent task sampling or estimates of live effectiveness.

## Arms and budgets

- No brief receives only the ticket.
- Simple map gets task-ranked signatures plus literal source excerpts.
- Semantic LOD gets task-ranked source spans and related caller evidence from the existing Python fact and resolution layer.

Both generators have a 1,024 UTF-8 byte ceiling, including headers and source annotations. This is a common wire budget. Token accounting depends on the live provider. No brief leaves its allocation unused. All arms use the same ticket and source. Acceptance code, expected line anchors, and patch variants stay out of the brief inputs.

For a future live pilot, use the same agent configuration and native tools for each arm, with a 10-call, 16,000-byte tool response budget and 350-word final response cap. Charge initial prompts and briefs to actual total token cost. Hold post-edit advisories constant in all arms. Set a common wall timeout and randomize arm order before launch. Compare independently judged patch acceptance first and measure cost across all episodes. Use fresh tickets with independent acceptance review for any claim of generalization.

## Offline admission

Verify source digests, deterministic rendering, exact physical anchors, budget compliance, and absence of private evaluator inputs in the compiler interface. Every pristine and incomplete variant must fail its acceptance check and every reference must pass. Compare required physical line coverage at startup separately from file names or signatures. Coverage is diagnostic and is not an admission threshold. No provider calls are part of this preflight. A separate comparison with the previous one-hop compiler can explain the effect of traversal on these same fixtures.

## Run the preflight

From the repository root, run the following with Python 3.10 or later. Choose a new output directory for each run.

```sh
python experiments/one_shot/preflight.py ./artifacts/one-shot-preflight
```

The receipt includes the pinned fixture digest, per-file source digests, each brief hash, and missing physical lines by arm. The implementation reuses the existing Python fact parser and resolver. This variant takes the best lexical seed and preserves its directly related spans. It then follows up to two uniquely resolved callees of that seed and considers up to four distinct sibling callers in total. The traversal stops at those callers. Source spans retain exact physical lines and must fit within the same brief ceiling. It is a bounded pilot of the upstream approach.

For a separate source snapshot with a plain-text ticket, install the candidate and run:

```sh
python -m mipdelta.brief path/to/snapshot --ticket path/to/ticket.txt --arm lod --max-bytes 1024
```

Use a source-only snapshot with evaluator data outside the source directory. The source directory should be stable while the brief is compiled.
