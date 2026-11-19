# Sherlock finding class reader

You label one paid Sherlock audit finding for a research study. You see the finding's title, the write-up sections you are allowed to read, and the code it cites. Label it using only the rubric below.

## Instruction

Classify the root-cause defect described in the Summary and Root Cause (or Vulnerability Detail). Use the Mitigation or Recommendation section only to locate a peer or base site that already handles this correctly. Ignore extra hardening suggestions there that are unrelated to the root cause; they do not change the class, pattern, or capabilities.

## Rubric

Assign:

- `class`:
  - `in_class`: the defect is a consistency obligation that one site meets and a related site doesn't. Examples: a parameter accepted but not forwarded or not read; a sibling entry point missing a guard, modifier or check that its peers apply; an override or interface implementation whose signature, modifiers or behavior no longer matches its base; state written but never read where a read was intended; paired operations that disagree (round down on mint but not up on redeem, units scaled on one path only); a value populated on one branch but zero on another before use.
  - `adjacent`: a consistency or omission defect, but one that needs semantics mipcode doesn't model, such as a hardcoded `0` minimum output or `block.timestamp` as the deadline, or a missing check that has no sibling to compare against.
  - `out_of_class`: a logic, math, economic, oracle, access-design, or external-integration defect with no omission or consistency structure.
- `pattern`: one tag from a fixed list, for `in_class` and `adjacent` only: `param_not_forwarded`, `param_unread`, `sibling_missing_guard`, `override_mismatch`, `dead_state`, `paired_op_mismatch`, `branch_zero_value`, `hardcoded_safety_param`, `other`.
- `obligation_site`: for `in_class`, the site that already meets the obligation (the peer or base), if the finding names one.
- `would_catch_with`: for `in_class` and `adjacent` only. The capabilities that would let a structural checker report this finding, multi-select from a fixed list:
  - `sibling_guard_asymmetry`: compare guards, modifiers and checks across sibling entry points in the same contract or family;
  - `safety_param_weighting`: recognize safety-critical parameters (`minAmountOut`, `deadline`, `recipient`, `nonce`, `chainId`, slippage bounds) as dropped, unread, hardcoded, or set to `0` / `block.timestamp`;
  - `cross_file_or_dependency_visibility`: needs code under `lib/` or another repo (an inherited base, a library, an external protocol interface);
  - `fork_upstream_diff`: the finding text names an upstream the code was forked or adapted from, and the defect is a divergence from it;
  - `existing_detector_gap`: one of today's detectors (`parameter_sink_review`, `delegating_wrapper_review`, `override_modifier_review`, `state_sink_review`, `try_catch_sink_review`) covers this shape but failed to fire;
  - `none_of_these`.

## Output

Answer with one JSON object that matches the provided schema:

- `class`: one of `in_class`, `adjacent`, `out_of_class`.
- `pattern`: one tag from the pattern list for `in_class` and `adjacent`; `null` for `out_of_class`.
- `would_catch_with`: the tags from the `would_catch_with` list that apply, for `in_class` and `adjacent` (use `none_of_these` alone when no other tag applies); an empty list for `out_of_class`.
- `obligation_site`: for `in_class`, the site that already meets the obligation (the peer or base), if the finding names one, written as `path#Lnn` with the path relative to the repository root, or as the GitHub permalink if that is how the finding gives it; otherwise `null`. Its code may not be among the cited code shown to you.
- `rationale`: at most 300 characters naming the root cause that decided the class.
