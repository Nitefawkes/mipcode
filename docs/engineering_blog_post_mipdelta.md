# We Tried to Beat `ripgrep` for Coding Agents. Here’s Why We Stopped—and What Worked Instead.

For months, we were convinced that coding agents needed a better way to search code.

If you watch an LLM work inside a repository using `ripgrep` (`rg`) and file reads, it looks clumsy at first. It greps for a function name, gets forty hits, reads three files it doesn't need, and eventually finds the right call site. As systems engineers, our instinct was obvious: *give the model a structural index*. Let it query call graphs, symbol definitions, and impact trees directly through custom tools (`mip_open`, `mip_impact`, `mip_refs`).

Then we ran controlled, head-to-head benchmarks against plain `rg` + `read_file`—and got humbled.

On routine single-file edits in our evaluations, a frontier model armed with nothing but `rg` and `read_file` was already fast, cheap, and accurate. Worse, giving the agent our custom structural tools introduced what we came to call the **local-edit tax**. When you hand an agent extra navigation tools, it often uses them—or spends tokens deciding whether to use them—even on a three-line local bugfix. We were adding friction to ordinary edits in order to help on cross-file contract changes.

We had a choice: keep tweaking tool descriptions hoping to beat `rg` at search, or read the failure transcripts and figure out what was actually going wrong.

---

## The Failure Mode We Actually Observed: Premature Completion

When we read the transcripts where `gpt-6.1-sol` (running in Codex) failed our multi-file tasks, something jumped out immediately.

The model rarely failed because it *couldn't* use `rg`. When it knew a caller existed, it found it and fixed it. **It failed because it stopped looking too early.**

Here is a concrete example built on the real 22-file source tree of `pypa/packaging` (the standard third-party Python library that parses `pip` requirements):

We asked the agent to update `parse_requirement` in `packaging/_parser.py` so that whitespace-only requirement strings raise a dedicated `EmptyRequirementError` instead of falling through to a generic syntax error.

In our control run (Arm A, using standard `rg` + `read` + `write`), the agent:
1. Read `packaging/errors.py` and added `class EmptyRequirementError(Exception)`.
2. Read `packaging/_parser.py` and raised `EmptyRequirementError` when the input was empty.
3. Declared victory and finalized.

Total time: 41.39 seconds. Clean code. And **broken at the public caller**.

Why? Because two files over, at `packaging/requirements.py:102`, the public `Requirement.__init__` constructor wraps `_parse_requirement(requirement_string)` in a narrow `try ... except ParserSyntaxError:` block to re-raise a user-facing `InvalidRequirement`. Because `EmptyRequirementError` didn't inherit from `ParserSyntaxError` and wasn't added to that `except` clause, calling `Requirement("   ")` leaked an uncaught internal exception straight through the public API.

The agent didn't fail because `rg` can't find `_parse_requirement`. It failed because the local file looked complete, so **it never ran the search**.

Across our evaluated contract-change tasks, we targeted four recurring forms of silent contract drift:
1. **Return & Exception Drift:** Changing a function to return a 3-tuple instead of a 2-tuple (`a, b = verify_token()` crashes at runtime), or raising a new exception that bypasses a caller's narrow `try/except`.
2. **Wrapper & Subclass Amnesia:** Adding an optional keyword like `base_iri=None` to `Graph.parse()` while forgetting that `ConjunctiveGraph.parse()` overrides or wraps it without forwarding `base_iri`.
3. **Callback & Descriptor Tables:** Adding a required keyword-only argument (`*, strict: bool`) to `dump_header` in `werkzeug/http.py`, catching direct `dump_header(...)` calls, and missing `header_property(..., dump_func=dump_header)` bindings in `werkzeug/sansio/response.py` where `dump_header` is passed as a bare function reference.
4. **Dynamic Plugin Dispatch & Chained Attributes:** Changing `NTSerializer.serialize` or `OptionValidator.validate` when callers invoke it dynamically through a plugin registry (`plugin.serialize(...)`, `v.validate(...)`) or a multi-hop attribute chain (`self.session.token_store.issue(...)`).

Even more telling: in our dynamic plugin tasks, **running the existing unit test suite didn't save the agent.** When the agent ran the configured unit test after editing `NTSerializer.serialize`, the existing tests called `exporter.serialize("nt", triples)` using the old default arguments. The test command returned `exit_code=0`. Seeing green tests convinced the agent it was done—even though passing the new `base_iri` argument through `GraphExporter.serialize` crashed with `TypeError`.

---

## The Pivot: Delete the Search Tools. Guard the `write` and `test` Seam.

Once we reframed the problem as *premature completion after a contract-changing edit*, the architecture flipped upside down:

1. **Remove all custom `mip_*` search tools from the agent's prompt.** Let the agent use regular `rg`, `read`, `write`, and `test`.
2. **Attach a delta detector (`mipdelta`) to the `write` and `test` responses.** Every time the agent writes a file, compare the AST of the file *before* the edit to the AST *after* the edit against a warm in-memory symbol index.
3. **If the edit doesn't change a tracked contract, emit nothing (`0` bytes).** If a function's signature, return shape, raised exceptions, and class state fields haven't drifted in a way that triggers our detectors, the tool output is untouched.
4. **If the edit *does* open a candidate cross-file seam, append a single line (`<= 250` UTF-8 bytes of advisory text per response) to the `write` or `test` response.** Name the affected consumer's file, line number, detector, provenance tag (`[static]`, `[rt-obs]`, or `[type-cand]`), and—when a single seam is flagged and fits the byte cap—the specific mismatch evidence.

For that `packaging` failure above, the moment the agent wrote `packaging/_parser.py`, the `write` response came back with `197` extra bytes at the bottom:

```text
[mip-delta] packaging/requirements.py:102 exception_contract_review[static] parse_requirement (parse_requirement newly raises EmptyRequirementError, bypassing caller try/except (ParserSyntaxError))
```

Instead of saying *"here is another caller,"* the advisory tells the agent *"this new exception escapes this caller's existing handler."* It gives the agent a concrete reason to continue before finalizing.

---

## Extending Coverage Beyond Pure Static ASTs

Pure AST diffing catches direct calls, subclass overrides, and first-class callback tables (`header_property(..., dump_header)`), while abstaining when a callback is already adapted via `lambda` or `functools.partial`.

To extend coverage to cases where static ASTs alone cannot resolve the receiver, we wired in two additional evidence sources:

### 1. Python 3.12 `sys.monitoring` (`[rt-obs]`)
Python 3.12 introduced [PEP 669 (`sys.monitoring`)](https://peps.python.org/pep-0669/), a VM event hook. We wrote a small `sitecustomize` collector (`mipdelta.monitoring`) that activates when the agent runs tests. Whenever a repo function calls a method on an object, it records the `(caller_file, caller_line, receiver_runtime_type, method_name)` edge into a JSON shard (capped per call site to bound overhead, which trades exhaustive polymorphism capture for speed).

Crucially, we made this **order-independent**. Coding agents frequently edit the target file *first* and run `test` *second*. When the `test` command finishes, `mipdelta` ingests the runtime call edges observed during that test run and re-evaluates any files the agent has already modified in the session.

In our constructed `rdflib_pkg` plugin task (`OSS05`), the agent added `*, base_iri=None` to `NTSerializer.serialize` and ran the unit tests. The unit tests passed (`exit_code=0`), because they only tested the default call `exporter.serialize("nt", triples)`. In the control arm, the agent saw green tests and finalized.

In the `mipdelta` arm, even though the unit test passed, `sys.monitoring` observed that `GraphExporter.serialize` at `rdflib_pkg/exporter.py:7` had called `NTSerializer.serialize` during the test run. Right beneath the passing test output, `mipdelta` attached:

```text
[mip-delta] rdflib_pkg/exporter.py:7 delegating_wrapper_review[rt-obs] GraphExporter.serialize (delegating wrapper GraphExporter.serialize calls NTSerializer.serialize without forwarding optional parameter base_iri)
```

The agent opened `rdflib_pkg/exporter.py`, forwarded `base_iri`, and passed the post-episode oracle.

### 2. Conditional `.pyi` Stub Chaining (`[type-cand]`)
When `.pyi` stub files or inline attribute type annotations are present in a workspace, `mipdelta` automatically scans them and folds their attribute/return annotations into the symbol resolver.

To test this mechanism on both a large real source tree and a compact fixture, we evaluated a 109-file `docutils` workspace with benchmark-supplied `nodes.pyi` and `transforms/__init__.pyi` stubs (`OSS07`), as well as a 9-file `web_pkg` fixture with `context.pyi` (`OSS08`). When the agent modified `Reporter.attach_observer` or `TokenStore.issue`, `mipdelta` walked the stub-typed attribute chain (`self.document.reporter.attach_observer` and `self.session.token_store.issue`) and flagged `docutils/parsers/__init__.py:87`, `docutils/transforms/__init__.py:183`, and `web_pkg/views.py:5`.

---

## What We Measured (and How the Tasks Were Constructed)

### 1. Offline Verification: 50 Local OSS Commits + 10 Historical Replay Cases
- **50 Selected Single-Function-Body OSS Commits** across `click`, `packaging`, `platformdirs`, `tomlkit`, and `werkzeug` ([`natural-local-corpus-v2.md`](../results/v2_delta_readout/natural-local-corpus-v2.md)): To test whether `mipdelta` stays silent when an edit only modifies local function body logic without changing signatures or symbols, we replayed 50 real commits selected via a mechanical filter. Across all 50 commits, `mipdelta` emitted **`0` warnings (`0 / 50` commits)**, with a **warm incremental `apply_write` wall-clock median of `5.73 ms` (`28.71 ms` p95; CPU median `6.64 ms` / p95 `32.81 ms`, excluding cold workspace indexing)**.
- **10 Historical Miss Replay Cases** ([`historical-replay-v2.md`](../results/v2_delta_readout/historical-replay-v2.md)): Reduced candidate warnings from `53` in v1 to **`10`** (`-81.13%` chaff removed) at **`8.32 ms` warm median wall time**.

### 2. Live Paired Codex Evaluation: 6 Constructed Pilot Tasks + 8 Multi-Stratum Tasks (`28` Episodes)
We ran two paired live evaluations using `codex-cli` (`gpt-6.1-sol`) comparing **Arm A** (`rg` + `read` + `write` + `test`) against **Arm B** (identical tools + the `<= 250`-byte `mipdelta` advisory on `write` and `test`).

To keep synthetic mechanism checks clearly separated from real OSS source trees, here is the exact breakdown across all 14 tasks ([`live-codex-paired-pilot-v2.md`](../results/v2_delta_readout/live-codex-paired-pilot-v2.md) and [`live-codex-oss-benchmark-v2.md`](../results/v2_delta_readout/live-codex-oss-benchmark-v2.md)):

| Task Group & Provenance | Tasks (`Episodes`) | Arm A (`rg` Control) Oracle Pass | Arm B (`+ mipdelta`) Oracle Pass | Arm B Advisory Bytes (Episode Totals) | Flagged Files Subsequently Edited in Arm B |
| :--- | ---: | ---: | ---: | ---: | ---: |
| **Constructed Local-Edit Pilot (`L01`–`L02`)** | `2` (`4`) | `2 / 2` | `2 / 2` | `0` B | `0 / 0` (silent) |
| **Real-OSS Local-Edit Commits (`OSS01` `platformdirs`, `OSS02` `werkzeug`)** | `2` (`4`) | `2 / 2` | `2 / 2` | `0` B | `0 / 0` (silent) |
| **Constructed Static Contract Pilot (`L03`–`L06`)** | `4` (`8`) | `2 / 4` | **`4 / 4`** | `89–213` B (`3` tasks) | `3 / 3` triggered (`L04` pre-satisfied) |
| **Real-OSS Static Contract Tasks (`OSS03` `werkzeug` 52 files, `OSS04` `packaging` 22 files)** | `2` (`4`) | `1 / 2` | **`2 / 2`** | `197–241` B | `2 / 2` triggered |
| **Constructed Runtime `sys.monitoring` Tasks (`OSS05` `rdflib_pkg`, `OSS06` `cli_pkg`)** | `2` (`4`) | `0 / 2` | **`2 / 2`** | `197–430` B | `2 / 2` triggered |
| **Stub-Typed `[type-cand]` Tasks (`OSS07` real 109-file `docutils` + added `.pyi`; `OSS08` constructed `web_pkg`)** | `2` (`4`) | `1 / 2` | **`2 / 2`** | `158–202` B | `2 / 2` triggered |
| **Aggregate Across Evaluated Tasks** | **`14` (`28`)** | **`8 / 14`** | **`14 / 14`** | **`0` B on local edits** | **`9 / 9` triggered tasks** |

The advisory column sums advisory-text bytes across each episode, not the size of one response. In `OSS05`, **430 B = two 215 B advisories**, delivered on separate `test` responses at sequences 3 and 10; `OSS06` delivered one 197 B advisory on its sequence 4 `test` response. Each advisory is individually below the 250 B cap. These figures exclude JSON framing; the gateway separately bounds added serialized wire bytes to 350 B.

On the two large OSS source trees where both arms passed (`OSS03` `werkzeug` and `OSS07` `docutils`), here is the full token and timing breakdown from [`live-codex-oss-benchmark-v2.json`](../results/v2_delta_readout/live-codex-oss-benchmark-v2.json) (noting that episodes ran sequentially A-then-B):
- **`OSS03` (`werkzeug`, 52 files):**
  - Arm A: `17` calls, `94.83s` wall, `352,660` input tokens (`289,280` cached), `2,072` output tokens (`65,452` non-cached).
  - Arm B: `14` calls, `75.64s` wall, `255,529` input tokens (`225,408` cached), `2,366` output tokens (`32,487` non-cached).
- **`OSS07` (`docutils` + `.pyi`, 109 files):**
  - Arm A: `12` calls, `55.52s` wall, `215,772` input tokens (`159,616` cached), `1,240` output tokens (`57,396` non-cached).
  - Arm B: `11` calls, `42.67s` wall, `191,938` input tokens (`162,944` cached), `1,092` output tokens (`30,086` non-cached).

**Interpreting the timing and token differences:** Arm B made fewer tool calls (`14` vs. `17` on `OSS03`; `11` vs. `12` on `OSS07`) and consumed fewer total input tokens (`255,529` vs. `352,660`; `191,938` vs. `215,772`). Those measurements support reduced interaction work in these recorded trajectories. Non-cached tokens here mean `input_tokens - cached_input_tokens + output_tokens`: `32,487` vs. `65,452` on `OSS03` and `30,086` vs. `57,396` on `OSS07`. This metric is affected by provider cache hits; it is not a cache-independent measure of work. [Provider prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) can reduce input-processing latency. The observed reductions in calls and input volume are consistent with the shorter wall times, but sequential A-then-B execution does not isolate their contribution from provider caching, OS/disk warmth, service variability, or model variation. These are descriptive results, not a causal speedup estimate; a timing comparison needs randomized arm order, repeated runs, and recorded cold/warm conditions.

---

## The Hardest Question: Do You Need an Engine, or Just a Generic Reminder?

After looking at those 14 paired tasks, a skeptical reviewer asked the sharpest question of the project:

> *How much of Arm B's advantage comes from `mipdelta`'s cross-file engine—and how much comes purely from interrupting premature completion after a contract change?*

To find out, we built **Arm R (`Generic Contract Reminder`)** ([`live-codex-reminder-ablation-v2.md`](../results/v2_delta_readout/live-codex-reminder-ablation-v2.md)). Arm R uses no repository index, no `.pyi` stubs, and no `sys.monitoring`. Instead, when a `write` modifies a function's parameters, return tuple arity, `None` return, raised exceptions, or state fields in the edited file alone, Arm R attaches a generic 189-byte reminder:

```text
[reminder] Contract change detected in this file: before finalizing, check callers, delegating wrappers, subclass overrides, return unpackers, and try/except handlers across the repository.
```

We ran Arm R on all **6 discordant tasks** (`L05`, `L06`, `OSS04`, `OSS05`, `OSS06`, `OSS08`) where Arm A (`Control`) had failed (`0/6`) and Arm B (`mipdelta`) had passed (`6/6`). Only the six R episodes were new; A and B were retained historical results. This outcome-selected diagnostic comparison is not a fresh randomized three-arm trial or an estimate of general task success rates:

| Task ID | Stratum | `Arm A` Control (`Calls`, `Bytes`) | `Arm R` Generic Reminder (`Calls`, `Bytes`) | `Arm B` `mipdelta` (`Calls`, `Bytes`) |
| :--- | :--- | :--- | :--- | :--- |
| `L05-delegating-wrapper-forwarding` | `2` (`[static]`) | **`FAIL`** (`3`, `1,172`) | **`PASS`** (`9`, `4,797`) | **`PASS`** (`8`, `3,519`) |
| `L06-return-and-exception-contract` | `2` (`[static]`) | **`FAIL`** (`4`, `1,413`) | **`PASS`** (`11`, `6,827`) | **`PASS`** (`7`, `2,972`) |
| `OSS04-packaging-parse-requirement-exception` | `2` (OSS `[static]`) | **`FAIL`** (`7`, `27,061`) | **`PASS`** (`12`, `41,458`) | **`PASS`** (`12`, `41,473`) |
| `OSS05-rdflib-runtime-plugin-serializer` | `3` (`[rt-obs]`) | **`FAIL`** (`4`, `1,256`) | **`FAIL`** (`8`, `5,045`) | **`PASS`** (`11`, `5,555`) |
| `OSS06-click-runtime-command-loader` | `3` (`[rt-obs]`) | **`FAIL`** (`5`, `1,952`) | **`PASS`** (`11`, `6,331`) | **`PASS`** (`8`, `3,509`) |
| `OSS08-web-stub-session-chain` | `4` (`[type-cand]`) | **`FAIL`** (`3`, `746`) | **`PASS`** (`7`, `3,032`) | **`PASS`** (`6`, `2,005`) |
| **Discordant Cohort Totals (`6` Tasks)** | **Strata 2–4** | **`0 / 6` (`0.0%`)** | **`5 / 6` (`83.3%`)** | **`6 / 6` (`100.0%`)** |

The six new episodes sharpened two hypotheses:

1. **A locally triggered reminder recovered five of the six selected historical failures.**
   Across the five tasks where R and B both passed, R used `50` calls and `62,445` returned bytes versus B's `41` calls and `53,478` bytes: R used `22.0%` more calls, or equivalently B used `18.0%` fewer calls and `14.4%` fewer returned bytes. Pooled non-cached tokens were `148,779` for R versus `153,496` for B, so fewer calls and bytes did not establish lower token cost. R itself includes a local AST change detector; this result supports the value of a well-timed contract-review intervention without establishing how much additional benefit requires repository analysis.
2. **The sole B-only oracle pass (`OSS05`) exposes a specification question as well as a possible diagnosis benefit.**
   The reminder agent searched for `serialize|NTSerializer`, read `rdflib_pkg/exporter.py` and the registry, and received another reminder after a passing test. It then finalized without changing the wrapper. B received the specific forwarding warning and passed the oracle. However, the public ticket requests the optional parameter on `NTSerializer.serialize`, while the oracle additionally requires `GraphExporter.serialize` to expose it; the wrapper's existing default behavior remains compatible. Before treating this difference as a prevented defect, an independent review must establish that public-wrapper propagation was a stated requirement. Preserve these recorded outcomes, and use fresh tickets that specify the required public behavior without naming editable files.

**A direct follow-up is R versus B on larger repositories.** The current small fixtures contain 7 source files each in `L05` and `L06`, 9 in `OSS06`, and 9 files (8 `.py` plus one `.pyi`) in `OSS08`; the packaging task has 22 files. Their small search spaces limit what we can infer about navigation costs, but we did not measure homonym noise. The existing 52-file Werkzeug and 109-file docutils results compare A with B, not R with B. They motivate the hypothesis that specific `file:line` evidence could save more search/read work as lexical ambiguity and cross-module obligations increase; they do not show that a generic reminder forces broad grep sweeps in those repositories. Test that hypothesis on fresh, independently specified tasks with randomized R/B order and repeated episodes, measuring irrelevant search hits, navigation calls, accepted patches, tokens, and wall time. Include cases where a wrapper intentionally must not expose a new option. File count alone is not a measure of search difficulty.

---

## What We Kept From Semantic LOD

We stopped asking the agent to navigate through a separate structural tool catalog. The source anchors, caller graph, and selection logic are still useful research components. One experiment compiles a task brief before the agent starts, then gives the agent its usual search, read, edit, and test tools. The brief uses a fixed allowance and labels the exact source lines it includes. A separate direction would apply specific, executable architecture rules to a patch in CI.

The [one-shot pilot](../experiments/one_shot/README.md) compares starting with the ticket alone, a simple symbol map with lexical excerpts, and a Semantic LOD brief with related source spans. Its offline preflight checks delivery and fixture behavior. In the subsequent [live pilot](../results/one_shot/README.md), each arm passed all three constructed tasks. Semantic LOD returned fewer tool bytes, and the no-brief arm used the fewest total provider tokens and had the lowest wall time. The pilot ran one episode per task and arm. We are keeping the compiler as an experiment alongside the advisory CLI.

---

## Limitations & What We Still Need to Prove

These results show that in selected Python contract-change tasks, small source-anchored warnings delivered after edits or tests helped coding agents repair omissions that search and passing baseline tests did not prevent. At the same time, several important questions remain open:

1. **Scaling the Reminder vs. Engine Comparison to Larger Repositories:**
   The direct R/B experiment described above remains unrun on the larger source trees. Require measured lexical ambiguity and independently specified behavioral obligations; do not infer search difficulty from file count or select tasks based on an observed B win.
2. **Precision on Intentional Breaking Changes (Not Just Silence on Local Edits):**
   Our 50-commit natural corpus verified that `mipdelta` stayed quiet when signatures and symbols didn't change. It does not measure false-positive nuisance when a developer *intentionally* changes an API or writes a wrapper that deliberately hides an inner parameter.
3. **Runtime Sampling vs. Completeness:**
   To keep `sys.monitoring` overhead low during `pytest`, our collector caps observations per call site (`max_events_per_site=8`). In our offline Click benchmark ([`pytest-sys-monitoring-v2.md`](../results/v2_delta_readout/pytest-sys-monitoring-v2.md)), `max_events_per_site=8` observed `88` polymorphic sites versus `206` under unbounded monitoring. Runtime edges are positive evidence of executed paths, never proof that unobserved callers do not exist.
4. **Independent Shadow-Mode Usage:**
   Constructed and maintainer-curated benchmarks can validate a mechanism, but real product validation requires independent Python developers running `mip-delta --check-git` and `mip-delta --run-pytest` on their own everyday work and choosing to keep it enabled.
